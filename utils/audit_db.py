"""Database helpers for the alliance audit (table: milcom_audit_config).

Uses the shared Supabase session from utils.database, like utils.reminders_db.
"""
from __future__ import annotations

from datetime import datetime, timezone

from utils import database


def _base():
    if database._session is None or not database._base_url:
        raise RuntimeError("Supabase database client has not been initialized")
    return database._base_url + "/rest/v1"


async def _check(resp, allowed=(200, 201, 204)):
    if resp.status not in allowed:
        raise RuntimeError(f"Supabase error {resp.status}: {(await resp.text())[:300]}")


async def get_config(guild_id: int) -> dict | None:
    params = {"guild_id": f"eq.{int(guild_id)}", "select": "*"}
    async with database._session.get(_base() + "/milcom_audit_config", params=params) as r:
        await _check(r, (200,))
        rows = await r.json()
        return rows[0] if rows else None


async def save_config(guild_id: int, alliance_id: int, channel_id: int) -> None:
    """Create or update alliance + channel. Leaves daily_enabled as it was."""
    payload = {
        "guild_id": int(guild_id),
        "alliance_id": int(alliance_id),
        "channel_id": int(channel_id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    async with database._session.post(
        _base() + "/milcom_audit_config?on_conflict=guild_id",
        json=payload,
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    ) as r:
        await _check(r)


async def set_daily(guild_id: int, enabled: bool) -> None:
    async with database._session.patch(
        _base() + "/milcom_audit_config",
        params={"guild_id": f"eq.{int(guild_id)}"},
        json={"daily_enabled": bool(enabled), "updated_at": datetime.now(timezone.utc).isoformat()},
    ) as r:
        await _check(r)


async def list_daily_enabled() -> list[dict]:
    params = {"daily_enabled": "eq.true", "select": "*"}
    async with database._session.get(_base() + "/milcom_audit_config", params=params) as r:
        await _check(r, (200,))
        return await r.json()


async def mark_run(guild_id: int, run_date_iso: str) -> None:
    async with database._session.patch(
        _base() + "/milcom_audit_config",
        params={"guild_id": f"eq.{int(guild_id)}"},
        json={"last_run_date": run_date_iso},
    ) as r:
        await _check(r)
