"""Alliances this server treats as friendly, for the spy-loss suspect list.

The P&W treaty lookup only returns treaties that are active right now. This
server's coalitions (ALLIES and EXTENSION) can name friendly alliances it does
not return: manual entries, other treaty types, or treaties that changed since
the last sync. Reading them here lets the spy report leave those alliances out.
"""
from __future__ import annotations

from utils import database

FRIENDLY_BLOC_TYPES = ("ALLIES", "EXTENSION")


async def fetch_friendly_alliances(guild_id: int) -> dict[int, str]:
    """Return {alliance_id: alliance_name} for this guild's ALLIES + EXTENSION coalitions."""
    if database._session is None or not database._base_url:
        raise RuntimeError("Supabase database client has not been initialized")
    params = {
        "guild_id": f"eq.{int(guild_id)}",
        "bloc_type": f"in.({','.join(FRIENDLY_BLOC_TYPES)})",
        "select": "alliance_id,alliance_name",
    }
    async with database._session.get(database._base_url + "/rest/v1/coalitions", params=params) as r:
        if r.status != 200:
            raise RuntimeError(f"Supabase error {r.status}: {(await r.text())[:200]}")
        rows = await r.json()
    friends: dict[int, str] = {}
    for row in rows:
        if row.get("alliance_id") is not None:
            friends[int(row["alliance_id"])] = row.get("alliance_name")
    return friends
