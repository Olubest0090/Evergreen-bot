"""Database helpers for AI reminders and per-person timezones (Supabase REST).

Uses the shared session from utils.database, like utils.sphere_db. Every call raises
RuntimeError on a bad response (for example when the migration has not been run yet), and
callers decide how to report that.
"""
from __future__ import annotations

from datetime import datetime

from utils import database


def _base():
    if database._session is None or not database._base_url:
        raise RuntimeError("Supabase database client has not been initialized")
    return database._base_url + "/rest/v1"


async def _check(resp, allowed=(200, 201, 204)):
    if resp.status not in allowed:
        raise RuntimeError(f"Supabase error {resp.status}: {(await resp.text())[:300]}")


async def insert_reminder(
    *, guild_id: int, channel_id: int, requester_id: int, kind: str,
    target_ids: list[int], message: str, due_at: datetime,
) -> int:
    """Store a pending reminder and return its id."""
    payload = {
        "guild_id": guild_id, "channel_id": channel_id, "requester_id": requester_id,
        "kind": kind, "target_ids": target_ids, "message": message,
        "due_at": due_at.isoformat(), "status": "pending",
    }
    async with database._session.post(
        _base() + "/ai_reminders", json=payload, headers={"Prefer": "return=representation"}
    ) as r:
        await _check(r)
        rows = await r.json()
        return int(rows[0]["id"])


async def list_pending() -> list[dict]:
    """Every pending reminder (loaded once at startup, then tracked in memory)."""
    params = {"status": "eq.pending", "select": "*", "order": "due_at.asc", "limit": "1000"}
    async with database._session.get(_base() + "/ai_reminders", params=params) as r:
        await _check(r, (200,))
        return await r.json()


async def set_status(reminder_id: int, status: str) -> None:
    if status not in {"done", "cancelled", "failed", "missed"}:
        raise ValueError("unsupported reminder status")
    async with database._session.patch(
        _base() + "/ai_reminders", params={"id": f"eq.{int(reminder_id)}"}, json={"status": status}
    ) as r:
        await _check(r)


async def get_timezone(user_id: int) -> str | None:
    params = {"user_id": f"eq.{int(user_id)}", "select": "tz"}
    async with database._session.get(_base() + "/ai_user_timezones", params=params) as r:
        await _check(r, (200,))
        rows = await r.json()
        return rows[0]["tz"] if rows else None


async def set_timezone(user_id: int, tz_name: str) -> None:
    payload = {"user_id": int(user_id), "tz": tz_name, "updated_at": datetime.utcnow().isoformat() + "Z"}
    async with database._session.post(
        _base() + "/ai_user_timezones?on_conflict=user_id",
        json=payload, headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    ) as r:
        await _check(r)
