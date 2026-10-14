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


def _stack_orders(fighters: list[dict], enemy: dict, objective: str | None) -> str:
    """Plain instructional orders — who waits on whom. Sims run off-screen."""
    obj = objective or OBJ_AIR_BLOCKADE_GROUND
    label = OBJ_LABELS.get(obj, obj)
    en_planes = int(enemy.get("aircraft") or 0)
    en_ships = int(enemy.get("ships") or 0)

    stack_as = any(f["controls"].air_sup_att for f in fighters)
    stack_blockade = any(f["controls"].blockade_att for f in fighters)

    need_air = obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_AIR_GROUND) and not stack_as and en_planes > 0
    need_blockade = (
        obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_BLOCKADE_GROUND)
        and not stack_blockade
        and en_ships > 0
    )

    # Best candidates by sim (no numbers shown to members)
    by_air = sorted(fighters, key=lambda f: f["air"]["immense_pct"], reverse=True)
    by_nav = sorted(
        [f for f in fighters if f["ships"] > 0],
        key=lambda f: f["nav"]["immense_pct"],
        reverse=True,
    )
    by_gnd = sorted(fighters, key=lambda f: f["gnd"]["win_pct"], reverse=True)

    lines: list[str] = []
    lines.append(f"**Orders vs {_name(enemy)}** · {label}")
    lines.append("")

    air_lead = None
    nav_lead = None

    # ---- AIR ----
    if obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_AIR_GROUND):
        if stack_as:
            lines.append("Air superiority is already up — nobody dogfights again unless they rebuild planes.")
        elif en_planes <= 0:
            lines.append("Enemy has no planes — skip air.")
        else:
            # pick who should take AS
            ready = [f for f in by_air if f["map"] >= 4]
            if ready:
                air_lead = ready[0]
                lines.append(
                    f"**{air_lead['name']}** — you go first. **Airstrike aircraft** now and take AS."
                )
                hold = [f["name"] for f in fighters if f["id"] != air_lead["id"]]
                if hold:
                    lines.append(
                        f"**{' · '.join(hold)}** — do **not** airstrike until **{air_lead['name']}** has AS."
                    )
            else:
                # soonest to 4 MAP among best air
                air_lead = by_air[0]
                hrs = _hours_until_map(air_lead["map"], 4)
                lines.append(
                    f"**{air_lead['name']}** — bank MAP. In about **{hrs}h** you’ll have 4 MAP — "
                    f"then **airstrike aircraft** for AS."
                )
                others = [f["name"] for f in fighters if f["id"] != air_lead["id"]]
                if others:
                    lines.append(
                        f"**{' · '.join(others)}** — wait for **{air_lead['name']}** to land AS before you spend MAP on air."
                    )

    # ---- BLOCKADE ----
    if obj in (OBJ_AIR_BLOCKADE_GROUND, OBJ_BLOCKADE_GROUND):
        lines.append("")
        if stack_blockade:
            lines.append("Blockade is already up.")
        elif en_ships <= 0:
            lines.append("Enemy has no ships — skip blockade.")
        else:
            nav_cands = by_nav if by_nav else by_air
            nav_ready = [f for f in nav_cands if f["map"] >= 4 and f["ships"] > 0]
            if need_air and air_lead:
                # sequence after air
                if nav_ready and nav_ready[0]["id"] != air_lead["id"]:
                    nav_lead = nav_ready[0]
                    lines.append(
                        f"**{nav_lead['name']}** — **after {air_lead['name']} has AS**, you **naval** for blockade. "
                        f"Do not naval before that."
                    )
                elif nav_ready:
                    nav_lead = nav_ready[0]
                    lines.append(
                        f"**{nav_lead['name']}** — after your AS is secure, **naval** for blockade same war if MAP allows "
                        f"(need 4 MAP left or wait a cycle)."
                    )
                else:
                    nav_lead = nav_cands[0] if nav_cands else None
                    if nav_lead:
                        hrs = _hours_until_map(nav_lead["map"], 4)
                        lines.append(
                            f"**{nav_lead['name']}** — after AS, bank for naval. \~**{hrs}h** to 4 MAP, then **naval** blockade."
                        )
                rest = [
                    f["name"] for f in fighters
                    if (not air_lead or f["id"] != air_lead["id"])
                    and (not nav_lead or f["id"] != nav_lead["id"])
                ]
                if rest and nav_lead:
                    lines.append(
                        f"**{' · '.join(rest)}** — do **not** naval until **{nav_lead['name']}** lands the blockade."
                    )
            else:
                # air not blocking — blockade now
                if nav_ready:
                    nav_lead = nav_ready[0]
                    lines.append(
                        f"**{nav_lead['name']}** — you take blockade. **Naval** now."
                    )
                    rest = [f["name"] for f in fighters if f["id"] != nav_lead["id"]]
                    if rest:
                        lines.append(
                            f"**{' · '.join(rest)}** — no naval until **{nav_lead['name']}** has the blockade."
                        )
                else:
                    nav_lead = by_nav[0] if by_nav else fighters[0]
                    hrs = _hours_until_map(nav_lead["map"], 4)
                    lines.append(
                        f"**{nav_lead['name']}** — need 4 MAP for naval (\~**{hrs}h**). "
                        f"Then you take the blockade; others wait on you."
                    )

    # ---- GROUND ----
    lines.append("")
    if need_air or need_blockade:
        wait_on = []
        if need_air and air_lead:
            wait_on.append(f"{air_lead['name']} (AS)")
        if need_blockade and nav_lead:
            wait_on.append(f"{nav_lead['name']} (blockade)")
        who = " and ".join(wait_on) if wait_on else "air/blockade steps"
        lines.append(f"**Ground** — nobody grounds until {who} is done.")
        for f in fighters:
            hrs3 = _hours_until_map(f["map"], 3)
            if f["map"] >= 3:
                lines.append(f"• **{f['name']}** — hold MAP for now; ground after that.")
            else:
                lines.append(
                    f"• **{f['name']}** — bank MAP (\~**{hrs3}h** to 3). Ground only after {who}."
                )
    else:
        lines.append("**Ground** — strip military. Go in this order (best odds first):")
        for f in by_gnd:
            if f["map"] >= 3:
                lines.append(f"• **{f['name']}** — **ground now**.")
            else:
                hrs = _hours_until_map(f["map"], 3)
                lines.append(
                    f"• **{f['name']}** — wait \~**{hrs}h** for 3 MAP, then ground."
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
