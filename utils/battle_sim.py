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


def infer_controls_from_attacks(attacks: list[dict], att_nation_id: int) -> ControlState:
    """Walk attacks chronologically; Immense air/ground/naval sets control."""
    st = ControlState()
    ordered = sorted(attacks or [], key=lambda a: a.get("date") or "")
    for a in ordered:
        t = (a.get("type") or "").upper()
        success = a.get("success")
        # success: 3 = immense in many API dumps; also check string
        is_immense = success == 3 or str(success).upper() in ("3", "IMMENSE", "IMMENSE_TRIUMPH")
        is_win = is_immense or success in (1, 2, 3) or str(success) in ("1", "2", "3")
        actor = a.get("att_id") or a.get("victor")
        we_attacked = actor == att_nation_id

        if "AIR" in t or "DOGFIGHT" in t or "AIRSTRIKE" in t:
            if is_immense and we_attacked:
                st.air_sup_att = True
                st.air_sup_def = False
            elif is_immense and not we_attacked:
                st.air_sup_def = True
                st.air_sup_att = False
            elif is_win and we_attacked:
                st.air_sup_def = False  # break their AS
            elif is_win and not we_attacked:
                st.air_sup_att = False
        elif t == "GROUND" or "GROUND" in t:
            if is_immense and we_attacked:
                st.ground_ctrl_att = True
                st.ground_ctrl_def = False
            elif is_immense and not we_attacked:
                st.ground_ctrl_def = True
                st.ground_ctrl_att = False
            elif is_win and we_attacked:
                st.ground_ctrl_def = False
            elif is_win and not we_attacked:
                st.ground_ctrl_att = False
        elif "NAVAL" in t:
            if is_immense and we_attacked:
                st.blockade_att = True
                st.blockade_def = False
            elif is_immense and not we_attacked:
                st.blockade_def = True
                st.blockade_att = False
            elif is_win and we_attacked:
                st.blockade_def = False
            elif is_win and not we_attacked:
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

    tips.append(f"MAP {our_map}/12 · enemy {enemy_map}/12 · Res {our_res}% · enemy {enemy_res}%")

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
        f"Enemy mil: {enemy.soldiers:,}S · {enemy.tanks:,}T · "
        f"{enemy.aircraft:,} planes · {enemy.ships:,} ships"
    )
    tips.append(
        f"Your mil: {our.soldiers:,}S · {our.tanks:,}T · "
        f"{our.aircraft:,} planes · {our.ships:,} ships"
    )

    # Priority actions
    if controls.air_sup_def:
        tips.append(
            f"**Break their air superiority** — any airstrike win. "
            f"Planes {our.aircraft:,} vs {enemy.aircraft:,} (\~{air['immense_pct']:.0f}% IT) · 4 MAP"
        )
    elif not controls.air_sup_att and enemy.aircraft > 0:
        tips.append(
            f"**Take air superiority** — airstrike IT. "
            f"Planes {our.aircraft:,} vs {enemy.aircraft:,} (\~{air['immense_pct']:.0f}% IT) · 4 MAP"
        )
    elif controls.air_sup_att:
        tips.append("Air superiority is yours — enemy tanks fight at half. Ground hard.")
    else:
        tips.append("No enemy planes left — skip air.")

    if controls.blockade_def:
        tips.append(
            "**Break blockade** — any naval win vs them, zero their ships, "
            "or a partner at war with them lands naval IT."
        )
    if not controls.blockade_att and enemy.ships > 0:
        tips.append(
            f"**Blockade** — naval IT. Ships {our.ships:,} vs {enemy.ships:,} "
            f"(\~{nav['immense_pct']:.0f}% IT) · 4 MAP. Also frees allies they blockaded."
        )
    elif controls.blockade_att:
        tips.append("Blockade is up — they can't bank or trade.")
    elif enemy.ships <= 0:
        tips.append("Enemy has no ships — blockade not needed.")

    tips.append(
        f"**Ground** — strip soldiers/tanks. "
        f"(\~{gnd['immense_pct']:.0f}% IT) · 3 MAP"
        + (" · GC chips their planes on wins" if controls.ground_ctrl_att else "")
    )

    if our_map < 3:
        tips.append("Not enough MAP — wait for the next turn.")
    elif our_map >= 4 and not controls.air_sup_att and enemy.aircraft > 0:
        tips.append("You have MAP for air this cycle if IT looks good.")
    elif our_map >= 3:
        tips.append("You have MAP for ground this cycle.")

    return tips
