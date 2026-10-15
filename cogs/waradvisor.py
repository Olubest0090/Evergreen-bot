"""Stack MILCOM orders + war-room objectives."""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

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

# Preset objective keys
OBJ_AIR_BLOCKADE_GROUND = "air_blockade_ground"
OBJ_AIR_GROUND = "air_ground"
OBJ_BLOCKADE_GROUND = "blockade_ground"
OBJ_GROUND = "ground_focus"

OBJ_LABELS = {
    OBJ_AIR_BLOCKADE_GROUND: "Air superiority → blockade → ground",
    OBJ_AIR_GROUND: "Air superiority → ground",
    OBJ_BLOCKADE_GROUND: "Blockade → ground",
    OBJ_GROUND: "Ground focus",
}


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


def _inactive_days(n: dict) -> float:
    la = n.get("last_active")
    if not la:
        return 999
    try:
        last = datetime.fromisoformat(str(la).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).total_seconds() / 86400
    except Exception:
        return 0


async def _load_fighter(client, nation: dict, enemy: dict) -> dict:
    nid = int(nation["id"])
    eid = int(enemy["id"])
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

    our_map, enemy_map = 0, 0
    our_res, enemy_res = 100, 100
    controls = ControlState()
    war_id = None

    if war:
        war_id = war.get("id")
        if int(war.get("att_id") or 0) == nid:
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
            controls = infer_controls_from_attacks(attacks or [], nid)
        except Exception as e:
            log.warning("attacks: %s", e)

    our_f = _force(full)
    en_f = _force(enemy)
    return {
        "nation": full,
        "name": _name(full),
        "id": nid,
        "war_id": war_id,
        "map": our_map,
        "controls": controls,
        "air": simulate_air(our_f, en_f),
        "gnd": simulate_ground(
            our_f, en_f,
            att_has_air_sup=controls.air_sup_att,
            def_has_air_sup=controls.air_sup_def,
        ),
        "nav": simulate_naval(our_f, en_f),
        "planes": our_f.aircraft,
        "ships": our_f.ships,
        "soldiers": our_f.soldiers,
        "tanks": our_f.tanks,
    }



def _hours_until_map(current: int, need: int) -> int:
    """MAP +1 every 2 hours."""
    missing = max(0, need - int(current or 0))
    return missing * 2


def _planes_for_parity(enemy_planes: int) -> int:
    """Planes needed for a fair-ish dogfight vs enemy air."""
    return max(0, int(enemy_planes * 1.05))


def _ships_for_parity(enemy_ships: int) -> int:
    return max(0, int(enemy_ships * 1.05))


def _stack_orders(fighters: list[dict], enemy: dict, objective: str | None) -> str:
    """Odds-aware orders. Never green-light a near-UT fight."""
    obj = objective or OBJ_AIR_BLOCKADE_GROUND
    label = OBJ_LABELS.get(obj, obj)
    en_planes = int(enemy.get("aircraft") or 0)
    en_ships = int(enemy.get("ships") or 0)
    en_soldiers = int(enemy.get("soldiers") or 0)
    en_tanks = int(enemy.get("tanks") or 0)
    solo = len(fighters) == 1

    stack_as = any(f["controls"].air_sup_att for f in fighters)
    stack_blockade = any(f["controls"].blockade_att for f in fighters)

    need_air = obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_AIR_GROUND) and not stack_as and en_planes > 0
    need_blockade = (
        obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_BLOCKADE_GROUND)
        and not stack_blockade
        and en_ships > 0
    )

    by_air = sorted(fighters, key=lambda f: f["air"]["immense_pct"], reverse=True)
    by_nav = sorted(
        [f for f in fighters if f["ships"] > 0],
        key=lambda f: f["nav"]["immense_pct"],
        reverse=True,
    )
    by_gnd = sorted(fighters, key=lambda f: f["gnd"]["win_pct"], reverse=True)

    MIN_AIR_IT, MIN_AIR_WIN = 25.0, 40.0
    MIN_NAV_IT, MIN_NAV_WIN = 25.0, 40.0
    MIN_GND_WIN = 45.0

    lines: list[str] = []
    lines.append(f"**Orders vs {_name(enemy)}** · {label}")
    if solo:
        lines.append(f"_Solo — {fighters[0]['name']} only. No stack timing._")
    lines.append("")

    # ---- AIR ----
    if obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_AIR_GROUND):
        if stack_as:
            lines.append("**Air:** AS held — do not dogfight again unless they rebuild planes.")
        elif en_planes <= 0:
            lines.append("**Air:** enemy 0 planes — skip.")
        else:
            best = by_air[0]
            it = float(best["air"]["immense_pct"])
            win = float(best["air"]["win_pct"])
            fail = float(best["air"].get("fail_pct", 100 - win))
            our_p = int(best["planes"])
            need_p = _planes_for_parity(en_planes)
            can = (it >= MIN_AIR_IT) or (win >= MIN_AIR_WIN and it >= 15)
            map_ok = int(best["map"]) >= 4

            if can and map_ok:
                if solo:
                    lines.append(
                        f"**{best['name']}** — **airstrike aircraft** "
                        f"(\~{it:.0f}% IT / \~{win:.0f}% any win vs {en_planes} planes)."
                    )
                else:
                    lines.append(
                        f"**{best['name']}** — take AS. **Airstrike aircraft** "
                        f"(\~{it:.0f}% IT / \~{win:.0f}% win)."
                    )
                    others = [f["name"] for f in fighters if f["id"] != best["id"]]
                    if others:
                        lines.append(
                            f"**{' · '.join(others)}** — no air until **{best['name']}** has AS."
                        )
            elif can and not map_ok:
                hrs = _hours_until_map(best["map"], 4)
                lines.append(
                    f"**{best['name']}** — air odds OK (\~{it:.0f}% IT) but MAP {best['map']}. "
                    f"Bank \~**{hrs}h** → 4 MAP → **airstrike aircraft**."
                )
            else:
                gap = max(0, need_p - our_p)
                lines.append(
                    f"**{best['name']}** — **do NOT airstrike.** "
                    f"**{our_p}** planes vs **{en_planes}** → \~{it:.0f}% IT / \~{fail:.0f}% UT. That is a throw."
                )
                lines.append(
                    f"**Rebuild:** buy toward **\~{need_p} planes** (\~**{gap}** more). "
                    f"If you are not on **MMR 5/5/5/3**, switch so hangars are maxed, rebuild, then `/waradvice` again."
                )

    # ---- BLOCKADE ----
    if obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_BLOCKADE_GROUND):
        lines.append("")
        if stack_blockade:
            lines.append("**Blockade:** already up.")
        elif en_ships <= 0:
            lines.append("**Blockade:** enemy 0 ships — skip.")
        else:
            cands = by_nav if by_nav else by_air
            best = cands[0] if cands else fighters[0]
            it = float(best["nav"]["immense_pct"])
            win = float(best["nav"]["win_pct"])
            our_s = int(best["ships"])
            need_s = _ships_for_parity(en_ships)
            can = (it >= MIN_NAV_IT) or (win >= MIN_NAV_WIN and it >= 15)
            map_ok = int(best["map"]) >= 4 and our_s > 0

            if need_air and not stack_as:
                lines.append("**Blockade:** only after AS is real — do not naval first.")
            elif can and map_ok:
                if solo:
                    lines.append(
                        f"**{best['name']}** — **naval** for blockade "
                        f"(\~{it:.0f}% IT / \~{win:.0f}% win vs {en_ships} ships)."
                    )
                else:
                    lines.append(
                        f"**{best['name']}** — take blockade. **Naval** (\~{it:.0f}% IT)."
                    )
                    rest = [f["name"] for f in fighters if f["id"] != best["id"]]
                    if rest:
                        lines.append(
                            f"**{' · '.join(rest)}** — no naval until **{best['name']}** has it."
                        )
            elif can and not map_ok:
                hrs = _hours_until_map(best["map"], 4)
                lines.append(
                    f"**{best['name']}** — naval odds OK (\~{it:.0f}% IT) but need 4 MAP (\~**{hrs}h**)."
                )
            else:
                gap = max(0, need_s - our_s)
                lines.append(
                    f"**{best['name']}** — **do NOT naval.** "
                    f"**{our_s}** ships vs **{en_ships}** (\~{it:.0f}% IT). "
                    f"Rebuild toward **\~{need_s}** ships (\~{gap} more). Drydocks on **MMR 5/5/5/3**."
                )

    # ---- GROUND ----
    lines.append("")
    if need_air or need_blockade:
        bits = []
        if need_air:
            bits.append("AS")
        if need_blockade:
            bits.append("blockade")
        lines.append(f"**Ground:** locked until {' + '.join(bits)} — and only if ground odds are real.")
        for f in fighters:
            hrs = _hours_until_map(f["map"], 3)
            gw = float(f["gnd"]["win_pct"])
            if int(f["map"]) >= 3:
                lines.append(f"• **{f['name']}** — hold (ground \~{gw:.0f}% win even later).")
            else:
                lines.append(f"• **{f['name']}** — bank MAP (\~**{hrs}h** to 3).")
    else:
        best_g = by_gnd[0]
        g_win = float(best_g["gnd"]["win_pct"])
        if g_win < MIN_GND_WIN:
            lines.append(
                f"**Ground:** **do NOT ground.** Best \~{g_win:.0f}% win "
                f"({best_g['name']}: {int(best_g['soldiers']):,}S/{int(best_g['tanks']):,}T vs "
                f"{en_soldiers:,}S/{en_tanks:,}T). Rebuild barracks/factories (**MMR 5/5/5/3**), then `/waradvice`."
            )
        else:
            lines.append("**Ground:** strip mil —")
            for f in by_gnd:
                gw = float(f["gnd"]["win_pct"])
                if int(f["map"]) >= 3 and gw >= MIN_GND_WIN:
                    lines.append(f"• **{f['name']}** — **ground now** (\~{gw:.0f}% win).")
                elif int(f["map"]) >= 3:
                    lines.append(f"• **{f['name']}** — hold/rebuild (\~{gw:.0f}% too low).")
                else:
                    hrs = _hours_until_map(f["map"], 3)
                    lines.append(f"• **{f['name']}** — \~**{hrs}h** to 3 MAP, re-check odds.")

    lines.append("")
    lines.append(
        f"Enemy **{_name(enemy)}** — {en_soldiers:,}S · {en_tanks:,}T · "
        f"{en_planes} planes · {en_ships} ships"
    )
    return "\n".join(lines)





class ObjectiveView(discord.ui.View):
    def __init__(self, guild_id: int, enemy_nation_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.enemy_nation_id = enemy_nation_id

    async def _set(self, interaction: discord.Interaction, key: str):
        await database.set_war_room_objective(self.guild_id, self.enemy_nation_id, key)
        await interaction.response.send_message(
            f"Objective set: **{OBJ_LABELS.get(key, key)}**\nRun `/waradvice` for stack orders.",
            ephemeral=False,
        )

    @discord.ui.button(label="Air → Blockade → Ground", style=discord.ButtonStyle.primary, custom_id="obj_abg")
    async def abg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, OBJ_AIR_BLOCKADE_GROUND)

    @discord.ui.button(label="Air → Ground", style=discord.ButtonStyle.secondary, custom_id="obj_ag")
    async def ag(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, OBJ_AIR_GROUND)

    @discord.ui.button(label="Blockade → Ground", style=discord.ButtonStyle.secondary, custom_id="obj_bg")
    async def bg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, OBJ_BLOCKADE_GROUND)

    @discord.ui.button(label="Ground focus", style=discord.ButtonStyle.secondary, custom_id="obj_g")
    async def g(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, OBJ_GROUND)


class WarAdvisor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # persistent buttons
        self.bot.add_view(ObjectiveView(0, 0))  # placeholder; real ids in custom handlers via room

    @app_commands.command(name="waradvice", description="Stack MILCOM orders for this war room")
    @app_commands.describe(enemy="Enemy (optional in war room)")
    async def waradvice(self, interaction: discord.Interaction, enemy: str | None = None):
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

            # Skip 3d+ inactive enemies for aggressive plans note
            inactive = _inactive_days(def_n) >= 3

            our_list = await get_our_nations_for_enemy(self.bot, interaction.guild_id, eid)
            if not our_list:
                nid = await database.get_nation_for_user(interaction.user.id)
                if nid:
                    one = await client.get_nation(int(nid))
                    if one:
                        our_list = [one]
            if not our_list:
                return await interaction.followup.send(
                    embed=embeds.error("No stack", "No Evergreen wars on this target.")
                )

            fighters = []
            for n in our_list:
                try:
                    fighters.append(await _load_fighter(client, n, def_n))
                except Exception as e:
                    log.warning("fighter: %s", e)

            if not fighters:
                return await interaction.followup.send(embed=embeds.error("Could not load stack"))

            objective = None
            if room:
                objective = room.get("objective")
            if not objective:
                objective = OBJ_AIR_BLOCKADE_GROUND

            lines = _stack_orders(fighters, def_n, objective)
            if inactive:
                lines.insert(1, "_Enemy inactive 3d+ — still following objective, loot focus OK._")

            body = lines if isinstance(lines, str) else "\n".join(lines)
            if len(body) > 1900:
                body = body[:1900] + "\n…"
            await interaction.followup.send(body)
        except Exception as e:
            log.exception("waradvice")
            err = str(e)
            if "rate limit" in err.lower():
                err = "PnW rate limit — wait 30–60s and try again."
            await interaction.followup.send(embed=embeds.error("War advice failed", err[:400]))

    @app_commands.command(name="warobjective", description="Set or ask MILCOM objective for this war room")
    @app_commands.describe(
        action="ask = post buttons for MILCOM · show = current objective",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="ask MILCOM (buttons)", value="ask"),
            app_commands.Choice(name="show current", value="show"),
        ]
    )
    async def warobjective(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        room = await database.get_war_room_by_channel(interaction.channel_id)
        if not room:
            return await interaction.response.send_message(
                "Use this in a war room channel.", ephemeral=True
            )
        eid = int(room["enemy_nation_id"])
        if action.value == "show":
            obj = room.get("objective") or "(not set — default Air → Blockade → Ground)"
            label = OBJ_LABELS.get(obj, obj)
            return await interaction.response.send_message(f"Current objective: **{label}**")

        view = ObjectiveView(interaction.guild_id, eid)
        # re-bind custom ids unique per room so buttons work
        for child in view.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                child.custom_id = f"{child.custom_id}:{interaction.guild_id}:{eid}"

        await interaction.response.send_message(
            "**MILCOM — what is the objective for this war?**\n"
            "Pick one (applies to `/waradvice` for this room):",
            view=view,
        )

    @app_commands.command(
        name="warobjective_ask_all",
        description="Post objective question in every war room missing one",
    )
    async def warobjective_ask_all(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rooms = await database.get_all_war_rooms(interaction.guild_id)
        posted = 0
        for room in rooms or []:
            if room.get("objective"):
                continue
            ch = self.bot.get_channel(int(room["channel_id"]))
            if not ch:
                continue
            eid = int(room["enemy_nation_id"])
            view = ObjectiveView(interaction.guild_id, eid)
            for child in view.children:
                if isinstance(child, discord.ui.Button) and child.custom_id:
                    child.custom_id = f"{child.custom_id}:{interaction.guild_id}:{eid}"
            try:
                await ch.send(
                    "**MILCOM — what is the objective for this war?**\n"
                    "Active target (not a 3-day inactive farm unless you say so).\n"
                    "Pick one:",
                    view=view,
                )
                posted += 1
            except discord.HTTPException:
                pass
        await interaction.followup.send(f"Posted objective prompt in **{posted}** war room(s).", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WarAdvisor(bot))
