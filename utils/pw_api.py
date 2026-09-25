"""
Thin wrapper around the Politics & War v3 GraphQL API.
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
              alliance_id
              alliance { name }
              cities { id, infrastructure }
              soldiers
              tanks
              aircraft
              ships
              spies
              vacation_mode_turns
              beige_turns
              last_active
            }
          }
        }
        """
        data = await self._query(query, {"id": [nation_id]})
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
                score
                soldiers
                tanks
                aircraft
                ships
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [alliance_id]})
        alliances = data["alliances"]["data"]
        return alliances[0]["nations"] if alliances else []
