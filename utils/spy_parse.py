"""Parse Politics & War espionage result messages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RES_ALIASES = {
    "money": "money", "cash": "money",
    "coal": "coal", "oil": "oil", "uranium": "uranium",
    "lead": "lead", "iron": "iron", "bauxite": "bauxite",
    "gasoline": "gasoline", "munitions": "munitions",
    "steel": "steel", "aluminum": "aluminum", "aluminium": "aluminum",
    "food": "food", "credits": "credits", "credit": "credits",
}

# Build cost: money + resources (post-2025 where known)
UNIT_BUILD_COST = {
    "nuke": {"money": 1_750_000, "aluminum": 1000, "gasoline": 500, "uranium": 500},
    "missile": {"money": 150_000, "aluminum": 150, "gasoline": 100, "munitions": 100},
    "spy": {"money": 50_000},
    "tank": {"money": 60, "steel": 0.5, "gasoline": 0.75},  # approx per tank; refine if needed
    "aircraft": {"money": 4000, "aluminum": 10, "gasoline": 1.25},
    "ship": {"money": 50000, "steel": 20, "gasoline": 2.75},
    "soldier": {"money": 5},
}

SPY_RECRUIT_COST = 50_000


def _num(s: str) -> float:
    try:
        return float(s.strip().replace(",", ""))
    except ValueError:
        return 0.0


@dataclass
class SpyReport:
    kind: str  # intel | sabotage | spykill | other
    target: str | None = None
    resources: dict[str, float] = field(default_factory=dict)
    op_cost_money: float | None = None
    spies_lost: int | None = None
    spies_killed: int | None = None
    destroyed: dict[str, int] = field(default_factory=dict)  # unit -> count


def parse_spy_message(text: str) -> SpyReport | None:
    if not text or len(text) < 20:
        return None
    t = text.replace("\xa0", " ")
    low = t.lower()

    markers = (
        "your spies discovered", "gathered intelligence", "your agents were able",
        "spies were captured", "the operation cost you", "assassinate",
        "destroy", "sabotage", "detonated", "terrorize", "nuclear weapon",
        "missile",
    )
    if not any(m in low for m in markers):
        return None

    report = SpyReport(kind="other")

    # Target: "about X" / "in X" / "against X"
    m = re.search(
        r"(?:gathered intelligence about|intelligence about|discovered that|in|against)\s+([A-Za-z0-9][A-Za-z0-9 _'\-]{1,40}?)(?:\.|,| Your |$)",
        t,
        flags=re.I,
    )
    if m:
        report.target = m.group(1).strip().rstrip(".")

    if "discovered that" in low or "gathered intelligence" in low:
        report.kind = "intel"
        m_money = re.search(r"has\s+\$([0-9,]+\.?[0-9]*)", t, flags=re.I)
        if m_money:
            report.resources["money"] = _num(m_money.group(1))
        for m in re.finditer(
            r"([0-9,]+\.?[0-9]*)\s+(coal|oil|uranium|lead|iron|bauxite|gasoline|munitions|steel|aluminum|aluminium|food|credits?)",
            t,
            flags=re.I,
        ):
            raw = m.group(2).lower()
            key = "credits" if raw.startswith("credit") else RES_ALIASES.get(raw)
            if key:
                report.resources[key] = _num(m.group(1))

    # Destroyed units
    for label, key in (
        (r"(\d+)\s+nuclear weapons?", "nuke"),
        (r"(\d+)\s+nukes?", "nuke"),
        (r"destroyed\s+(\d+)\s+nuclear weapon", "nuke"),
        (r"(\d+)\s+missiles?", "missile"),
        (r"(\d+)\s+tanks?", "tank"),
        (r"(\d+)\s+aircraft", "aircraft"),
        (r"(\d+)\s+planes?", "aircraft"),
        (r"(\d+)\s+ships?", "ship"),
        (r"(\d+)\s+soldiers?", "soldier"),
    ):
        m = re.search(label, t, flags=re.I)
        if m:
            report.destroyed[key] = report.destroyed.get(key, 0) + int(m.group(1))
            report.kind = "sabotage"

    if "assassinate" in low or re.search(r"killed\s+\d+\s+(?:enemy\s+)?spies", low):
        report.kind = "spykill"
        m = re.search(r"(?:killed|eliminated|destroyed)\s+(\d+)\s+(?:enemy\s+)?spies", t, flags=re.I)
        if m:
            report.spies_killed = int(m.group(1))
            report.destroyed["spy"] = report.destroyed.get("spy", 0) + int(m.group(1))

    m = re.search(r"operation cost you\s+\$([0-9,]+\.?[0-9]*)", t, flags=re.I)
    if m:
        report.op_cost_money = _num(m.group(1))

    m = re.search(r"(\d+)\s+of your spies were captured", t, flags=re.I)
    if m:
        report.spies_lost = int(m.group(1))

    if report.kind == "other" and not report.destroyed and not report.resources and report.op_cost_money is None:
        return None
    return report


def format_money(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"


def value_resources(resources: dict[str, float], prices: dict | None) -> float:
    total = float(resources.get("money") or 0)
    if not prices:
        return total
    for k, amt in resources.items():
        if k == "money" or not amt:
            continue
        p = prices.get(k)
        if p is None:
            continue
        try:
            total += float(amt) * float(p)
        except (TypeError, ValueError):
            pass
    return total


def unit_production_value(unit: str, count: int, prices: dict | None) -> float:
    cost = UNIT_BUILD_COST.get(unit)
    if not cost or count <= 0:
        return 0.0
    total = 0.0
    for k, amt in cost.items():
        if k == "money":
            total += float(amt) * count
        else:
            p = (prices or {}).get(k)
            if p is not None:
                total += float(amt) * count * float(p)
            # if no price, still count money part only
    return total
