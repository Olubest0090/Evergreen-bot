"""
/raid — Find good raid targets for the linked nation.
"""

import re
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds
from cogs.coalitions import is_dnr_protected

# ---------- helpers ----------

def parse_duration(text: str) -> timedelta:
    """Parse 7d, 2w, 1m, 3m, 1y etc. into a timedelta."""
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
        return True  # treat missing as inactive
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


def free_defensive_slots(n: dict) -> int:
    active = sum(1 for w in (n.get("defensive_wars") or []) if (w.get("turns_left") or 0) > 0)
    return max(0, 3 - active)


def loot_estimate(n: dict, my_strength: float) -> float:
    """Simple public loot score (higher = better target)."""
    cities = n.get("num_cities") or len(n.get("cities") or []) or 1
    infra = n.get("total_infra") or 0
    inactive_days = 0
    if n.get("last_active"):
        try:
            last = datetime.fromisoformat(n["last_active"].replace("Z", "+00:00"))
            inactive_days = (datetime.now(timezone.utc) - last).days
        except Exception:
            inactive_days = 30

    # Base value from infra + cities
    score = infra * 0.8 + cities * 1200

    # Inactivity bonus
    score += min(inactive_days, 60) * 80

    # Prefer targets with free slots
    score += free_defensive_slots(n) * 1500

    # Prefer weaker targets
    their_str = military_strength(n)
    if my_strength > 0 and their_str > 0:
        ratio = my_strength / their_str
        if ratio > 1.5:
            score *= 1.25
        elif ratio < 0.8:
            score *= 0.6

    # Beige penalty
    if (n.get("beige_turns") or 0) > 0:
        score *= 0.7

    return score


# ---------- cog ----------

class Raid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="raid", description="Find good raid targets for your nation")
    @app_commands.describe(
        inactive="Minimum inactivity (e.g. 7d, 2w, 1m, 3m, 1y). Default 7d",
        results="How many targets to show (1-20). Default 10",
        include_vm="Include nations in Vacation Mode",
        include_beige="Include nations currently on beige",
        weak_only="Only show targets weaker than you",
    )
    async def raid(
        self,
        interaction: discord.Interaction,
        inactive: str = "7d",
        results: app_commands.Range[int, 1, 20] = 10,
        include_vm: bool = False,
        include_beige: bool = False,
        weak_only: bool = False,
    ):
        await interaction.response.defer()

        # 1. Get the caller's linked nation
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

        # 2. Parse inactivity
        try:
            cutoff = parse_duration(inactive)
        except ValueError as e:
            await interaction.followup.send(embed=embeds.error("Invalid Duration", str(e)))
            return

        # 3. Fetch candidates
        try:
            candidates = await self.bot.pw_client.get_nations_for_raid(min_score, max_score)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("API Error", str(e)))
            return

        # 4. Apply filters
        filtered = []
        for n in candidates:
            if n.get("id") == me.get("id"):
                continue

            # Vacation mode
            if not include_vm and (n.get("vacation_mode_turns") or 0) > 0:
                continue

            # Beige
            if not include_beige and (n.get("beige_turns") or 0) > 0:
                continue

            # Inactivity
            if not inactive_since(n.get("last_active"), cutoff):
                continue

            # Weak only
            if weak_only and military_strength(n) >= my_strength:
                continue

            # DNR check (uses existing logic)
            try:
                alliance_id = n.get("alliance_id")
                position = n.get("alliance_position")
                if alliance_id and await is_dnr_protected(self.bot, interaction.guild_id, alliance_id, position):
                    continue
            except Exception:
                pass

            # Must have at least one free defensive slot ideally, but we still show them
            filtered.append(n)

        # 5. Score & sort
        for n in filtered:
            n["_loot_score"] = loot_estimate(n, my_strength)

        filtered.sort(key=lambda x: x["_loot_score"], reverse=True)
        top = filtered[:results]

        # 6. Build embed
        if not top:
            await interaction.followup.send(
                embed=embeds.info(
                    "No Targets Found",
                    f"No nations matched your filters (inactive ≥ {inactive}, "
                    f"VM={'yes' if include_vm else 'no'}, beige={'yes' if include_beige else 'no'}, "
                    f"weak_only={'yes' if weak_only else 'no'})."
                )
            )
            return

        lines = []
        for i, n in enumerate(top, 1):
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
            slots = free_defensive_slots(n)
            mil = (
                f"{n.get('soldiers', 0):,}s "
                f"{n.get('tanks', 0):,}t "
                f"{n.get('aircraft', 0):,}a "
                f"{n.get('ships', 0):,}n"
            )

            lines.append(
                f"**{i}. [{name}](https://politicsandwar.com/nation/id={nid})** ({alliance})\n"
                f"Cities: {cities} | Infra: {infra:,.0f} | Slots: {slots}/3 | Beige: {beige}\n"
                f"Military: `{mil}` | Last active: {last_str}"
            )

        embed = embeds.info(
            f"Raid Targets for {me.get('nation_name')}",
            f"Showing top **{len(top)}** targets (inactive ≥ **{inactive}**)\n\n"
            + "\n\n".join(lines)
        )
        embed.set_footer(text="Sorted by estimated loot value • Use Pirate policy for +40% loot")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Raid(bot))
