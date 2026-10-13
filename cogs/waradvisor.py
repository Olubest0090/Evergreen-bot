"""War advisor — MAP-aware priorities + optional advisor channel."""

from __future__ import annotations

import os

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds
from utils.battle_sim import (
    Force,
    advice_for_war,
    infer_controls_from_attacks,
    simulate_air,
    simulate_ground,
    simulate_naval,
    ratio_label,
)
from cogs.link import resolve_nation

ADVISOR_CHANNEL_ID = int(os.environ.get("WAR_ADVISOR_CHANNEL_ID", "0") or 0)


def _force(n: dict) -> Force:
    return Force(
        soldiers=int(n.get("soldiers") or 0),
        tanks=int(n.get("tanks") or 0),
        aircraft=int(n.get("aircraft") or 0),
        ships=int(n.get("ships") or 0),
        munitions=True,
    )


class WarAdvisor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="waradvice", description="MAP + control aware advice for a war target")
    @app_commands.describe(
        enemy="Enemy nation (name/id/link)",
        attacker="Your nation (optional — uses linked nation)",
        war_id="Optional active war id for live MAP/resistance/controls",
    )
    async def waradvice(
        self,
        interaction: discord.Interaction,
        enemy: str,
        attacker: str | None = None,
        war_id: int | None = None,
    ):
        await interaction.response.defer()
        client = self.bot.pw_client

        if attacker:
            att_n = await resolve_nation(self.bot, interaction, attacker)
        else:
            # linked nation
            from utils import database as db
            link = await db.get_nation_link(interaction.user.id) if hasattr(db, "get_nation_link") else None
            if not link:
                # try common helper names
                for fn in ("get_link", "get_user_nation", "get_linked_nation"):
                    if hasattr(db, fn):
                        link = await getattr(db, fn)(interaction.user.id)
                        break
            if isinstance(link, dict) and link.get("nation_id"):
                att_n = await client.get_nation(int(link["nation_id"]))
            elif isinstance(link, int):
                att_n = await client.get_nation(link)
            else:
                att_n = await resolve_nation(self.bot, interaction, str(interaction.user))
        def_n = await resolve_nation(self.bot, interaction, enemy)
        if not att_n or not def_n:
            return await interaction.followup.send(embed=embeds.error("Could not resolve nations"))

        our_map = enemy_map = 6
        our_res = enemy_res = 100
        controls = None
        other_wars = 0
        att_id = int(att_n["id"])
        def_id = int(def_n["id"])

        war = None
        if war_id:
            war = await client.get_war(war_id)
        if not war:
            # try find active war between them
            try:
                wars = await client.get_active_wars(int(att_n.get("alliance_id") or 0)) if att_n.get("alliance_id") else []
            except Exception:
                wars = []
            for w in wars or []:
                a, d = w.get("att_id"), w.get("def_id")
                if {a, d} == {att_id, def_id}:
                    war = w
                    break

        if war:
            if war.get("att_id") == att_id:
                our_map = int(war.get("att_points") or 0)
                enemy_map = int(war.get("def_points") or 0)
                our_res = int(war.get("att_resistance") or 100)
                enemy_res = int(war.get("def_resistance") or 100)
            else:
                our_map = int(war.get("def_points") or 0)
                enemy_map = int(war.get("att_points") or 0)
                our_res = int(war.get("def_resistance") or 100)
                enemy_res = int(war.get("att_resistance") or 100)
            try:
                attacks = await client.get_war_attacks(int(war["id"]))
                controls = infer_controls_from_attacks(attacks, att_id)
            except Exception:
                controls = None

        if controls is None:
            from utils.battle_sim import ControlState
            controls = ControlState()

        # enemy other wars count (defensive slots used)
        try:
            full = await client.get_nation(def_id)
            dw = full.get("defensive_wars") or []
            other_wars = max(0, sum(1 for w in dw if (w.get("turns_left") or 0) > 0) - 1)
        except Exception:
            pass

        tips = advice_for_war(
            our=_force(att_n),
            enemy=_force(def_n),
            our_map=our_map,
            enemy_map=enemy_map,
            our_res=our_res,
            enemy_res=enemy_res,
            controls=controls,
            enemy_other_wars=other_wars,
        )

        air = simulate_air(_force(att_n), _force(def_n))
        gnd = simulate_ground(
            _force(att_n), _force(def_n),
            att_has_air_sup=controls.air_sup_att,
            def_has_air_sup=controls.air_sup_def,
        )
        nav = simulate_naval(_force(att_n), _force(def_n))

        emb = embeds.info(
            f"War advice: {att_n.get('nation_name')} → {def_n.get('nation_name')}",
            "\n".join(f"• {t}" for t in tips),
        )
        emb.add_field(
            name="Quick odds",
            value=(
                f"Air IT \~{air['immense_pct']:.0f}% ({ratio_label(air['att_value'], air['def_value'])})\n"
                f"Ground IT \~{gnd['immense_pct']:.0f}% ({ratio_label(gnd['att_value'], gnd['def_value'])})\n"
                f"Naval IT \~{nav['immense_pct']:.0f}% ({ratio_label(nav['att_value'], nav['def_value'])})"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=emb)

        if ADVISOR_CHANNEL_ID and interaction.channel_id != ADVISOR_CHANNEL_ID:
            ch = self.bot.get_channel(ADVISOR_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(
                        f"**Advice for** {interaction.user.mention}\n",
                        embed=emb,
                    )
                except discord.HTTPException:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(WarAdvisor(bot))
