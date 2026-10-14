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
    """MILCOM advice from P&W wiki + admin blockade rules.

    MAP is per-war only (+1/turn, cap 12).
    Blockade break (wiki):
      1) blockaded nation any naval win vs blockader
      2) blockader reaches 0 ships
      3) third party also at war with blockader lands naval IT on them
    """
    tips: list[str] = []
    air = simulate_air(our, enemy)
    nav = simulate_naval(our, enemy)
    gnd = simulate_ground(
        our, enemy,
        att_has_air_sup=controls.air_sup_att,
        def_has_air_sup=controls.air_sup_def,
    )

    tips.append(
        f"**This war MAP** you {our_map}/12 | enemy {enemy_map}/12 | "
        f"Res you {our_res} | enemy {enemy_res}"
    )
    tips.append(
        "_MAP is per war only. +1 MAP each turn (2h), cap 12. "
        "Other wars do not share this MAP._"
    )

    if enemy_other_wars:
        tips.append(
            f"Enemy has **{enemy_other_wars}** other active war(s) "
            f"(units/beige only — not this war's MAP)."
        )

    ctrl = []
    if controls.air_sup_att:
        ctrl.append("YOU air superiority")
    if controls.air_sup_def:
        ctrl.append("ENEMY air superiority")
    if controls.ground_ctrl_att:
        ctrl.append("YOU ground control")
    if controls.ground_ctrl_def:
        ctrl.append("ENEMY ground control")
    if controls.blockade_att:
        ctrl.append("YOU blockade them")
    if controls.blockade_def:
        ctrl.append("THEY blockade you")
    tips.append("Controls: " + (", ".join(ctrl) if ctrl else "none"))

    # 1 Air
    if not controls.air_sup_att and enemy.aircraft > 0:
        tips.append(
            f"**1. Air IT** for superiority (halves enemy tanks). "
            f"Planes {our.aircraft} vs {enemy.aircraft} "
            f"({ratio_label(air['att_value'], air['def_value'])}, "
            f"\~{air['immense_pct']:.0f}% IT). **{MAP_COST['air']} MAP** this war."
        )
        if air["immense_pct"] < 35 and our.aircraft < enemy.aircraft:
            tips.append("   Weak air odds — wait or let a stronger slot partner take IT.")
    elif controls.air_sup_att:
        tips.append(
            "**Air superiority held** — enemy tanks at 50%. "
            "Any enemy airstrike win can break it. Don't re-dogfight unless they rebuild."
        )
    else:
        tips.append("Enemy \~0 planes — air optional.")

    # 2 Blockade (official break rules)
    if controls.blockade_def:
        tips.append(
            "**You are blockaded.** Break by: (1) any naval win vs the blockader, "
            "(2) zero their ships, or (3) an ally also at war with them lands **naval IT**."
        )

    if not controls.blockade_att and enemy.ships > 0:
        if our_map >= MAP_COST["naval"]:
            tips.append(
                f"**2. Naval IT → blockade.** Ships {our.ships} vs {enemy.ships} "
                f"({ratio_label(nav['att_value'], nav['def_value'])}, "
                f"\~{nav['immense_pct']:.0f}% IT). **{MAP_COST['naval']} MAP**. "
                f"Your naval IT also frees allies they were blockading."
            )
        else:
            tips.append(
                f"**2. Want blockade** but need {MAP_COST['naval']} MAP "
                f"(you have {our_map}). Bank a turn or let a partner naval IT."
            )
    elif controls.blockade_att:
        tips.append(
            "**You blockade them** — no bank/trade for them. "
            "Broken if they get any naval win on you, you hit 0 ships, "
            "or someone else naval-ITs you while at war with you."
        )
    elif enemy.ships == 0:
        tips.append("Enemy 0 ships — blockade N/A (0 ships also clears their blockade on others).")

    # 3 Ground
    tips.append(
        f"**3. Ground** to strip S/T. Value {gnd['att_value']:.0f} vs {gnd['def_value']:.0f} "
        f"({ratio_label(gnd['att_value'], gnd['def_value'])}, \~{gnd['immense_pct']:.0f}% IT). "
        f"**{MAP_COST['ground']} MAP**."
    )
    if controls.air_sup_att:
        tips.append("   With AS, prefer ground over re-dogfighting.")
    if controls.ground_ctrl_att:
        tips.append("   Ground control held — winning grounds also chip their aircraft.")

    if our_map < 3:
        tips.append("**Low MAP** — wait (+1/turn) before spending.")
    elif our_map >= 7 and not controls.air_sup_att and enemy.aircraft > 0:
        tips.append("**High MAP** — can air (4) then ground (3) same cycle if IT is realistic.")

    tips.append(
        "Stack: **air IT → naval IT (blockade + free partners) → grounds**. "
        "If a partner already holds AS on this target, others ground/naval."
    )
    return tips
