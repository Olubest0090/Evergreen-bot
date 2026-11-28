"""Repeat suspects: nations that show up again in a member's earlier spy reports.

When a member loses spies, the alert lists nations active in the 15 minutes before
it. This compares that list with the SAME member's previous spy reports (saved in
spy_incidents) and picks out nations that keep appearing.

Only the "recent" tier (active within 15 min before the alert) is compared, in
both the current and the earlier reports, so it matches the list people see.
"""
from __future__ import annotations

from utils import database

HISTORY_REPORTS = 2
MAX_LINES = 15


async def fetch_previous_incidents(guild_id: int, victim_nation_id: int, limit: int = HISTORY_REPORTS) -> list[dict]:
    """The member's most recent saved spy reports, newest first."""
    if database._session is None or not database._base_url:
        raise RuntimeError("Supabase database client has not been initialized")
    params = {
        "guild_id": f"eq.{int(guild_id)}",
        "victim_nation_id": f"eq.{int(victim_nation_id)}",
        "select": "id,detected_at,candidates",
        "order": "detected_at.desc",
        "limit": str(int(limit)),
    }
    async with database._session.get(database._base_url + "/rest/v1/spy_incidents", params=params) as r:
        if r.status != 200:
            raise RuntimeError(f"Supabase error {r.status}: {(await r.text())[:200]}")
        return await r.json()


def _recent_ids(incident: dict) -> set[str]:
    return {
        str(c["id"])
        for c in (incident.get("candidates") or [])
        if c.get("tier") == "recent" and c.get("id") is not None
    }


def find_repeat_suspects(report, previous: list[dict]) -> dict:
    """Compare this report's recent list with the earlier reports.

    Returns {"compared": number of earlier reports,
             "all":  nations in this alert AND every earlier report,
             "some": [(nation, how many earlier reports)] in this alert and only some of them}.
    Order follows the alert's own list (most recently active first).
    """
    past = [_recent_ids(p) for p in previous]
    result = {"compared": len(past), "all": [], "some": []}
    if not past:
        return result
    for nation in report.recent:
        hits = sum(1 for ids in past if str(nation.get("id")) in ids)
        if hits == len(past):
            result["all"].append(nation)
        elif hits:
            result["some"].append((nation, hits))
    return result
