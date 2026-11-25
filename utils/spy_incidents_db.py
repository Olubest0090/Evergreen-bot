"""Stores one snapshot per detected spy loss so investigations can use the data
as it was at the time, not as it is days later.

Table: public.spy_incidents (see sql/003_spy_incidents.sql).
"""

from utils import database

MAX_STORED_CANDIDATES = 400


def _candidate_row(nation: dict) -> dict:
    last = nation.get("_last_active_dt")
    return {
        "id": nation.get("id"),
        "name": nation.get("nation_name"),
        "alliance_id": nation.get("alliance_id"),
        "alliance": (nation.get("alliance") or {}).get("name"),
        "score": nation.get("score"),
        "last_active": last.isoformat() if last else nation.get("last_active"),
        "tier": nation.get("_tier"),
    }


async def save_spy_incident(guild_id, victim: dict, report, spies_before: int, spies_after: int) -> None:
    candidates = [
        _candidate_row(n)
        for n in (report.recent + report.earlier)[:MAX_STORED_CANDIDATES]
    ]

    payload = {
        "guild_id": int(guild_id),
        "victim_nation_id": int(victim["id"]),
        "victim_name": victim.get("nation_name"),
        "victim_alliance_id": victim.get("alliance_id"),
        "victim_score": report.victim_score,
        "spies_before": int(spies_before),
        "spies_after": int(spies_after),
        "detected_at": report.detected_at.isoformat(),
        "range_min": report.min_score,
        "range_max": report.max_score,
        "in_range_total": report.in_range_total,
        "candidates": candidates,
    }

    url = f"{database._base_url}/rest/v1/spy_incidents"
    headers = {"Prefer": "return=minimal"}

    async with database._session.post(url, json=payload, headers=headers) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"Supabase error {resp.status}: {await resp.text()}")
