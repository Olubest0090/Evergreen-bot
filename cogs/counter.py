"""
/counter nation — finds Evergreen members who can legally and
effectively counter-attack a given enemy nation.

A candidate qualifies if, from their own score, the enemy falls within
THEIR offensive war range (score x0.75 to x2.5), they have a free
offensive war slot, and their soldiers/tanks/aircraft/ships each meet
or beat the enemy's. Results are sorted by total military strength,
strongest first.
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds
from utils.formatting import nation_block
from cogs.link import resolve_nation

MAX_RESULTS = 10


def max_offensive_slots(nation: dict) -> int:
    if nation.get("advanced_pirate_economy"):
        return 7
    if nation.get("pirate_economy"):
        return 6
    return 5


def free_offensive_slots(nation: dict) -> int:
    active = sum(1 for w in (nation.get("offensive_wars") or []) if (w.get("turns_left") or 0) > 0)
    return max_offensive_slots(nation) - active


def qualifies(candidate: dict, enemy: dict) -> bool:
    score = candidate.get("score", 0)
    att_low, att_high = score * 0.75, score * 2.5
    enemy_score = enemy.get("score", 0)
    if not (att_low <= enemy_score <= att_high):
        return False
    if free_offensive_slots(candidate) <= 0:
        return False
    for key in ("soldiers", "tanks", "aircraft", "ships"):
        if (candidate.get(key) or 0) < (enemy.get(key) or 0):
            return False
    return True


def military_total(nation: dict) -> float:
    return (
        (nation.get("soldiers") or 0)
        + (nation.get("tanks") or 0) * 10
        + (nation.get("aircraft") or 0) * 40
        + (nation.get("ships") or 0) * 60
    )


class Counter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    counter_group = app_commands.Group(name="counter", description="Find members who can counter-attack a nation")

    @counter_group.command(name="nation", description="Find Evergreen members who can counter this nation")
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

        candidates = [m for m in members if m.get("id") != enemy.get("id") and qualifies(m, enemy)]
        candidates.sort(key=military_total, reverse=True)

        embed = embeds.info(f"Counters for {enemy.get('nation_name', 'Unknown')}")
        parts = [f"**Enemy:**\n{nation_block(enemy, None, None)}"]

        if not candidates:
            parts.append("*No qualifying members found right now.*")
        else:
            parts.append("**Counters:**")
            for m in candidates[:MAX_RESULTS]:
                discord_id = await database.get_discord_id_for_nation(m["id"])
                tag = f" — <@{discord_id}>" if discord_id else ""
                parts.append(f"{nation_block(m, None, None)}{tag}")
            if len(candidates) > MAX_RESULTS:
                parts.append(f"*+{len(candidates) - MAX_RESULTS} more qualify but are not shown*")

        embed.description = "\n\n".join(parts)[:4096]
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Counter(bot))
