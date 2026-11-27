"""Pure audit logic for /audit and /roster (no Discord, no network).

Everything here takes plain nation dicts as returned by utils.audit_data and
returns plain data, so it can be tested without the bot running.

Military definitions (maximum an alliance member can field per city):
    5 barracks x 3000 soldiers = 15000   5 factories x 250 tanks   = 1250
    5 hangars  x 15 aircraft   = 75      3 drydocks  x 5 ships     = 15
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

IMPROVEMENT_TARGETS = {"barracks": 5, "factory": 5, "hangar": 5, "drydock": 3}
IMPROVEMENT_LABELS = {
    "barracks": "Barracks",
    "factory": "Factory",
    "hangar": "Hangar",
    "drydock": "Drydock",
}

# unit -> (improvement that houses it, units per building)
UNIT_RULES = {
    "soldiers": ("barracks", 3000),
    "tanks": ("factory", 250),
    "aircraft": ("hangar", 15),
    "ships": ("drydock", 5),
}
UNIT_SHORT = {"soldiers": "S", "tanks": "T", "aircraft": "A", "ships": "N"}

INACTIVE_DAYS = 7  # same cutoff the rest of the bot uses for "inactive"
WATCH_DAYS = 3     # listed in the activity report from this point on


def nation_url(nation_id) -> str:
    return f"https://politicsandwar.com/nation/id={nation_id}"


def nation_link(nation: dict) -> str:
    """Markdown link to the nation page, safe against [ ] in nation names."""
    name = str(nation.get("nation_name") or nation.get("id")).replace("[", "(").replace("]", ")")
    return f"[{name}]({nation_url(nation.get('id'))})"


def split_members(nations: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (active_members, vacation_mode_members). Applicants are dropped."""
    active, vacation = [], []
    for n in nations:
        if str(n.get("alliance_position") or "").upper() == "APPLICANT":
            continue
        if int(n.get("vacation_mode_turns") or 0) > 0:
            vacation.append(n)
        else:
            active.append(n)
    return active, vacation


def improvement_gaps(nation: dict) -> dict[str, int]:
    """Missing military improvements, summed over all cities. {} = compliant."""
    cities = nation.get("cities") or []
    gaps: dict[str, int] = {}
    for building, target in IMPROVEMENT_TARGETS.items():
        missing = sum(max(0, target - int(c.get(building) or 0)) for c in cities)
        if missing:
            gaps[building] = missing
    return gaps


def format_gaps(gaps: dict[str, int]) -> str:
    return ", ".join(f"{n}× {IMPROVEMENT_LABELS[b]}" for b, n in gaps.items())


def improvement_progress(nation: dict) -> dict[str, float]:
    """Average buildings per city for each improvement that is below target.

    Rounded DOWN to 1 decimal so 4.97 never displays as the full 5.
    {} = compliant.
    """
    cities = nation.get("cities") or []
    if not cities:
        return {}
    out: dict[str, float] = {}
    for building, target in IMPROVEMENT_TARGETS.items():
        have = sum(min(target, int(c.get(building) or 0)) for c in cities)
        if have < target * len(cities):
            out[building] = math.floor(have / len(cities) * 10) / 10
    return out


def missing_share(nation: dict) -> float:
    """Fraction (0..1) of all target buildings that are missing; used to sort worst-first."""
    cities = len(nation.get("cities") or [])
    if not cities:
        return 0.0
    total = cities * sum(IMPROVEMENT_TARGETS.values())
    return sum(improvement_gaps(nation).values()) / total


def _num(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:.1f}"


def format_progress(nation: dict) -> str:
    """'Barracks 0/5 · Factory 2.5/5' — average per city against the target."""
    prog = improvement_progress(nation)
    return " · ".join(
        f"{IMPROVEMENT_LABELS[b]} {_num(v)}/{IMPROVEMENT_TARGETS[b]}" for b, v in prog.items()
    )


def military_status(nation: dict) -> dict:
    """Each unit as a fraction (0..1) of the maximum the city count allows."""
    cities = len(nation.get("cities") or [])
    per_unit: dict[str, float] = {}
    for unit, (building, per_building) in UNIT_RULES.items():
        cap = cities * IMPROVEMENT_TARGETS[building] * per_building
        have = int(nation.get(unit) or 0)
        per_unit[unit] = min(1.0, have / cap) if cap else 1.0
    overall = sum(per_unit.values()) / len(per_unit)
    return {
        "cities": cities,
        "per_unit": per_unit,
        "overall": overall,
        "is_full": all(v >= 1.0 for v in per_unit.values()),
    }


def percent(fraction: float) -> int:
    """Floor, so a nation that is 99.6% is never shown as 100%."""
    return int(fraction * 100)


def below_military(members: list[dict], threshold_pct: int = 100) -> list[tuple[dict, dict]]:
    """Members under the threshold, lowest first."""
    rows = []
    for n in members:
        status = military_status(n)
        if threshold_pct >= 100:
            under = not status["is_full"]
        else:
            under = percent(status["overall"]) < threshold_pct
        if under:
            rows.append((n, status))
    rows.sort(key=lambda r: r[1]["overall"])
    return rows


def parse_last_active(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_inactive(nation: dict, now: datetime) -> float | None:
    dt = parse_last_active(nation.get("last_active"))
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 86400)


def activity_report(members: list[dict], now: datetime) -> dict:
    """Bucket counts plus the (nation, days) list of members idle WATCH_DAYS+."""
    counts = {"day": 0, "three": 0, "week": 0, "inactive": 0, "unknown": 0}
    watch: list[tuple[dict, float]] = []
    for n in members:
        days = days_inactive(n, now)
        if days is None:
            counts["unknown"] += 1
        elif days < 1:
            counts["day"] += 1
        elif days < WATCH_DAYS:
            counts["three"] += 1
        elif days < INACTIVE_DAYS:
            counts["week"] += 1
        else:
            counts["inactive"] += 1
        if days is not None and days >= WATCH_DAYS:
            watch.append((n, days))
    watch.sort(key=lambda r: -r[1])
    return {"counts": counts, "watch": watch}
