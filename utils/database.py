"""
Database layer for the Evergreen MILCOM bot.

Talks to Supabase's REST API (PostgREST) over plain HTTPS instead of a
raw Postgres driver — no C extensions to compile, works fine on Termux.
"""

import os
import aiohttp

_session: aiohttp.ClientSession | None = None
_base_url: str = ""


async def init_client() -> None:
    """Create the HTTP session used for all Supabase calls.
    Call this once from bot.py's setup_hook before anything else runs.
    """
    global _session, _base_url
    _base_url = os.environ["SUPABASE_URL"].rstrip("/")
    service_key = os.environ["SUPABASE_SERVICE_KEY"]
    _session = aiohttp.ClientSession(
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
    )


async def close_client() -> None:
    if _session:
        await _session.close()


async def set_guild_role(guild_id: int, role_type: str, role_id: int) -> None:
    url = f"{_base_url}/rest/v1/guild_roles"
    payload = {"guild_id": guild_id, "role_type": role_type, "role_id": role_id}
    headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
    async with _session.post(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            text = await resp.text()
            raise RuntimeError(f"Supabase error {resp.status}: {text}")


async def get_guild_role(guild_id: int, role_type: str) -> int | None:
    url = f"{_base_url}/rest/v1/guild_roles"
    params = {
        "guild_id": f"eq.{guild_id}",
        "role_type": f"eq.{role_type}",
        "select": "role_id",
    }
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return data[0]["role_id"] if data else None


async def get_all_guild_roles(guild_id: int) -> dict[str, int]:
    url = f"{_base_url}/rest/v1/guild_roles"
    params = {"guild_id": f"eq.{guild_id}", "select": "role_type,role_id"}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return {row["role_type"]: row["role_id"] for row in data}
