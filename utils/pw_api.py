"""
Thin wrapper around the Politics & War v3 GraphQL API.

NOTE ON FIELD NAMES: if the API rejects a query, the GraphQL error
message will name the exact bad field — check it against the live
schema at https://api.politicsandwar.com/graphql-docs and adjust the
query string here accordingly. War-level fields like att_resistance /
att_points are a best-effort guess at the v3 schema; if get_active_wars
errors, these are the most likely culprits to rename/remove.
"""

import os
import aiohttp

PW_API_URL = "https://api.politicsandwar.com/graphql"


class PWApiError(Exception):
    pass


class PWApiClient:
    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._api_key = os.environ["PNW_API_KEY"]

    async def _query(self, query: str, variables: dict | None = None) -> dict:
        url = f"{PW_API_URL}?api_key={self._api_key}"
        payload = {"query": query, "variables": variables or {}}
        async with self._session.post(url, json=payload) as resp:
            data = await resp.json()
            if "errors" in data:
                raise PWApiError(str(data["errors"]))
            return data["data"]

    async def get_nation(self, nation_id: int) -> dict | None:
        query = """
        query($id: [Int]) {
          nations(id: $id, first: 1) {
            data {
              id
              nation_name
              leader_name
              score
              color
              alliance_id
              alliance_position
              alliance { id name }
              cities { id, infrastructure }
              soldiers
              tanks
              aircraft
              ships
              missiles
              nukes
              spies
              vacation_mode_turns
              beige_turns
              last_active
              pirate_economy
              advanced_pirate_economy
              domestic_policy
              war_policy
              offensive_wars { id turns_left }
              defensive_wars {
                id
                turns_left
                naval_blockade
                att_id
                attacker { id nation_name ships }
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [nation_id]})
        nations = data["nations"]["data"]
        return nations[0] if nations else None

    async def get_active_war_counts(self, nation_id: int) -> tuple[int, int]:
        """Returns (offensive_count, defensive_count) for CURRENTLY ACTIVE
        wars only, by fetching the nation's full war relation lists and
        filtering client-side to turns_left > 0 — the wars(nation_id:...)
        filter is not a valid query argument on the live schema, it
        silently returns zero results instead of erroring."""
        nation = await self.get_nation(nation_id)
        if not nation:
            return 0, 0
        off_wars = nation.get("offensive_wars") or []
        def_wars = nation.get("defensive_wars") or []
        off_count = sum(1 for w in off_wars if (w.get("turns_left") or 0) > 0)
        def_count = sum(1 for w in def_wars if (w.get("turns_left") or 0) > 0)
        return off_count, def_count

    async def get_nation_by_name(self, nation_name: str) -> dict | None:
        query = """
        query($name: [String]) {
          nations(nation_name: $name, first: 1) {
            data {
              id
              nation_name
              leader_name
              score
              alliance_id
              alliance { name }
            }
          }
        }
        """
        data = await self._query(query, {"name": [nation_name]})
        nations = data["nations"]["data"]
        return nations[0] if nations else None

    async def get_alliance_members(self, alliance_id: int) -> list[dict]:
        query = """
        query($id: [Int]) {
          alliances(id: $id, first: 1) {
            data {
              id
              name
              nations {
                id
                nation_name
                leader_name
                alliance_position
                score
                soldiers
                tanks
                aircraft
                ships
                spies
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [alliance_id]})
        alliances = data["alliances"]["data"]
        return alliances[0]["nations"] if alliances else []

    async def get_active_wars(self, alliance_id: int) -> list[dict]:
        query = """
        query($id: [Int]) {
          wars(alliance_id: $id, active: true, first: 100) {
            data {
              id
              date
              war_type
              turns_left
              att_id
              def_id
              att_alliance_id
              def_alliance_id
              att_resistance
              def_resistance
              att_points
              def_points
              attacker {
                id
                nation_name
                alliance_position
                alliance { name }
                last_active
                soldiers
                tanks
                aircraft
                ships
                spies
              }
              defender {
                id
                nation_name
                alliance_position
                alliance { name }
                last_active
                soldiers
                tanks
                aircraft
                ships
                spies
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [alliance_id]})
        return data["wars"]["data"]
