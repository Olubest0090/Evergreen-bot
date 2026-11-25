"""Shared spy-loss suspect detection for Evergreen and Federal Sphere.

Game rule (the same numbers Locutus uses): a spy op can only target a nation whose
score is between 0.4x and 2.5x of the ATTACKER's own score. Turned around, for a
victim with score D the attacker's score must lie between D / 2.5 and D / 0.4.

Nothing here proves who ran an operation. It builds a ranked candidate list and says
how many nations were left out and why.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

SPY_RANGE_MIN_MODIFIER = 0.4
SPY_RANGE_MAX_MODIFIER = 2.5

# Scores move a little between the op and the moment the loss is noticed.
RANGE_PADDING = 0.02

# "Recent" = active within this long before the alert. This used to be a hard cutoff;
# it is now only the boundary between two displayed tiers.
RECENT_WINDOW_SECONDS = 15 * 60
EARLIER_WINDOW_SECONDS = 60 * 60

# Kept so older imports keep working.
ONLINE_WINDOW_SECONDS = RECENT_WINDOW_SECONDS


def parse_last_active(value):
    if not value:
        return None

    try:
        value = str(value).strip()

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        result = datetime.fromisoformat(value)

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return None


def format_activity_age(value):
    last_active = parse_last_active(value)

    if not last_active:
        return "Unknown activity time"

    seconds = max(
        0,
        int((datetime.now(timezone.utc) - last_active).total_seconds())
    )

    if seconds < 60:
        return f"{seconds}s ago"

    if seconds < 3600:
        return f"{seconds // 60}m ago"

    if seconds < 86400:
        return f"{seconds // 3600}h ago"

    return f"{seconds // 86400}d ago"


def attacker_score_bounds(defender_score: float) -> tuple[float, float]:
    """Lowest and highest attacker score that could spy on this defender."""
    low = defender_score / SPY_RANGE_MAX_MODIFIER
    high = defender_score / SPY_RANGE_MIN_MODIFIER
    return low * (1 - RANGE_PADDING), high * (1 + RANGE_PADDING)


def is_in_spy_range(attacker_score: float, defender_score: float) -> bool:
    return (
        attacker_score * SPY_RANGE_MIN_MODIFIER
        <= defender_score
        <= attacker_score * SPY_RANGE_MAX_MODIFIER
    )


@dataclass
class SuspectReport:
    detected_at: datetime
    victim_id: int
    victim_score: float
    min_score: float
    max_score: float
    recent: list = field(default_factory=list)
    earlier: list = field(default_factory=list)
    older_count: int = 0
    in_range_total: int = 0
    excluded_same_alliance: int = 0
    excluded_treaty: int = 0
    no_activity_data: int = 0
    unreadable: int = 0
    treaty_alliance_ids: set = field(default_factory=set)
    treaty_lookup_failed: bool = False


async def find_spy_suspect_report(pw_client, victim, detected_at=None):
    """
    Build a full, ranked report of nations that could have spied on `victim`.

    Rules:
    1. Search every non-vacation nation in the real spy range (0.4x-2.5x).
    2. Exclude the victim's own alliance and active treaty partners (the game
       blocks ops between them, so they cannot be the attacker).
    3. Rank by how recently each nation was active. Activity is a ranking signal,
       never an exclusion: nations are split into "recent" (15 min), "earlier"
       (15-60 min) and "older" (counted, not listed).
    4. Count every nation left out so nothing disappears silently.
    """
    detected_at = detected_at or datetime.now(timezone.utc)

    victim_id = victim.get("id")
    try:
        victim_score = float(victim.get("score") or 0)
        victim_alliance_id = int(victim.get("alliance_id"))
    except (TypeError, ValueError):
        raise ValueError("Victim score or alliance ID is invalid.")

    if victim_score <= 0:
        raise ValueError("Victim score is missing or invalid.")

    if not victim_id:
        raise ValueError("Victim nation ID is missing.")

    min_score, max_score = attacker_score_bounds(victim_score)

    report = SuspectReport(
        detected_at=detected_at,
        victim_id=int(victim_id),
        victim_score=victim_score,
        min_score=min_score,
        max_score=max_score,
    )

    # Victim's own alliance plus active treaty partners.
    excluded_alliances = {victim_alliance_id}

    try:
        treaties = await pw_client.get_alliance_treaties(victim_alliance_id)
    except Exception:
        # Be inclusive if the treaty lookup fails: more candidates, flagged in the report.
        treaties = []
        report.treaty_lookup_failed = True

    for treaty in treaties:
        turns_left = treaty.get("turns_left")

        try:
            active = (
                turns_left is not None
                and int(turns_left) > 0
            )
        except (TypeError, ValueError):
            active = False

        other_id = treaty.get("other_alliance_id")

        if active and other_id:
            excluded_alliances.add(int(other_id))
            report.treaty_alliance_ids.add(int(other_id))

    # Must retrieve every pagination page.
    candidates = await pw_client.get_nations_in_score_range(
        min_score,
        max_score
    )

    for candidate in candidates:
        try:
            candidate_id = candidate.get("id")

            if str(candidate_id) == str(victim_id):
                continue

            report.in_range_total += 1

            alliance_id = candidate.get("alliance_id")

            if alliance_id not in (None, ""):
                alliance_int = int(alliance_id)

                if alliance_int in excluded_alliances:
                    if alliance_int == victim_alliance_id:
                        report.excluded_same_alliance += 1
                    else:
                        report.excluded_treaty += 1
                    continue

            last_active = parse_last_active(candidate.get("last_active"))

            if not last_active:
                report.no_activity_data += 1
                continue

            # A negative age only means the game clock is slightly ahead of ours.
            age_seconds = max(
                0.0,
                (detected_at - last_active).total_seconds()
            )

            candidate["_last_active_dt"] = last_active
            candidate["_age_seconds"] = age_seconds

            if age_seconds <= RECENT_WINDOW_SECONDS:
                candidate["_tier"] = "recent"
                report.recent.append(candidate)
            elif age_seconds <= EARLIER_WINDOW_SECONDS:
                candidate["_tier"] = "earlier"
                report.earlier.append(candidate)
            else:
                report.older_count += 1

        except (TypeError, ValueError):
            report.unreadable += 1
            continue

    report.recent.sort(key=lambda n: n["_last_active_dt"], reverse=True)
    report.earlier.sort(key=lambda n: n["_last_active_dt"], reverse=True)

    return report


async def find_possible_spy_suspects(pw_client, victim):
    """
    Backwards-compatible entry point (used by the Federal Sphere cog).

    Returns the "recent" tier: nations in the real spy range that were active within
    15 minutes of the alert, most recently active first.
    """
    report = await find_spy_suspect_report(pw_client, victim)
    return report.recent


def format_suspect_line(nation) -> str:
    name = str(nation.get("nation_name") or "Unknown")
    name = name.replace("[", "(").replace("]", ")")
    alliance = (nation.get("alliance") or {}).get("name") or "None"
    alliance_id = nation.get("alliance_id") or "None"

    try:
        score = f"{float(nation.get('score') or 0):,.0f}"
    except (TypeError, ValueError):
        score = "?"

    return (
        f"[{name}](https://politicsandwar.com/nation/id={nation.get('id')}) "
        f"— *{alliance}* (`{alliance_id}`) "
        f"— {score} score "
        f"— {format_activity_age(nation.get('last_active'))}"
    )


def chunk_lines(lines, max_chars: int = 3800):
    """Group lines into text blocks that each fit inside one embed description."""
    block = []
    size = 0

    for line in lines:
        line = line[:600]
        extra = len(line) + 1

        if block and size + extra > max_chars:
            yield "\n".join(block)
            block = []
            size = 0

        block.append(line)
        size += extra

    if block:
        yield "\n".join(block)
