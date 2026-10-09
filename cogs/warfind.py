"""
/war enemy — find enemy war targets (ENEMIES bloc only).
Optional ranking boosts: high_infra, high_military, high_loot.
"""

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button

from utils import database, embeds


def military_strength(n: dict) -> float:
    return (
        (n.get("soldiers") or 0)
        + (n.get("tanks") or 0) * 10
        + (n.get("aircraft") or 0) * 40
        + (n.get("ships") or 0) * 60
    )


def used_defensive_slots(n: dict) -> int:
    wars = n.get("defensive_wars") or []
    active = sum(1 for w in wars if (w.get("turns_left") or 0) > 0)
    return min(active, 3)


def free_defensive_slots(n: dict) -> int:
    return max(0, 3 - used_defensive_slots(n))


def loot_estimate(n: dict) -> float:
    cities = n.get("num_cities") or len(n.get("cities") or []) or 1
    infra = n.get("total_infra") or 0
    inactive_days = 0
    if n.get("last_active"):
        try:
            last = datetime.fromisoformat(n["last_active"].replace("Z", "+00:00"))
            inactive_days = (datetime.now(timezone.utc) - last).days
        except Exception:
            pass
    score = infra * 280 + cities * 220_000
    score += min(inactive_days, 150) * 55_000
    free = free_defensive_slots(n)
    if free == 0:
        score *= 0.35
    else:
        score += free * 1_500_000
    if (n.get("beige_turns") or 0) > 0:
        score *= 0.55
    return max(score, 0)


def format_money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value/1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value/1_000:.0f}k"
    return f"${value:.0f}"


def rank_score(n: dict, high_infra: bool, high_military: bool, high_loot: bool) -> float:
    """Combine optional boosts into one sort key."""
    infra = n.get("total_infra") or 0
    mil = military_strength(n)
    loot = n.get("_loot_score") or loot_estimate(n)

    # Normalize roughly so each toggle has similar weight
    score = 0.0
    if high_infra:
        score += infra
    if high_military:
        score += mil * 15  # mil numbers are smaller than infra
    if high_loot:
        score += loot / 1000

    # If nothing selected, default to balanced: loot + some infra
    if not (high_infra or high_military or high_loot):
        score = (loot / 1000) + infra * 0.3 + mil * 5

    # Prefer free slots slightly always
    score += free_defensive_slots(n) * 50_000
    return score


class EnemyView(View):
    def __init__(self, targets: list, me_name: str, flags: str, page: int = 0):
        super().__init__(timeout=180)
        self.targets = targets
        self.me_name = me_name
        self.flags = flags
        self.page = page
        self.per_page = 10

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.targets[start:end]

        lines = []
        for i, n in enumerate(chunk, start + 1):
            name = n.get("nation_name", "Unknown")
            nid = n.get("id")
            alliance = (n.get("alliance") or {}).get("name") or "None"
            cities = n.get("num_cities") or len(n.get("cities") or []) or "?"
            infra = n.get("total_infra") or 0
            used = used_defensive_slots(n)
            beige = n.get("beige_turns") or 0
            mil = (
                f"{n.get('soldiers', 0):,}s "
                f"{n.get('tanks', 0):,}t "
                f"{n.get('aircraft', 0):,}a "
                f"{n.get('ships', 0):,}n"
            )
            est = format_money(n.get("_loot_score") or 0)
            last_str = "unknown"
            if n.get("last_active"):
                try:
                    dt = datetime.fromisoformat(n["last_active"].replace("Z", "+00:00"))
                    days = (datetime.now(timezone.utc) - dt).days
                    last_str = f"{days}d ago"
                except Exception:
                    pass

            lines.append(
                f"**{i}. [{name}](https://politicsandwar.com/nation/id={nid})** ({alliance}) — est. {est}\n"
                f"Cities: {cities} | Infra: {infra:,.0f} | Slots: {used}/3 | Beige: {beige}\n"
                f"Military: `{mil}` | Last active: {last_str}"
            )

        total_pages = max(1, (len(self.targets) - 1) // self.per_page + 1)
        embed = embeds.info(
            f"Enemy Targets for {self.me_name}",
            f"Page **{self.page + 1}/{total_pages}** • {len(self.targets)} total"
            + (f" • filters: {self.flags}" if self.flags else " • balanced rank")
            + "\n\n"
            + "\n\n".join(lines)
        )
        embed.set_footer(text="ENEMIES bloc only • Slots = used defensive wars")
        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: Button):
        if self.page > 0:
            self.page -= 1
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: Button):
        max_page = (len(self.targets) - 1) // self.per_page
        if self.page < max_page:
            self.page += 1
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.defer()


class WarFind(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    war_group = app_commands.Group(name="war", description="War target tools")

    @war_group.command(name="enemy", description="Find enemy war targets (ENEMIES bloc only)")
    @app_commands.describe(
        high_infra="Boost high-infrastructure targets",
        high_military="Boost high-military targets",
        high_loot="Boost high estimated-loot targets",
        results="How many targets to return (5–40). Default 15",
        include_slotted="Include fully slotted enemies (3/3). Default hide",
    )
    async def war_enemy(
        self,
        interaction: discord.Interaction,
        high_infra: bool = False,
        high_military: bool = False,
        high_loot: bool = False,
        results: app_commands.Range[int, 5, 40] = 15,
        include_slotted: bool = False,
    ):
        await interaction.response.defer()

        nation_id = await database.get_nation_for_user(interaction.user.id)
        if not nation_id:
            await interaction.followup.send(
                embed=embeds.error("Not Linked", "Link your nation first with `/link`.")
            )
            return

        # ENEMIES bloc
        enemy_rows = await database.list_coalitions(interaction.guild_id, "ENEMIES")
        enemy_ids = {int(r["alliance_id"]) for r in enemy_rows if r.get("alliance_id") is not None}
        if not enemy_ids:
            await interaction.followup.send(
                embed=embeds.error(
                    "No Enemies Configured",
                    "Add alliances to the **ENEMIES** bloc with `/bloc add` first.",
                )
            )
            return

        try:
            me = await self.bot.pw_client.get_nation(nation_id)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", str(e)))
            return
        if not me:
            await interaction.followup.send(embed=embeds.error("Nation Not Found", "Could not load your nation."))
            return

        my_score = me.get("score") or 0
        min_score = my_score * 0.75
        max_score = my_score * 2.5

        try:
            candidates = await self.bot.pw_client.get_nations_for_raid(min_score, max_score)
        except Exception as e:
            msg = str(e)
            if "rate limit" in msg.lower():
                await interaction.followup.send(
                    embed=embeds.error("Rate Limited", "P&W API rate limit — try again in a minute.")
                )
            else:
                await interaction.followup.send(embed=embeds.error("API Error", msg))
            return

        filtered = []
        for n in candidates:
            if n.get("id") == me.get("id"):
                continue
            aid = n.get("alliance_id")
            if aid is None or int(aid) not in enemy_ids:
                continue
            if (n.get("vacation_mode_turns") or 0) > 0:
                continue
            if not include_slotted and free_defensive_slots(n) <= 0:
                continue
            n["_loot_score"] = loot_estimate(n)
            n["_rank"] = rank_score(n, high_infra, high_military, high_loot)
            filtered.append(n)

        filtered.sort(key=lambda x: x["_rank"], reverse=True)
        top = filtered[:results]

        if not top:
            await interaction.followup.send(
                embed=embeds.info(
                    "No Enemy Targets",
                    "No nations in the **ENEMIES** bloc are in your war range right now.",
                )
            )
            return

        flags = []
        if high_infra:
            flags.append("high infra")
        if high_military:
            flags.append("high military")
        if high_loot:
            flags.append("high loot")
        flag_str = ", ".join(flags)

        view = EnemyView(top, me.get("nation_name", "You"), flag_str)
        await interaction.followup.send(embed=view.build_embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(WarFind(bot))
