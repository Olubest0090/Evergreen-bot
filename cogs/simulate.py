""" /simulate air|ground|naval — P&W battle odds """

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import embeds
from utils.battle_sim import Force, simulate_air, simulate_ground, simulate_naval, ratio_label, VICTORY_NAME
from cogs.link import resolve_nation


def _force_from_nation(n: dict, munitions: bool = True) -> Force:
    return Force(
        soldiers=int(n.get("soldiers") or 0),
        tanks=int(n.get("tanks") or 0),
        aircraft=int(n.get("aircraft") or 0),
        ships=int(n.get("ships") or 0),
        munitions=munitions,
    )


def _fmt_result(title: str, r: dict) -> discord.Embed:
    e = embeds.info(title, "")
    e.description = (
        f"**Likely:** {VICTORY_NAME[r['likely']]}\n"
        f"IT {r['immense_pct']:.0f}% · Mod {r['mod_pct']:.0f}% · "
        f"Pyr {r['pyr_pct']:.0f}% · Fail {r['fail_pct']:.0f}%\n"
        f"Strength {r['att_value']:.0f} vs {r['def_value']:.0f} "
        f"({ratio_label(r['att_value'], r['def_value'])})\n"
        f"MAP cost: **{r['map_cost']}**"
    )
    if r.get("immense_gives"):
        e.add_field(name="Immense gives", value=r["immense_gives"], inline=False)
    return e


class Simulate(commands.Cog):
    sim = app_commands.Group(name="simulate", description="Battle odds (P&W formulas)")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _two_nations(self, interaction: discord.Interaction, attacker: str, defender: str):
        att = await resolve_nation(self.bot, interaction, attacker)
        deff = await resolve_nation(self.bot, interaction, defender)
        return att, deff

    @sim.command(name="air", description="Simulate airstrike / dogfight odds")
    @app_commands.describe(attacker="Attacker nation", defender="Defender nation")
    async def air(self, interaction: discord.Interaction, attacker: str, defender: str):
        await interaction.response.defer()
        att_n, def_n = await self._two_nations(interaction, attacker, defender)
        if not att_n or not def_n:
            return await interaction.followup.send(embed=embeds.error("Nation not found"))
        r = simulate_air(_force_from_nation(att_n), _force_from_nation(def_n))
        emb = _fmt_result(
            f"Air: {att_n.get('nation_name')} vs {def_n.get('nation_name')}", r
        )
        emb.add_field(
            name="Planes",
            value=f"{att_n.get('aircraft')} vs {def_n.get('aircraft')}",
            inline=True,
        )
        await interaction.followup.send(embed=emb)

    @sim.command(name="ground", description="Simulate ground battle odds")
    @app_commands.describe(
        attacker="Attacker nation",
        defender="Defender nation",
        attacker_air_sup="Does attacker already have air superiority?",
        defender_air_sup="Does defender have air superiority over attacker?",
        munitions="Attacker arms soldiers with munitions",
    )
    async def ground(
        self,
        interaction: discord.Interaction,
        attacker: str,
        defender: str,
        attacker_air_sup: bool = False,
        defender_air_sup: bool = False,
        munitions: bool = True,
    ):
        await interaction.response.defer()
        att_n, def_n = await self._two_nations(interaction, attacker, defender)
        if not att_n or not def_n:
            return await interaction.followup.send(embed=embeds.error("Nation not found"))
        r = simulate_ground(
            _force_from_nation(att_n, munitions),
            _force_from_nation(def_n, True),
            att_has_air_sup=attacker_air_sup,
            def_has_air_sup=defender_air_sup,
        )
        emb = _fmt_result(
            f"Ground: {att_n.get('nation_name')} vs {def_n.get('nation_name')}", r
        )
        emb.add_field(
            name="Units",
            value=(
                f"Att S/T {att_n.get('soldiers')}/{att_n.get('tanks')}\n"
                f"Def S/T {def_n.get('soldiers')}/{def_n.get('tanks')}"
            ),
            inline=True,
        )
        await interaction.followup.send(embed=emb)

    @sim.command(name="naval", description="Simulate naval battle / blockade odds")
    @app_commands.describe(attacker="Attacker nation", defender="Defender nation")
    async def naval(self, interaction: discord.Interaction, attacker: str, defender: str):
        await interaction.response.defer()
        att_n, def_n = await self._two_nations(interaction, attacker, defender)
        if not att_n or not def_n:
            return await interaction.followup.send(embed=embeds.error("Nation not found"))
        r = simulate_naval(_force_from_nation(att_n), _force_from_nation(def_n))
        emb = _fmt_result(
            f"Naval: {att_n.get('nation_name')} vs {def_n.get('nation_name')}", r
        )
        emb.add_field(
            name="Ships",
            value=f"{att_n.get('ships')} vs {def_n.get('ships')}",
            inline=True,
        )
        await interaction.followup.send(embed=emb)


async def setup(bot: commands.Bot):
    await bot.add_cog(Simulate(bot))
