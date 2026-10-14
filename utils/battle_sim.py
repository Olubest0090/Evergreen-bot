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
    """Priority: air superiority → blockade → strip military.

    MAP is per-war only (other wars do not share or drain this war's MAP).
    Ally naval IT / blockade on the enemy can lift the enemy's blockade on partners.
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
        f"**This war only** — MAP you {our_map} | enemy {enemy_map} | "
        f"Res you {our_res} | enemy {enemy_res}"
    )
    tips.append("_MAP does not transfer between wars. Each war has its own MAP._")

    if enemy_other_wars:
        tips.append(
            f"Enemy is in **{enemy_other_wars}** other active war(s) "
            f"(units/beige pressure only — **not** MAP on this war)."
        )

    ctrl = []
    if controls.air_sup_att:
        ctrl.append("YOU have air superiority")
    if controls.air_sup_def:
        ctrl.append("ENEMY has air superiority")
    if controls.ground_ctrl_att:
        ctrl.append("YOU have ground control")
    if controls.blockade_att:
        ctrl.append("YOU blockade them")
    if controls.blockade_def:
        ctrl.append("THEY blockade you")
    tips.append("Controls: " + (", ".join(ctrl) if ctrl else "none"))

    # 1) Air
    if not controls.air_sup_att and enemy.aircraft > 0:
        tips.append(
            f"**1. Air first** — dogfight for IT. Planes {our.aircraft} vs {enemy.aircraft} "
            f"({ratio_label(air['att_value'], air['def_value'])}, "
            f"\~{air['immense_pct']:.0f}% IT). Costs {MAP_COST['air']} MAP **on this war**."
        )
        if air["immense_pct"] < 35 and our.aircraft < enemy.aircraft:
            tips.append("   Air is ugly — wait for more planes or let a stronger slot partner take IT.")
    elif controls.air_sup_att:
        tips.append(
            "**Air superiority held** — enemy tanks halved here. "
            "Don't waste this war's MAP re-dogfighting unless they rebuild planes."
        )
    else:
        tips.append("Enemy has \~0 planes — air optional on this war.")

    # 2) Blockade — ally naval can free partners
    if controls.blockade_def:
        tips.append(
            "**Enemy is blockading you (or a partner).** "
            "A **successful naval / blockade IT by anyone on this target** can lift their blockade on allies. "
            "Prioritize naval if ships allow."
        )

    if not controls.blockade_att and enemy.ships > 0 and our_map >= MAP_COST["naval"]:
        tips.append(
            f"**2. Blockade** — naval IT. Ships {our.ships} vs {enemy.ships} "
            f"({ratio_label(nav['att_value'], nav['def_value'])}, \~{nav['immense_pct']:.0f}% IT). "
            f"{MAP_COST['naval']} MAP on this war. "
            f"Landing blockade also helps free partners they were blockading."
        )
    elif controls.blockade_att:
        tips.append(
            "**You blockade them** — they can't bank/trade. "
            "Partners they were blockading should also be free of that blockade."
        )
    elif enemy.ships == 0:
        tips.append("Enemy navy empty — blockade not needed.")

    # 3) Ground
    tips.append(
        f"**3. Strip ground** — S/T value you {gnd['att_value']:.0f} vs {gnd['def_value']:.0f} "
        f"({ratio_label(gnd['att_value'], gnd['def_value'])}, \~{gnd['immense_pct']:.0f}% IT). "
        f"{MAP_COST['ground']} MAP on this war."
    )
    if controls.air_sup_att:
        tips.append("   With AS, prefer grounds over re-airstriking planes.")
    if controls.ground_ctrl_att:
        tips.append("   You have GC — grounds also chip their aircraft. Keep grounding.")

    # MAP budgeting (this war only)
    if our_map < 3:
        tips.append("**Low MAP on this war** — wait a turn (+1 MAP/turn) before attacking.")
    elif our_map >= 7:
        tips.append("**High MAP on this war** — can open air (4) then ground (3), or double ground.")
    elif our_map >= 4 and not controls.air_sup_att and enemy.aircraft > 0:
        tips.append("Spend **4 MAP on air** this war if IT is realistic; otherwise bank for ground.")

    tips.append(
        "Stack order: **air IT → naval blockade (frees partners too) → grounds**. "
        "If a partner already holds AS on this target, others ground/naval — don't all re-dogfight. "
        "Each war's MAP is independent."
    )
    return tips
