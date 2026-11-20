"""Helpers for AI-triggered Discord actions: intents, timezones and time parsing.

Pure functions only (no Discord objects), so they are easy to test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

try:  # tzdata may be missing on slim images; fixed offsets still work without it
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

MIN_DELAY = timedelta(minutes=1)
MAX_DELAY = timedelta(days=30)
MAX_PENDING_PER_USER = 10
TEXT_MAX = 300
MAX_PURGE = 100

ACTION_TOOL_NAMES = frozenset({"remind", "list_reminders", "cancel_reminder", "set_timezone", "channel_action"})
CHANNEL_ACTIONS = frozenset({"delete", "rename", "set_topic", "lock", "unlock", "slowmode", "purge"})


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    kind: str                      # remind | list_reminders | cancel_reminder | set_timezone | channel_action
    text: str = ""
    delay_minutes: float | None = None
    at_local: str | None = None    # 'YYYY-MM-DD HH:MM' in the requester's timezone
    targets: list[int] = field(default_factory=list)
    action: str = ""               # channel_action: one of CHANNEL_ACTIONS
    value: str = ""
    reminder_id: int | None = None
    cancel_all: bool = False
    tz: str = ""


def _num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _int_list(values) -> list[int]:
    out: list[int] = []
    for v in values or []:
        m = re.search(r"\d{5,25}", str(v))
        if m and int(m.group()) not in out:
            out.append(int(m.group()))
    return out


def intent_from_tool(name: str, args: dict) -> Intent | None:
    """Turn a planner tool call into an Intent (None if it isn't a usable action)."""
    args = args if isinstance(args, dict) else {}
    if name == "remind":
        return Intent(
            "remind",
            text=str(args.get("text") or "").strip(),
            delay_minutes=_num(args.get("delay_minutes")),
            at_local=(str(args.get("at_local") or "").strip() or None),
            targets=_int_list(args.get("targets")),
        )
    if name == "list_reminders":
        return Intent("list_reminders")
    if name == "cancel_reminder":
        rid = _num(args.get("id"))
        return Intent("cancel_reminder", reminder_id=int(rid) if rid is not None else None,
                      cancel_all=bool(args.get("all")))
    if name == "set_timezone":
        tz = str(args.get("tz") or "").strip()
        return Intent("set_timezone", tz=tz) if tz else None
    if name == "channel_action":
        action = str(args.get("action") or "").strip().lower()
        if action not in CHANNEL_ACTIONS:
            return None
        return Intent(
            "channel_action", action=action, value=str(args.get("value") or "").strip(),
            delay_minutes=_num(args.get("delay_minutes")),
            at_local=(str(args.get("at_local") or "").strip() or None),
        )
    return None


_UNIT_MINUTES = {"min": 1, "mins": 1, "minute": 1, "minutes": 1, "hr": 60, "hrs": 60, "hour": 60,
                 "hours": 60, "day": 1440, "days": 1440, "week": 10080, "weeks": 10080}
_SIMPLE_RE = re.compile(
    r"\b(?P<verb>remind|ping)\b(?P<who>.*?)\bin\s+(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>mins?|minutes?|hrs?|hours?|days?|weeks?)\b(?P<rest>.*)",
    re.IGNORECASE | re.DOTALL,
)


def parse_simple_reminder(text: str) -> Intent | None:
    """Offline fallback (planner down): 'remind me in 1 hour to X' / 'ping <@id> in 30 min'."""
    m = _SIMPLE_RE.search(text or "")
    if not m:
        return None
    minutes = float(m.group("n")) * _UNIT_MINUTES[m.group("unit").lower()]
    rest = re.sub(r"^\s*(?:to|that|about|:|,|-)\s*", "", m.group("rest").strip(), flags=re.IGNORECASE)
    targets = _int_list(re.findall(r"<@!?(\d+)>", m.group("who")))
    return Intent("remind", text=rest.strip(), delay_minutes=minutes, targets=targets)


# ---------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------

# name -> (IANA zone, fixed fallback offset in minutes or None when DST makes a fixed offset wrong)
_ABBREV = {
    "UTC": ("UTC", 0), "GMT": ("UTC", 0), "Z": ("UTC", 0),
    "WAT": ("Africa/Lagos", 60), "CAT": ("Africa/Maputo", 120), "EAT": ("Africa/Nairobi", 180),
    "SAST": ("Africa/Johannesburg", 120), "IST": ("Asia/Kolkata", 330), "PKT": ("Asia/Karachi", 300),
    "JST": ("Asia/Tokyo", 540), "KST": ("Asia/Seoul", 540), "SGT": ("Asia/Singapore", 480),
    "AEST": ("Australia/Brisbane", 600),
    "EST": ("America/New_York", None), "EDT": ("America/New_York", None),
    "PST": ("America/Los_Angeles", None), "PDT": ("America/Los_Angeles", None),
    "CET": ("Europe/Paris", None), "BST": ("Europe/London", None),
}
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedTZ:
    name: str       # what we store: IANA name or 'UTC+01:00'
    tz: tzinfo


def _fixed(minutes: int) -> ResolvedTZ:
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return ResolvedTZ(f"UTC{sign}{h:02d}:{m:02d}", timezone(timedelta(minutes=minutes)))


def _zone(name: str) -> ResolvedTZ | None:
    if ZoneInfo is None:
        return None
    candidates = [name, "/".join(p.title() if p.islower() else p for p in name.replace(" ", "_").split("/"))]
    for cand in candidates:
        try:
            return ResolvedTZ(cand, ZoneInfo(cand))
        except Exception:
            continue
    return None


def resolve_timezone(text: str) -> ResolvedTZ | None:
    """'Africa/Lagos', 'wat', 'UTC+1', 'GMT-5:30' -> a usable timezone (None if not understood)."""
    t = (text or "").strip()
    if not t or len(t) > 40:
        return None
    m = _OFFSET_RE.match(t)
    if m:
        minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
        minutes = -minutes if m.group(1) == "-" else minutes
        return _fixed(minutes) if -12 * 60 <= minutes <= 14 * 60 else None
    abbr = _ABBREV.get(t.upper())
    if abbr:
        zone = _zone(abbr[0])
        if zone is not None:
            return zone
        return _fixed(abbr[1]) if abbr[1] is not None else None
    return _zone(t)


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------

@dataclass
class DueResult:
    due: datetime | None = None
    error: str | None = None   # NO_TIME | NEED_TZ | BAD_TIME | PAST | TOO_SOON | TOO_FAR


ERROR_TEXT = {
    "NO_TIME": "I need a time, like 'in 30 minutes' or 'at 15:00'.",
    "NEED_TZ": "I don't know your timezone yet. Tell me, e.g. '@me my timezone is Africa/Lagos' (or 'UTC+1'), then ask again.",
    "BAD_TIME": "I couldn't read that time. Try 'in 30 minutes' or 'at 15:00 tomorrow'.",
    "PAST": "That time has already passed.",
    "TOO_SOON": "Reminders must be at least 1 minute away.",
    "TOO_FAR": "Reminders can be at most 30 days away.",
}


def compute_due(now_utc: datetime, *, delay_minutes: float | None, at_local: str | None, tz: tzinfo | None) -> DueResult:
    """When something should fire. Relative delays need no timezone; clock times use the requester's."""
    if delay_minutes is not None and delay_minutes > 0:
        due = now_utc + timedelta(minutes=delay_minutes)
    elif at_local:
        if tz is None:
            return DueResult(error="NEED_TZ")
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(at_local.strip(), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return DueResult(error="BAD_TIME")
        due = parsed.replace(tzinfo=tz).astimezone(timezone.utc)
    else:
        return DueResult(error="NO_TIME")
    if due <= now_utc:
        return DueResult(error="PAST")
    if due - now_utc < MIN_DELAY - timedelta(seconds=5):
        return DueResult(error="TOO_SOON")
    if due - now_utc > MAX_DELAY:
        return DueResult(error="TOO_FAR")
    return DueResult(due=due)


def fmt_delta(td: timedelta) -> str:
    # Round to the nearest minute so "in 1 hour" doesn't show as "in 59 minutes" a moment later.
    total_minutes = max(0, int((td.total_seconds() + 30) // 60))
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return "in " + (" ".join(parts) if parts else "under a minute")


def fmt_local(due_utc: datetime, tz: tzinfo | None, tz_name: str | None = None) -> str:
    """'Thu 19 Nov 15:32 WAT' in the person's timezone, or '... UTC' if we don't know it."""
    if tz is None:
        return f"{due_utc:%a %d %b %H:%M} UTC"
    local = due_utc.astimezone(tz)
    label = local.tzname() or tz_name or ""
    if label.startswith(("+", "-")) or not label:
        label = tz_name or label
    return f"{local:%a %d %b %H:%M} {label}".strip()


def clean_text(text: str) -> str:
    """Reminder text safe to repost: no @everyone/@here, capped length."""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = t.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return t[:TEXT_MAX]
