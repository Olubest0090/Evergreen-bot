"""P&W battle simulation — wiki formulas (same core as Locutus simulate)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


class Victory(IntEnum):
    UTTER_FAILURE = 0
    PYRRHIC = 1
    MODERATE = 2
    IMMENSE = 3


VICTORY_NAME = {
    Victory.UTTER_FAILURE: "Utter Failure",
    Victory.PYRRHIC: "Pyrrhic Victory",
    Victory.MODERATE: "Moderate Success",
    Victory.IMMENSE: "Immense Triumph",
}

MAP_COST = {"ground": 3, "air": 4, "naval": 4}


def _roll(value: float) -> float:
    return value * random.uniform(0.40, 1.00)


def _best_of_three(att_v: float, def_v: float) -> Victory:
    wins = 0
    for _ in range(3):
        if _roll(att_v) > _roll(def_v):
            wins += 1
    return Victory(wins)


def expected_victory(att_v: float, def_v: float, trials: int = 400) -> dict:
    """Monte Carlo expected outcome distribution."""
    if att_v <= 0 and def_v <= 0:
        return {"immense_pct": 0, "mod_pct": 0, "pyr_pct": 0, "fail_pct": 100, "avg_victory": 0.0}
    counts = [0, 0, 0, 0]
    for _ in range(trials):
        v = _best_of_three(max(att_v, 0.01), max(def_v, 0.01))
        counts[int(v)] += 1
    total = float(trials)
    avg = sum(i * counts[i] for i in range(4)) / total
    return {
        "immense_pct": counts[3] / total * 100,
        "mod_pct": counts[2] / total * 100,
        "pyr_pct": counts[1] / total * 100,
        "fail_pct": counts[0] / total * 100,
        "win_pct": (counts[1] + counts[2] + counts[3]) / total * 100,
        "avg_victory": avg,
        "likely": Victory(max(range(4), key=lambda i: counts[i])),
    }


@dataclass
class Force:
    soldiers: int = 0
    tanks: int = 0
    aircraft: int = 0
    ships: int = 0
    munitions: bool = True  # arm soldiers if true


def air_value(f: Force) -> float:
    return f.aircraft * 3.0


def naval_value(f: Force) -> float:
    return f.ships * 4.0


def ground_value(f: Force, enemy_has_air_superiority: bool = False) -> float:
    """Enemy air superiority halves tank combat power."""
    tank_mult = 20.0 if enemy_has_air_superiority else 40.0
    if f.munitions:
        return f.soldiers * 1.75 + f.tanks * tank_mult
    return f.soldiers * 1.0 + f.tanks * tank_mult


def simulate_air(att: Force, deff: Force) -> dict:
    av, dv = air_value(att), air_value(deff)
    exp = expected_victory(av, dv)
    # average plane losses (dogfight-ish mid estimates)
    att_loss = max(0, int(dv * 0.015 * 3 * 0.7))
    def_loss = max(0, int(av * 0.012 * 3 * 0.7))
    return {
        "type": "air",
        "att_value": av,
        "def_value": dv,
        "att_planes": att.aircraft,
        "def_planes": deff.aircraft,
        "map_cost": MAP_COST["air"],
        **exp,
        "est_att_plane_loss": att_loss,
        "est_def_plane_loss": def_loss,
        "immense_gives": "Air Superiority (enemy tanks at 50% value)",
    }


def simulate_naval(att: Force, deff: Force) -> dict:
    av, dv = naval_value(att), naval_value(deff)
    exp = expected_victory(av, dv)
    att_loss = max(0, int(dv * 0.01375 * 3 * 0.7))
    def_loss = max(0, int(av * 0.01375 * 3 * 0.7))
    return {
        "type": "naval",
        "att_value": av,
        "def_value": dv,
        "att_ships": att.ships,
        "def_ships": deff.ships,
        "map_cost": MAP_COST["naval"],
        **exp,
        "est_att_ship_loss": att_loss,
        "est_def_ship_loss": def_loss,
        "immense_gives": "Blockade",
    }


def simulate_ground(
    att: Force,
    deff: Force,
    *,
    att_has_air_sup: bool = False,
    def_has_air_sup: bool = False,
) -> dict:
    # air sup against you halves YOUR tanks; against them halves THEIRS
    av = ground_value(att, enemy_has_air_superiority=def_has_air_sup)
    dv = ground_value(deff, enemy_has_air_superiority=att_has_air_sup)
    exp = expected_victory(av, dv)
    return {
        "type": "ground",
        "att_value": av,
        "def_value": dv,
        "att_soldiers": att.soldiers,
        "att_tanks": att.tanks,
        "def_soldiers": deff.soldiers,
        "def_tanks": deff.tanks,
        "map_cost": MAP_COST["ground"],
        "att_air_sup": att_has_air_sup,
        "def_air_sup": def_has_air_sup,
        **exp,
        "immense_gives": "Ground Control (grounds chip enemy aircraft)",
        "note": "Successful ground also chips enemy aircraft if you hold GC",
    }


def ratio_label(att_v: float, def_v: float) -> str:
    if def_v <= 0:
        return "free IT"
    r = att_v / def_v
    if r >= 1.5:
        return "strong IT odds"
    if r >= 1.15:
        return "favored"
    if r >= 0.9:
        return "coin flip"
    if r >= 0.7:
        return "uphill"
    return "bad idea"


@dataclass
class ControlState:
    air_sup_att: bool = False   # attacker has air superiority over defender
    air_sup_def: bool = False
    ground_ctrl_att: bool = False
    ground_ctrl_def: bool = False
    blockade_att: bool = False  # attacker blockades defender
    blockade_def: bool = False


def infer_controls_from_attacks(attacks: list[dict], our_nation_id: int) -> ControlState:
    """Walk attacks in order. our_nation_id = nation we are advising for.

    Success: 0 fail, 1 pyrrhic, 2 moderate, 3 immense (GraphQL).
    Actor = who launched the attack (att_id), fallback victor.
    """
    st = ControlState()
    our_id = int(our_nation_id)
    ordered = sorted(attacks or [], key=lambda a: a.get("date") or "")
    for a in ordered:
        t = (a.get("type") or "").upper().replace(" ", "_")
        try:
            success = int(a.get("success") if a.get("success") is not None else -1)
        except (TypeError, ValueError):
            success = -1
        is_immense = success == 3
        is_win = success in (1, 2, 3)

        actor_raw = a.get("att_id")
        if actor_raw is None:
            actor_raw = a.get("victor")
        try:
            actor = int(actor_raw) if actor_raw is not None else 0
        except (TypeError, ValueError):
            actor = 0
        we = actor == our_id

        is_air = any(x in t for x in ("AIR", "DOGFIGHT", "AIRSTRIKE"))
        is_ground = "GROUND" in t and "NAVAL" not in t
        is_naval = "NAVAL" in t or t == "NAVY"

        if is_air:
            if is_immense and we:
                st.air_sup_att, st.air_sup_def = True, False
            elif is_immense and not we:
                st.air_sup_def, st.air_sup_att = True, False
            elif is_win and we:
                st.air_sup_def = False
            elif is_win and not we:
                st.air_sup_att = False
        elif is_ground:
            if is_immense and we:
                st.ground_ctrl_att, st.ground_ctrl_def = True, False
            elif is_immense and not we:
                st.ground_ctrl_def, st.ground_ctrl_att = True, False
            elif is_win and we:
                st.ground_ctrl_def = False
            elif is_win and not we:
                st.ground_ctrl_att = False
        elif is_naval:
            if is_immense and we:
                st.blockade_att, st.blockade_def = True, False
            elif is_immense and not we:
                st.blockade_def, st.blockade_att = True, False
            elif is_win and we:
                st.blockade_def = False
            elif is_win and not we:
                st.blockade_att = False
    return st


def advice_for_war(
    *,
    our: Force,
    enemy: Force,
    our_map: int,
    enemy_map: int,
    our_res: int,
    enemy_res: int,
    controls: ControlState,
    enemy_other_wars: int = 0,
) -> list[str]:
    tips: list[str] = []
    air = simulate_air(our, enemy)
    nav = simulate_naval(our, enemy)
    gnd = simulate_ground(
        our, enemy,
        att_has_air_sup=controls.air_sup_att,
        def_has_air_sup=controls.air_sup_def,
    )

    tips.append(f"MAP {our_map}/12 · enemy {enemy_map}/12 · Res {our_res} · enemy {enemy_res}")

    bits = []
    if controls.air_sup_att:
        bits.append("you hold air superiority")
    if controls.air_sup_def:
        bits.append("enemy holds air superiority")
    if controls.ground_ctrl_att:
        bits.append("you hold ground control")
    if controls.ground_ctrl_def:
        bits.append("enemy holds ground control")
    if controls.blockade_att:
        bits.append("you blockade them")
    if controls.blockade_def:
        bits.append("they blockade you")
    tips.append("Status: " + (", ".join(bits) if bits else "no space control yet"))

    tips.append(
        f"Enemy: {enemy.soldiers:,}S · {enemy.tanks:,}T · {enemy.aircraft:,} planes · {enemy.ships:,} ships"
    )
    tips.append(
        f"You: {our.soldiers:,}S · {our.tanks:,}T · {our.aircraft:,} planes · {our.ships:,} ships"
    )

    if controls.air_sup_def:
        tips.append(
            f"**Break their air superiority** (any airstrike win). "
            f"{our.aircraft:,} vs {enemy.aircraft:,} planes · "
            f"\~{air['win_pct']:.0f}% any win / \~{air['immense_pct']:.0f}% IT · 4 MAP"
        )
    elif not controls.air_sup_att and enemy.aircraft > 0:
        tips.append(
            f"**Take air superiority** (airstrike IT). "
            f"{our.aircraft:,} vs {enemy.aircraft:,} · "
            f"\~{air['immense_pct']:.0f}% IT · 4 MAP"
        )
    elif controls.air_sup_att:
        tips.append("You hold air superiority — their tanks fight at half. Prefer ground.")
    else:
        tips.append("No enemy planes — skip air.")

    if controls.blockade_def:
        tips.append(
            "**Break blockade:** any naval win vs them, zero their ships, "
            "or a partner at war with them lands naval IT."
        )
    if not controls.blockade_att and enemy.ships > 0:
        tips.append(
            f"**Blockade** needs naval IT. {our.ships:,} vs {enemy.ships:,} ships · "
            f"\~{nav['win_pct']:.0f}% any win / \~{nav['immense_pct']:.0f}% IT · 4 MAP"
        )
    elif controls.blockade_att:
        tips.append("You blockade them.")
    elif enemy.ships <= 0:
        tips.append("Enemy has no ships.")

    tips.append(
        f"**Ground** {our.soldiers:,}S/{our.tanks:,}T vs {enemy.soldiers:,}S/{enemy.tanks:,}T · "
        f"\~{gnd['win_pct']:.0f}% any win / \~{gnd['immense_pct']:.0f}% IT · 3 MAP"
        + (" · you have GC (grounds chip planes)" if controls.ground_ctrl_att else "")
        + (" · enemy has GC" if controls.ground_ctrl_def else "")
    )

    if our_map < 3:
        tips.append(f"MAP {our_map} — need 3 for ground, 4 for air/naval. Wait a turn.")
    elif our_map < 4 and not controls.air_sup_att and enemy.aircraft > 0:
        tips.append(f"MAP {our_map} — enough for ground, not air yet.")
    else:
        tips.append(f"MAP {our_map} — you can attack this cycle.")

    return tips
