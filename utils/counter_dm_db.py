"""Log of the counter-request DMs the bot sends (table: counter_dm_log).

Lets MA send a correction to exactly the members who were asked to counter a
war, if the request turns out to have been sent in error.
"""
from __future__ import annotations

from datetime import datetime, timezone

from utils import database


def _base() -> str:
    if database._session is None or not database._base_url:
        raise RuntimeError("Supabase database client has not been initialized")
    return database._base_url + "/rest/v1"


async def _check(resp, allowed=(200, 201, 204)):
    if resp.status not in allowed:
        raise RuntimeError(f"Supabase error {resp.status}: {(await resp.text())[:300]}")


async def log_sent(guild_id: int, war_id: int, nation_id: int, nation_name: str, discord_id: int) -> None:
    payload = {
        "guild_id": int(guild_id),
        "war_id": int(war_id),
        "nation_id": int(nation_id),
        "nation_name": nation_name,
        "discord_id": int(discord_id),
    }
    async with database._session.post(
        _base() + "/counter_dm_log", json=payload, headers={"Prefer": "return=minimal"}
    ) as r:
        await _check(r)


async def list_unretracted(guild_id: int, war_id: int) -> list[dict]:
    params = {
        "guild_id": f"eq.{int(guild_id)}",
        "war_id": f"eq.{int(war_id)}",
        "retracted_at": "is.null",
        "select": "id,nation_id,nation_name,discord_id",
    }
    async with database._session.get(_base() + "/counter_dm_log", params=params) as r:
        await _check(r, (200,))
        return await r.json()


async def mark_retracted(row_ids: list[int]) -> None:
    if not row_ids:
        return
    async with database._session.patch(
        _base() + "/counter_dm_log",
        params={"id": f"in.({','.join(str(int(i)) for i in row_ids)})"},
        json={"retracted_at": datetime.now(timezone.utc).isoformat()},
    ) as r:
        await _check(r)
