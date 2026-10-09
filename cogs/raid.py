"""
/raid — Find good raid targets for the linked nation.
Uses a calibrated public estimate (infra + inactivity + military).
"""

import re
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button

from utils import database, embeds
from cogs.coalitions import is_dnr_protected


def parse_duration(text: str) -> timedelta:
    text = text.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([dwmy])", text)
    if not match:
        raise ValueError("Use format like 7d, 2w, 1m, 3m, 1y")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    if unit == "m":
        return timedelta(days=amount * 30)
    if unit == "y":
        return timedelta(days=amount * 365)
    raise ValueError("Unknown unit")


def inactive_since(last_active_iso: str | None, cutoff: timedelta) -> bool:
    if not last_active_iso:
        return True
    try:
        last = datetime.fromisoformat(last_active_iso.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last >= cutoff
    except Exception:
        return True


def military_strength(n: dict) -> float:
    return (
        (n.get("soldiers") or 0)
        + (n.get("tanks") or 0) * 10
        + (n.get("aircraft") or 0) * 40
        + (n.get("ships") or 0) * 60
    )


def used_defensive_slots(n: dict) -> int:
    """How many defensive wars are active (0–3). Matches in-game 'Defensive Wars: X/3'."""
    wars = n.get("defensive_wars")
    if not wars:
        return 0
    active = 0
    for w in wars:
        turns = w.get("turns_left")
        if turns is not None and turns > 0:
            active += 1
    return min(active, 3)


def free_defensive_slots(n: dict) -> int:
    """Free slots = 3 - used."""
    return max(0, 3 - used_defensive_slots(n))


def loot_estimate(n: dict, my_strength: float) -> float:
    """
    Calibrated against real spy + Locutus data.
    Goal: Wyzfert-style nation (34c / 75k infra / very inactive / almost no mil)
    should land around $25M–$40M estimated loot.
    """
    cities = n.get("num_cities") or len(n.get("cities") or []) or 1
    infra = n.get("total_infra") or 0

    inactive_days = 30
    if n.get("last_active"):
        try:
            last = datetime.fromisoformat(n["last_active"].replace("Z", "+00:00"))
            inactive_days = (datetime.now(timezone.utc) - last).days
        except Exception:
            pass

    # Strong base from infra + cities
    score = infra * 280 + cities * 220_000

    # Heavy inactivity bonus (stockpile builds up)
    score += min(inactive_days, 150) * 55_000

    # Free slots are very valuable — fully slotted targets sink
    slots = free_defensive_slots(n)
    if slots == 0:
        score *= 0.25          # heavy penalty
    else:
        score += slots * 2_000_000

    # Prefer weaker targets
    their_str = military_strength(n)
    if my_strength > 0 and their_str > 0:
        ratio = my_strength / max(their_str, 1)
        if ratio >= 3:
            score *= 1.4
        elif ratio >= 1.8:
            score *= 1.2
        elif ratio < 0.9:
            score *= 0.45

    # Beige penalty
    if (n.get("beige_turns") or 0) > 0:
        score *= 0.55

    return max(score, 0)


def format_money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value/1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value/1_000:.0f}k"
    return f"${value:.0f}"


class RaidView(View):
    def __init__(self, targets: list, me_name: str, inactive: str, page: int = 0):
        super().__init__(timeout=180)
        self.targets = targets
        self.me_name = me_name
        self.inactive = inactive
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
            last = n.get("last_active")
            last_str = "unknown"
            if last:
                try:
                    dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    days = (datetime.now(timezone.utc) - dt).days
                    last_str = f"{days}d ago"
                except Exception:
                    pass

            beige = n.get("beige_turns") or 0
            slots = used_defensive_slots(n)
            mil = (
                f"{n.get('soldiers', 0):,}s "
                f"{n.get('tanks', 0):,}t "
                f"{n.get('aircraft', 0):,}a "
                f"{n.get('ships', 0):,}n"
            )
            est = format_money(n.get("_loot_score", 0))

            lines.append(
                f"**{i}. [{name}](https://politicsandwar.com/nation/id={nid})** ({alliance}) — est. {est}\n"
                f"Cities: {cities} | Infra: {infra:,.0f} | Slots: {slots}/3 | Beige: {beige}\n"
                f"Military: `{mil}` | Last active: {last_str}"
            )

        total_pages = max(1, (len(self.targets) - 1) // self.per_page + 1)
        embed = embeds.info(
            f"Raid Targets for {self.me_name}",
            f"Showing page **{self.page + 1}/{total_pages}** • inactive ≥ **{self.inactive}** • {len(self.targets)} total\n\n"
            + "\n\n".join(lines)
        )
        embed.set_footer(text="Estimated loot (public data) • Use Pirate policy for +40% loot")
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


class Raid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="raid", description="Find good raid targets for your nation")
    @app_commands.describe(
        inactive="Minimum inactivity (e.g. 7d, 2w, 1m, 3m, 1y). Default 7d",
        results="How many targets to fetch (10-50). Default 30",
        include_vm="Include nations in Vacation Mode",
        include_beige="Include nations currently on beige",
        weak_only="Only show targets weaker than you",
        safe_only="Only very low military targets (safest raids)",
        include_slotted="Include nations with 0 free defensive slots (default: hide them)",
    )
    async def raid(
        self,
        interaction: discord.Interaction,
        inactive: str = "7d",
        results: app_commands.Range[int, 10, 50] = 30,
        include_vm: bool = False,
        include_beige: bool = False,
        weak_only: bool = False,
        safe_only: bool = False,
        include_slotted: bool = False,
    ):
        await interaction.response.defer()

        nation_id = await database.get_nation_for_user(interaction.user.id)
        if not nation_id:
            await interaction.followup.send(
                embed=embeds.error("Not Linked", "Link your nation first with `/link`.")
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
        my_strength = military_strength(me)
        min_score = my_score * 0.75
        max_score = my_score * 2.5

        try:
            cutoff = parse_duration(inactive)
        except ValueError as e:
            await interaction.followup.send(embed=embeds.error("Invalid Duration", str(e)))
            return

        try:
            candidates = await self.bot.pw_client.get_nations_for_raid(min_score, max_score)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("API Error", str(e)))
            return

        filtered = []
        for n in candidates:
            if n.get("id") == me.get("id"):
                continue
            if not include_vm and (n.get("vacation_mode_turns") or 0) > 0:
                continue
            if not include_beige and (n.get("beige_turns") or 0) > 0:
                continue
            if not inactive_since(n.get("last_active"), cutoff):
                continue

            their_str = military_strength(n)
            if weak_only and their_str >= my_strength:
                continue
            if safe_only and their_str > 5000:
                continue

            # Hide fully slotted targets unless include_slotted=True
            if not include_slotted and free_defensive_slots(n) <= 0:
                continue

            try:
                alliance_id = n.get("alliance_id")
                position = n.get("alliance_position")
                if alliance_id and await is_dnr_protected(self.bot, interaction.guild_id, alliance_id, position):
                    continue
            except Exception:
                pass

            filtered.append(n)

        for n in filtered:
            n["_loot_score"] = loot_estimate(n, my_strength)

        filtered.sort(key=lambda x: x["_loot_score"], reverse=True)
        top = filtered[:results]

        if not top:
            await interaction.followup.send(
                embed=embeds.info(
                    "No Targets Found",
                    f"No nations matched your filters (inactive ≥ {inactive})."
                )
            )
            return

        view = RaidView(top, me.get("nation_name", "You"), inactive)
        await interaction.followup.send(embed=view.build_embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Raid(bot))
