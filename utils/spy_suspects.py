"""Shared spy-loss suspect detection for Evergreen and Federal Sphere."""

from datetime import datetime, timezone

ONLINE_WINDOW_SECONDS = 15 * 60


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


async def find_possible_spy_suspects(pw_client, victim):
    """
    Find possible nations responsible for a spy loss.

    Rules:
    1. Search all nations within the victim's score range.
    2. Exclude the victim's own alliance.
    3. Exclude alliances with active in-game treaties.
    4. Exclude nations not active within the last 15 minutes.
    5. Sort remaining nations by most recent activity.
    """

    victim_id = victim.get("id")
    victim_alliance_id = victim.get("alliance_id")

    try:
        victim_score = float(victim.get("score") or 0)
        victim_alliance_id = int(victim_alliance_id)
    except (TypeError, ValueError):
        raise ValueError("Victim score or alliance ID is invalid.")

    if victim_score <= 0:
        raise ValueError("Victim score is missing or invalid.")

    if not victim_id:
        raise ValueError("Victim nation ID is missing.")

    # Exclude the victim's own alliance.
    excluded_alliances = {victim_alliance_id}

    # Exclude alliances connected by active in-game treaties.
    treaties = await pw_client.get_alliance_treaties(
        victim_alliance_id
    )

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

    # Defensive score range.
    minimum_score = victim_score / 2.5
    maximum_score = victim_score * 2.5

    # This method must retrieve every pagination page.
    candidates = await pw_client.get_nations_in_score_range(
        minimum_score,
        maximum_score
    )

    now = datetime.now(timezone.utc)
    suspects = []

    for candidate in candidates:
        try:
            candidate_id = candidate.get("id")

            if str(candidate_id) == str(victim_id):
                continue

            alliance_id = candidate.get("alliance_id")

            if alliance_id is not None:
                if int(alliance_id) in excluded_alliances:
                    continue

            last_active = parse_last_active(
                candidate.get("last_active")
            )

            if not last_active:
                continue

            age_seconds = (now - last_active).total_seconds()

            # Exclude nations not recently active.
            if age_seconds < 0:
                continue

            if age_seconds > ONLINE_WINDOW_SECONDS:
                continue

            candidate["_last_active_dt"] = last_active

            suspects.append(candidate)

        except (TypeError, ValueError):
            continue

    # Most recently active nations appear first.
    suspects.sort(
        key=lambda nation: nation["_last_active_dt"],
        reverse=True
    )

    return suspects
