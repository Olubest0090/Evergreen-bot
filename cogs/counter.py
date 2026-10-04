"""
/counter nation — lists Evergreen members eligible to counter-attack a
given enemy nation: in the enemy's defensive range from their own
score, with a free offensive slot, and active within 7 days. Shown for
everyone who qualifies, strongest first, regardless of whether their
military beats the enemy's, that stricter check is reserved for the
automatic DM trigger in alerts.py, not this manual lookup command.
"""

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds
from utils.formatting import nation_block
from cogs.link import resolve_nation

MAX_RESULTS_PER_MESSAGE = 20
INACTIVITY_CUTOFF_DAYS = 7


def max_offensive_slots(nation: dict) -> int:
    if nation.get("advanced_pirate_economy"):
        return 7
    if nation.get("pirate_economy"):
        return 6
    return 5


def free_offensive_slots(nation: dict) -> int:
    active = sum(1 for w in (nation.get("offensive_wars") or []) if (w.get("turns_left") or 0) > 0)
    return max_offensive_slots(nation) - active


def is_inactive(last_active_iso: str | None, days: int = INACTIVITY_CUTOFF_DAYS) -> bool:
    if not last_active_iso:
        return False
    try:
        last = datetime.fromisoformat(last_active_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).days >= days
    except Exception:
        return False


def military_total(nation: dict) -> float:
    return (
        (nation.get("soldiers") or 0)
        + (nation.get("tanks") or 0) * 10
        + (nation.get("aircraft") or 0) * 40
        + (nation.get("ships") or 0) * 60
    )


def in_range(candidate: dict, enemy: dict) -> bool:
    score = candidate.get("score", 0)
    low, high = score * 0.75, score * 2.5
    return low <= enemy.get("score", 0) <= high


def qualifies(candidate: dict, enemy: dict) -> bool:
    """Strict version used by the AUTOMATIC counter-request DM: must be
    in range, have a free slot, be active, AND actually outgun the
    enemy on every unit type. Deliberately stricter than the manual
    /counter nation command below, which lists everyone eligible to
    try, not just who's favored to win."""
    if not in_range(candidate, enemy):
        return False
    if free_offensive_slots(candidate) <= 0:
        return False
    if is_inactive(candidate.get("last_active")):
        return False
    for key in ("soldiers", "tanks", "aircraft", "ships"):
        if (candidate.get(key) or 0) < (enemy.get(key) or 0):
            return False
    return True


def list_candidates(candidate: dict, enemy: dict) -> bool:
    """Broader version for the /counter nation command: in range, has a
    free slot, and active. No military-strength requirement, results
    are sorted by strength instead so the viewer judges for themself."""
    if not in_range(candidate, enemy):
        return False
    if free_offensive_slots(candidate) <= 0:
        return False
    if is_inactive(candidate.get("last_active")):
        return False
    return True


class Counter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    counter_group = app_commands.Group(name="counter", description="Find members who can counter-attack a nation")

    @counter_group.command(name="nation", description="List every member who can counter this nation")
    @app_commands.describe(nation="Nation ID, URL, or exact name of the enemy")
    async def counter_nation(self, interaction: discord.Interaction, nation: str):
        await interaction.response.defer()

        try:
            enemy = await resolve_nation(self.bot.pw_client, nation)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return
        if not enemy:
            await interaction.followup.send(embed=embeds.error("Nation Not Found", f"No nation matching `{nation}`."))
            return

        config = await database.get_alerts_config(interaction.guild_id)
        alliance_id = config.get("alliance_id") if config else None
        if not alliance_id:
            await interaction.followup.send(
                embed=embeds.error("No Alliance Set", "Set one first with /alerts alliance.")
            )
            return

        try:
            members = await self.bot.pw_client.get_alliance_members(alliance_id)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return

        candidates = [m for m in members if m.get("id") != enemy.get("id") and list_candidates(m, enemy)]
        candidates.sort(key=military_total, reverse=True)

        enemy_embed = embeds.info(f"Counters for {enemy.get('nation_name', 'Unknown')}")
        enemy_embed.description = f"**Enemy:**\n{nation_block(enemy, None, None)}\n\n**{len(candidates)} member(s) qualify.**"
        await interaction.followup.send(embed=enemy_embed)

        if not candidates:
            return

        # Sent in chunks since a long, fully-detailed list (your request
        # was "I don't care if it's very long, list all of them") can
        # exceed a single embed's size limit.
        for i in range(0, len(candidates), MAX_RESULTS_PER_MESSAGE):
            chunk = candidates[i : i + MAX_RESULTS_PER_MESSAGE]
            lines = []
            for m in chunk:
                discord_id = await database.get_discord_id_for_nation(m["id"])
                if not discord_id:
                    link_status = "*not linked in Discord*"
                else:
                    member_obj = interaction.guild.get_member(discord_id)
                    status_labels = {
                        discord.Status.online: "🟢 Online",
                        discord.Status.idle: "🌙 Idle",
                        discord.Status.dnd: "⛔ Do Not Disturb",
                        discord.Status.offline: "⚫ Offline",
                    }
                    status = status_labels.get(member_obj.status, "⚫ Offline") if member_obj else "*not in this server*"
                    link_status = f"<@{discord_id}> — {status}"
                lines.append(f"{nation_block(m, None, None)}\n{link_status}")
            embed = embeds.info(f"Qualifying Members ({i + 1}-{i + len(chunk)})")
            embed.description = "\n\n".join(lines)[:4096]
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Counter(bot))
