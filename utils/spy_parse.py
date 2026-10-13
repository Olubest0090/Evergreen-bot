
"""Parse Politics & War espionage result messages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


RESOURCES = (
    "money", "coal", "oil", "uranium", "lead", "iron", "bauxite",
    "gasoline", "munitions", "steel", "aluminum", "food", "credits",
)

# map display words -> key
RES_ALIASES = {
    "money": "money", "$": "money", "cash": "money",
    "coal": "coal", "oil": "oil", "uranium": "uranium",
    "lead": "lead", "iron": "iron", "bauxite": "bauxite",
    "gasoline": "gasoline", "munitions": "munitions",
    "steel": "steel", "aluminum": "aluminum", "aluminium": "aluminum",
    "food": "food", "credits": "credits", "credit": "credits",
}


def _num(s: str) -> float:
    s = s.strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


@dataclass
class SpyReport:
    kind: str  # intel | spykill | other | unknown
    target: str | None = None
    resources: dict[str, float] = field(default_factory=dict)
    op_cost_money: float | None = None
    spies_lost: int | None = None
    spies_killed: int | None = None
    raw_note: str = ""


def parse_spy_message(text: str) -> SpyReport | None:
    if not text or len(text) < 20:
        return None
    t = text.replace("\xa0", " ")
    low = t.lower()

    # Must look like a P&W spy result
    markers = (
        "your spies discovered",
        "you successfully gathered intelligence",
        "your agents were able to operate",
        "spies were captured",
        "the operation cost you",
        "assassinate",
        "destroy spies",
        "terrorize",
        "sabotage",
    )
    if not any(m in low for m in markers):
        return None

    report = SpyReport(kind="unknown")

    # Target name
    m = re.search(
        r"(?:gathered intelligence about|intelligence about|discovered that)\s+([A-Za-z0-9][A-Za-z0-9 _'\-]{1,40})",
        t,
        flags=re.I,
    )
    if m:
        report.target = m.group(1).strip().rstrip(".")

    # Intel resource block: "$14,467,712.54, 468.00 coal, ..."
    if "discovered that" in low or "gathered intelligence" in low:
        report.kind = "intel"
        # money first
        m$ = re.search(r"has\s+\$([0-9,]+\.?[0-9]*)", t, flags=re.I)
        if m$:
            report.resources["money"] = _num(m$.group(1))
        # "468.00 coal" style
        for m in re.finditer(
            r"([0-9,]+\.?[0-9]*)\s+(coal|oil|uranium|lead|iron|bauxite|gasoline|munitions|steel|aluminum|aluminium|food|credits?)",
            t,
            flags=re.I,
        ):
            key = RES_ALIASES.get(m.group(2).lower().rstrip("s") if m.group(2).lower().endswith("credits") else m.group(2).lower())
            if not key:
                key = RES_ALIASES.get(m.group(2).lower())
            if key:
                report.resources[key] = _num(m.group(1))
            # fix credits plural
            raw = m.group(2).lower()
            if raw.startswith("credit"):
                report.resources["credits"] = _num(m.group(1))

    # Op cost
    m = re.search(r"operation cost you\s+\$([0-9,]+\.?[0-9]*)", t, flags=re.I)
    if m:
        report.op_cost_money = _num(m.group(1))

    # Spies lost / killed
    m = re.search(r"(\d+)\s+of your spies were captured", t, flags=re.I)
    if m:
        report.spies_lost = int(m.group(1))
    m = re.search(r"(?:killed|eliminated|destroyed)\s+(\d+)\s+(?:enemy\s+)?spies", t, flags=re.I)
    if m:
        report.spies_killed = int(m.group(1))
        if report.kind == "unknown":
            report.kind = "spykill"

    if "destroy spies" in low or "assassinate" in low:
        if report.kind == "unknown":
            report.kind = "spykill"

    if report.kind == "unknown" and (report.op_cost_money is not None or report.resources):
        report.kind = "other"

    if report.kind == "unknown" and report.op_cost_money is None and not report.resources:
        return None
    return report


def format_money(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"


def value_resources(resources: dict[str, float], prices: dict | None) -> float:
    """Rough market value. Money counts as 1:1. Missing prices → 0 for that res."""
    total = float(resources.get("money") or 0)
    if not prices:
        return total
    for k, amt in resources.items():
        if k == "money" or not amt:
            continue
        # tradeprices keys match resource names
        p = prices.get(k)
        if p is None:
            continue
        try:
            total += float(amt) * float(p)
        except (TypeError, ValueError):
            pass
    return total
