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


# ---- govrole (Phase 1) ----

async def set_guild_role(guild_id: int, role_type: str, role_id: int) -> None:
    url = f"{_base_url}/rest/v1/guild_roles"
    payload = {"guild_id": guild_id, "role_type": role_type, "role_id": role_id}
    headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
    async with _session.post(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"Supabase error {resp.status}: {await resp.text()}")


async def get_guild_role(guild_id: int, role_type: str) -> int | None:
    url = f"{_base_url}/rest/v1/guild_roles"
    params = {"guild_id": f"eq.{guild_id}", "role_type": f"eq.{role_type}", "select": "role_id"}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return data[0]["role_id"] if data else None


async def get_all_guild_roles(guild_id: int) -> dict[str, int]:
    url = f"{_base_url}/rest/v1/guild_roles"
    params = {"guild_id": f"eq.{guild_id}", "select": "role_type,role_id"}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return {row["role_type"]: row["role_id"] for row in data}


# ---- alerts_config (Phase 2) ----

async def _upsert_alerts_config(guild_id: int, **fields) -> None:
    url = f"{_base_url}/rest/v1/alerts_config?on_conflict=guild_id"
    payload = {"guild_id": guild_id, **fields}
    headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
    async with _session.post(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"Supabase error {resp.status}: {await resp.text()}")


async def set_alliance_id(guild_id: int, alliance_id: int) -> None:
    await _upsert_alerts_config(guild_id, alliance_id=alliance_id)


async def set_alert_channel(guild_id: int, column: str, channel_id: int) -> None:
    await _upsert_alerts_config(guild_id, **{column: channel_id})


async def get_alerts_config(guild_id: int) -> dict | None:
    url = f"{_base_url}/rest/v1/alerts_config"
    params = {"guild_id": f"eq.{guild_id}", "select": "*"}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return data[0] if data else None


async def get_all_alerts_configs() -> list[dict]:
    url = f"{_base_url}/rest/v1/alerts_config"
    params = {"select": "*"}
    async with _session.get(url, params=params) as resp:
        return await resp.json()


# ---- nation_links (Phase 2) ----

async def link_nation(guild_id: int, discord_user_id: int, nation_id: int) -> None:
    url = f"{_base_url}/rest/v1/nation_links?on_conflict=discord_user_id"
    payload = {"discord_user_id": discord_user_id, "nation_id": nation_id, "guild_id": guild_id}
    headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
    async with _session.post(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"Supabase error {resp.status}: {await resp.text()}")


async def unlink_nation(discord_user_id: int) -> None:
    url = f"{_base_url}/rest/v1/nation_links"
    params = {"discord_user_id": f"eq.{discord_user_id}"}
    async with _session.delete(url, params=params) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Supabase error {resp.status}: {await resp.text()}")


async def get_nation_for_user(discord_user_id: int) -> int | None:
    url = f"{_base_url}/rest/v1/nation_links"
    params = {"discord_user_id": f"eq.{discord_user_id}", "select": "nation_id"}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return data[0]["nation_id"] if data else None


async def get_discord_id_for_nation(nation_id: int) -> int | None:
    url = f"{_base_url}/rest/v1/nation_links"
    params = {"nation_id": f"eq.{nation_id}", "select": "discord_user_id"}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return data[0]["discord_user_id"] if data else None


# ---- seen_wars (Phase 2) ----

async def is_war_alerted(war_id: int, field: str) -> bool:
    url = f"{_base_url}/rest/v1/seen_wars"
    params = {"war_id": f"eq.{war_id}", "select": field}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return bool(data and data[0].get(field))


async def mark_war_alerted(war_id: int, field: str) -> None:
    url = f"{_base_url}/rest/v1/seen_wars?on_conflict=war_id"
    payload = {"war_id": war_id, field: True}
    headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
    async with _session.post(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"Supabase error {resp.status}: {await resp.text()}")


# ---- nation_spy_tracking (Phase 2) ----

async def get_last_spies(nation_id: int) -> int | None:
    url = f"{_base_url}/rest/v1/nation_spy_tracking"
    params = {"nation_id": f"eq.{nation_id}", "select": "last_known_spies"}
    async with _session.get(url, params=params) as resp:
        data = await resp.json()
        return data[0]["last_known_spies"] if data else None


async def set_last_spies(nation_id: int, spies: int) -> None:
    url = f"{_base_url}/rest/v1/nation_spy_tracking?on_conflict=nation_id"
    payload = {"nation_id": nation_id, "last_known_spies": spies}
    headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
    async with _session.post(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"Supabase error {resp.status}: {await resp.text()}")
