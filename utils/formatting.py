"""
Shared formatting helpers used by multiple cogs (alerts, warroom,
counter). Lives here, not inside any one cog, so importing it never
creates a circular import between cogs that both need it.
"""

from datetime import datetime, timezone


def format_duration(last_active_iso: str | None) -> str:
    if not last_active_iso:
        return "unknown"
    try:
        last = datetime.fromisoformat(last_active_iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - last
        days, rem = divmod(int(delta.total_seconds()), 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d{hours}h"
        if hours:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"
    except Exception:
        return "unknown"


def military_line(nation: dict) -> str:
    return (
        f"`{nation.get('soldiers', 0)} 💂 | {nation.get('tanks', 0)} ⚙️ | "
        f"{nation.get('aircraft', 0)} ✈️ | {nation.get('ships', 0)} 🚢 | "
        f"{nation.get('spies', 0)} 🔍`"
    )


def nation_block(nation: dict, resistance, maps) -> str:
    name = nation.get("nation_name", "Unknown")
    nation_id = nation.get("id")
    alliance_name = (nation.get("alliance") or {}).get("name", "None")
    position = (nation.get("alliance_position") or "").title() or "None"
    active = format_duration(nation.get("last_active"))
    nation_link = f"https://politicsandwar.com/nation/id={nation_id}" if nation_id else ""
    num_cities = len(nation.get("cities") or [])

    lines = [
        f"[**{name}**]({nation_link}) — *{alliance_name}* — {active} — {position} — {num_cities} cities",
        military_line(nation),
    ]
    if resistance is not None:
        lines.append(f"Resistance: {resistance}/100")
    if maps is not None:
        lines.append(f"MAPs available: {maps}/12")
    return "\n".join(lines)
