"""War advisor — stack-wide MILCOM orders for everyone on the target."""

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
    infer_controls_from_attacks,
    simulate_air,
    simulate_ground,
    simulate_naval,
)
from cogs.link import resolve_nation
from cogs.warroom import get_our_nations_for_enemy

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


def _name(n: dict) -> str:
    return n.get("nation_name") or n.get("name") or f"id:{n.get('id')}"


async def _load_fighter(client, nation: dict, enemy: dict) -> dict:
    """Live war row for one of our members vs enemy."""
    nid = int(nation["id"])
    eid = int(enemy["id"])
    # refresh nation for full wars list if thin
    full = nation
    if not (nation.get("offensive_wars") or nation.get("defensive_wars")):
        try:
            full = await client.get_nation(nid) or nation
        except Exception:
            full = nation

    war = None
    for w in (full.get("offensive_wars") or []) + (full.get("defensive_wars") or []):
        if (w.get("turns_left") or 0) <= 0:
            continue
        if {int(w.get("att_id") or 0), int(w.get("def_id") or 0)} == {nid, eid}:
            try:
                war = await client.get_war(int(w["id"]))
            except Exception:
                war = w
            break

    our_map, enemy_map = 6, 6
    our_res, enemy_res = 100, 100
    controls = ControlState()
    war_id = None

    if war:
        war_id = war.get("id")
        if int(war.get("att_id") or 0) == nid:
            our_map = int(war.get("att_points") or war.get("points") or 0)
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
            controls = infer_controls_from_attacks(attacks or [], nid)
        except Exception as e:
            log.warning("attacks %s: %s", war_id, e)

    our_f = _force(full)
    en_f = _force(enemy)
    air = simulate_air(our_f, en_f)
    gnd = simulate_ground(
        our_f, en_f,
        att_has_air_sup=controls.air_sup_att,
        def_has_air_sup=controls.air_sup_def,
    )
    nav = simulate_naval(our_f, en_f)

    return {
        "nation": full,
        "name": _name(full),
        "id": nid,
        "war_id": war_id,
        "map": our_map,
        "enemy_map": enemy_map,
        "res": our_res,
        "enemy_res": enemy_res,
        "controls": controls,
        "air": air,
        "gnd": gnd,
        "nav": nav,
        "planes": our_f.aircraft,
        "ships": our_f.ships,
        "soldiers": our_f.soldiers,
        "tanks": our_f.tanks,
    }


def _stack_orders(fighters: list[dict], enemy: dict) -> list[str]:
    """MILCOM plan: who goes first so the whole stack wins."""
    lines: list[str] = []
    en_planes = int(enemy.get("aircraft") or 0)
    en_ships = int(enemy.get("ships") or 0)

    someone_has_as = any(f["controls"].air_sup_att for f in fighters)
    someone_blockades = any(f["controls"].blockade_att for f in fighters)
    enemy_has_as_on_any = any(f["controls"].air_sup_def for f in fighters)

    # Best air candidate for IT (MAP >= 4, highest IT%)
    air_ready = [f for f in fighters if f["map"] >= 4]
    air_ready.sort(key=lambda f: f["air"]["immense_pct"], reverse=True)
    best_air = air_ready[0] if air_ready else None

    ground_ready = [f for f in fighters if f["map"] >= 3]
    ground_ready.sort(key=lambda f: f["gnd"]["win_pct"], reverse=True)

    naval_ready = [f for f in fighters if f["map"] >= 4 and f["ships"] > 0]
    naval_ready.sort(key=lambda f: f["nav"]["immense_pct"], reverse=True)

    # --- STACK ORDER ---
    lines.append("**STACK PLAN**")

    if someone_has_as:
        lines.append("Air superiority already held — do **not** all dogfight.")
        step = 1
    elif en_planes <= 0:
        lines.append("Enemy has no planes — skip air.")
        step = 1
    else:
        if best_air and best_air["air"]["immense_pct"] >= 30:
            lines.append(
                f"**{step if False else 1}. {best_air['name']}** — "
                f"**airstrike aircraft** first (\~{best_air['air']['immense_pct']:.0f}% IT, "
                f"MAP {best_air['map']}). Everyone else waits for AS."
            )
        elif best_air:
            lines.append(
                f"**1. {best_air['name']}** — best air we have "
                f"(\~{best_air['air']['immense_pct']:.0f}% IT) — still try **airstrike aircraft** "
                f"if you can; others hold."
            )
        else:
            lines.append("**1.** Nobody has 4 MAP for air — **WAIT** a turn, then strongest planes dogfight.")
        step = 2

    # Blockade
    if someone_blockades:
        lines.append(f"**{step if someone_has_as or en_planes<=0 else 2}.** Blockade already up.")
    elif en_ships > 0 and naval_ready:
        nav = naval_ready[0]
        # only push naval IT if AS handled or not needed
        if someone_has_as or en_planes <= 0 or (best_air and best_air["id"] != nav["id"]):
            nstep = 2 if (someone_has_as or en_planes <= 0) else 2
            lines.append(
                f"**{nstep}. {nav['name']}** — **naval** for blockade "
                f"(\~{nav['nav']['immense_pct']:.0f}% IT, MAP {nav['map']}). "
                f"Frees partners if enemy was blockading."
            )
    elif en_ships > 0:
        lines.append("**Naval:** need 4 MAP + ships — bank turns or rebuild ships.")

    # Ground for the rest
    lines.append("**Ground (strip mil):**")
    if not ground_ready:
        lines.append("• Nobody has 3 MAP yet — wait.")
    else:
        for f in ground_ready:
            if not someone_has_as and en_planes > 0 and best_air and f["id"] == best_air["id"]:
                # this person is on air duty
                if f["map"] >= 7:
                    lines.append(
                        f"• **{f['name']}** — after your air IT, **ground** same cycle if MAP left"
                    )
                else:
                    lines.append(f"• **{f['name']}** — air first; ground next cycle")
                continue
            tag = "YES ground" if f["map"] >= 3 else "WAIT"
            extra = ""
            if f["controls"].air_sup_att:
                extra = " (you have AS)"
            elif someone_has_as:
                extra = " (stack has AS — tanks weak on them)"
            lines.append(
                f"• **{f['name']}** — **{tag}** · MAP {f['map']} · "
                f"\~{f['gnd']['win_pct']:.0f}% any win{extra}"
            )

    # Per-member one-liners
    lines.append("")
    lines.append("**Per member**")
    for f in fighters:
        bits = []
        if f["controls"].air_sup_att:
            bits.append("AS")
        if f["controls"].ground_ctrl_att:
            bits.append("GC")
        if f["controls"].blockade_att:
            bits.append("blockade")
        if f["controls"].air_sup_def:
            bits.append("enemy AS on you")
        ctrl = f" [{', '.join(bits)}]" if bits else ""
        lines.append(
            f"• **{f['name']}** MAP {f['map']}/12 · "
            f"{f['planes']}p/{f['ships']}s/{f['tanks']}t{ctrl}"
        )

    lines.append("")
    lines.append(
        f"Enemy **{_name(enemy)}**: {int(enemy.get('soldiers') or 0):,}S · "
        f"{int(enemy.get('tanks') or 0):,}T · {en_planes} planes · {en_ships} ships"
    )
    return lines


class WarAdvisor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="waradvice",
        description="Stack MILCOM orders for everyone fighting this target",
    )
    @app_commands.describe(
        enemy="Enemy (optional in war room)",
        war_id="Optional single war id",
    )
    async def waradvice(
        self,
        interaction: discord.Interaction,
        enemy: str | None = None,
        war_id: int | None = None,
    ):
        await interaction.response.defer(thinking=True)
        client = self.bot.pw_client
        try:
            room = await database.get_war_room_by_channel(interaction.channel_id)
            enemy_id = int(room["enemy_nation_id"]) if room and room.get("enemy_nation_id") else None

            if enemy:
                def_n = await resolve_nation(client, enemy)
            elif enemy_id:
                def_n = await client.get_nation(enemy_id)
            else:
                return await interaction.followup.send(
                    embed=embeds.error("Need a target", "Use a war room or pass enemy:")
                )
            if not def_n:
                return await interaction.followup.send(embed=embeds.error("Enemy not found"))

            def_n = await client.get_nation(int(def_n["id"])) or def_n
            eid = int(def_n["id"])

            # All Evergreen on this target
            our_list = await get_our_nations_for_enemy(
                self.bot, interaction.guild_id, eid
            )
            if not our_list:
                # fallback: only the linked runner
                nid = await database.get_nation_for_user(interaction.user.id)
                if nid:
                    one = await client.get_nation(int(nid))
                    if one:
                        our_list = [one]
            if not our_list:
                return await interaction.followup.send(
                    embed=embeds.error(
                        "No stack found",
                        "No Evergreen wars on this enemy (and you are not /link'd).",
                    )
                )

            fighters = []
            for n in our_list:
                try:
                    fighters.append(await _load_fighter(client, n, def_n))
                except Exception as e:
                    log.warning("fighter %s: %s", n.get("id"), e)

            if not fighters:
                return await interaction.followup.send(
                    embed=embeds.error("Could not load stack wars")
                )

            lines = _stack_orders(fighters, def_n)
            emb = embeds.info(
                f"MILCOM · {_name(def_n)} · {len(fighters)} on target",
                "\n".join(lines),
            )
            if len(emb.description or "") > 4000:
                emb.description = (emb.description or "")[:3900] + "\n…"
            await interaction.followup.send(embed=emb)

            if ADVISOR_CHANNEL_ID and interaction.channel_id != ADVISOR_CHANNEL_ID:
                ch = self.bot.get_channel(ADVISOR_CHANNEL_ID)
                if ch:
                    try:
                        await ch.send(embed=emb)
                    except discord.HTTPException:
                        pass
        except Exception as e:
            log.exception("waradvice failed")
            err = str(e)
            if "rate limit" in err.lower():
                err = "PnW API rate limit — wait \~30–60s and run /waradvice again."
            await interaction.followup.send(embed=embeds.error("War advice failed", err[:400]))


async def setup(bot: commands.Bot):
    await bot.add_cog(WarAdvisor(bot))
