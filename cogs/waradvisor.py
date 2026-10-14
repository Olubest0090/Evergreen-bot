"""War advisor — live MAP/resistance/controls from the actual war."""

from __future__ import annotations

import os
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds
from utils.battle_sim import (
    Force,
    ControlState,
    advice_for_war,
    infer_controls_from_attacks,
    simulate_air,
    simulate_ground,
    simulate_naval,
    ratio_label,
)
from cogs.link import resolve_nation

log = logging.getLogger("evergreen-ai")
ADVISOR_CHANNEL_ID = int(os.environ.get("WAR_ADVISOR_CHANNEL_ID", "0") or 0)


def _force(n: dict) -> Force:
    return Force(
        soldiers=int(n.get("soldiers") or 0),
        tanks=int(n.get("tanks") or 0),
        aircraft=int(n.get("aircraft") or 0),
        ships=int(n.get("ships") or 0),
        munitions=True,
    )


def _find_war_between(att: dict, deff: dict) -> dict | None:
    """Find active war id between two nations from either side's war lists."""
    att_id = int(att["id"])
    def_id = int(deff["id"])
    for w in (att.get("offensive_wars") or []) + (att.get("defensive_wars") or []):
        if (w.get("turns_left") or 0) <= 0:
            continue
        pair = {int(w.get("att_id") or 0), int(w.get("def_id") or 0)}
        if pair == {att_id, def_id}:
            return w
    for w in (deff.get("offensive_wars") or []) + (deff.get("defensive_wars") or []):
        if (w.get("turns_left") or 0) <= 0:
            continue
        pair = {int(w.get("att_id") or 0), int(w.get("def_id") or 0)}
        if pair == {att_id, def_id}:
            return w
    return None


class WarAdvisor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="waradvice", description="Live MAP / control advice for a target")
    @app_commands.describe(
        enemy="Enemy nation (optional in a war room — auto-detects)",
        attacker="Your nation (optional — uses /link)",
        war_id="War id (optional — auto-finds if you and enemy are at war)",
    )
    async def waradvice(
        self,
        interaction: discord.Interaction,
        enemy: str | None = None,
        attacker: str | None = None,
        war_id: int | None = None,
    ):
        await interaction.response.defer(thinking=True)
        client = self.bot.pw_client
        try:
            # --- resolve enemy: war room channel first ---
            room = await database.get_war_room_by_channel(interaction.channel_id)
            enemy_id = None
            if room and room.get("enemy_nation_id"):
                enemy_id = int(room["enemy_nation_id"])

            if enemy:
                def_n = await resolve_nation(client, enemy)
            elif enemy_id:
                def_n = await client.get_nation(enemy_id)
            else:
                return await interaction.followup.send(
                    embed=embeds.error(
                        "Need a target",
                        "Run this in a war room, or pass enemy: name/id",
                    )
                )
            if not def_n:
                return await interaction.followup.send(
                    embed=embeds.error("Enemy not found")
                )

            # --- resolve attacker ---
            if attacker:
                att_n = await resolve_nation(client, attacker)
            else:
                nation_id = await database.get_nation_for_user(interaction.user.id)
                if not nation_id:
                    return await interaction.followup.send(
                        embed=embeds.error(
                            "Link your nation",
                            "Use /link first, or pass attacker:",
                        )
                    )
                att_n = await client.get_nation(int(nation_id))
            if not att_n:
                return await interaction.followup.send(
                    embed=embeds.error("Attacker nation not found")
                )

            # Refresh full nations (wars lists + live mil)
            att_n = await client.get_nation(int(att_n["id"])) or att_n
            def_n = await client.get_nation(int(def_n["id"])) or def_n

            att_id = int(att_n["id"])
            def_id = int(def_n["id"])

            our_map, enemy_map = 6, 6
            our_res, enemy_res = 100, 100
            controls = ControlState()

            # --- live war ---
            war = None
            if war_id:
                war = await client.get_war(int(war_id))
            if not war:
                brief = _find_war_between(att_n, def_n)
                if brief and brief.get("id"):
                    war = await client.get_war(int(brief["id"]))

            if war:
                if int(war.get("att_id") or 0) == att_id:
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
                    controls = infer_controls_from_attacks(attacks or [], att_id)
                except Exception as e:
                    log.warning("war attacks failed: %s", e)

            tips = advice_for_war(
                our=_force(att_n),
                enemy=_force(def_n),
                our_map=our_map,
                enemy_map=enemy_map,
                our_res=our_res,
                enemy_res=enemy_res,
                controls=controls,
            )

            air = simulate_air(_force(att_n), _force(def_n))
            gnd = simulate_ground(
                _force(att_n),
                _force(def_n),
                att_has_air_sup=controls.air_sup_att,
                def_has_air_sup=controls.air_sup_def,
            )
            nav = simulate_naval(_force(att_n), _force(def_n))

            war_note = f" · war `{war['id']}`" if war else ""
            emb = embeds.info(
                f"{att_n.get('nation_name')} → {def_n.get('nation_name')}{war_note}",
                "
".join(tips),
            )
            await interaction.followup.send(embed=emb)

            if ADVISOR_CHANNEL_ID and interaction.channel_id != ADVISOR_CHANNEL_ID:
                ch = self.bot.get_channel(ADVISOR_CHANNEL_ID)
                if ch:
                    try:
                        await ch.send(content=f"{interaction.user.mention}", embed=emb)
                    except discord.HTTPException:
                        pass
        except Exception as e:
            log.exception("waradvice failed")
            err = str(e)
            if "rate limit" in err.lower() or "rate limited" in err.lower():
                err = (
                    "Politics & War API is rate-limiting us right now. "
                    "Wait \~30–60s and run /waradvice again."
                )
            await interaction.followup.send(
                embed=embeds.error("War advice failed", err[:400])
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(WarAdvisor(bot))
