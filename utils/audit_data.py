"""Fetches the data the alliance audit needs from the P&W API.

Kept separate from utils/pw_api.py on purpose so the existing API wrapper is
untouched. Uses the shared client's query + TTL cache, so /audit run, /roster
and the daily loop share one API call when they overlap.
"""
from __future__ import annotations

from utils.pw_api import PWApiError, PWApiClient

CACHE_SECONDS = 120

_QUERY = """
query($id: [Int]) {
  alliances(id: $id, first: 1) {
    data {
      id
      name
      nations {
        id
        nation_name
        alliance_position
        vacation_mode_turns
        last_active
        soldiers
        tanks
        aircraft
        ships
        cities { barracks factory hangar drydock }
      }
    }
  }
}
"""


async def fetch_alliance(pw: PWApiClient, alliance_id: int) -> tuple[str, list[dict]]:
    """Return (alliance_name, nations). Raises PWApiError if the alliance is unknown."""

    async def factory():
        data = await pw._query(_QUERY, {"id": [int(alliance_id)]})
        rows = data["alliances"]["data"]
        if not rows:
            raise PWApiError(f"No alliance found with ID {alliance_id}")
        return rows[0]["name"], rows[0].get("nations") or []

    name, nations = await pw._cached(("audit", int(alliance_id)), CACHE_SECONDS, factory)
    return name, list(nations)
