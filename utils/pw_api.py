"""
Thin wrapper around the Politics & War v3 GraphQL API.

NOTE ON FIELD NAMES: if the API rejects a query, the GraphQL error
message will name the exact bad field — check it against the live
schema at https://api.politicsandwar.com/graphql-docs and adjust the
query string here accordingly. Fields marked "best guess" below have
not been verified against the live schema.
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
              offensive_wars {
                id turns_left naval_blockade att_id def_id
                defender { id nation_name alliance_id }
              }
              defensive_wars {
                id turns_left naval_blockade att_id def_id
                attacker { id nation_name alliance_id }
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [nation_id]})
        nations = data["nations"]["data"]
        return nations[0] if nations else None

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
                discord
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [alliance_id]})
        alliances = data["alliances"]["data"]
        return alliances[0]["nations"] if alliances else []

    async def get_alliance_by_id_or_name(self, text: str) -> dict | None:
        text = text.strip()
        if text.isdigit():
            query = """
            query($id: [Int]) {
              alliances(id: $id, first: 1) { data { id name score } }
            }
            """
            data = await self._query(query, {"id": [int(text)]})
        else:
            query = """
            query($name: [String]) {
              alliances(name: $name, first: 1) { data { id name score } }
            }
            """
            data = await self._query(query, {"name": [text]})
        alliances = data["alliances"]["data"]
        return alliances[0] if alliances else None

    async def get_top_alliances(self, limit: int) -> list[dict]:
        """Top alliances by score, used for the DNR top-X threshold.
        NOTE: orderBy syntax is a best guess at the live schema."""
        query = """
        query($limit: Int) {
          alliances(first: $limit, orderBy: [{column: SCORE, order: DESC}]) {
            data { id name score }
          }
        }
        """
        data = await self._query(query, {"limit": limit})
        return data["alliances"]["data"]

    async def get_alliance_treaties(self, alliance_id: int) -> list[dict]:
        """Returns this alliance's active treaties, each tagged with
        the OTHER alliance's id/name regardless of which side of the
        treaty record our alliance sits on.
        NOTE: treaty field names (treaty_type, alliance1/alliance2,
        turns_left) are a best guess — unverified against the live
        schema. If this errors, the message will show the real names."""
        query = """
        query($id: [Int]) {
          alliances(id: $id, first: 1) {
            data {
              id
              treaties {
                id
                treaty_type
                turns_left
                alliance1 { id name }
                alliance2 { id name }
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [alliance_id]})
        alliances = data["alliances"]["data"]
        if not alliances:
            return []

        our_id_str = str(alliance_id)
        results = []
        for t in alliances[0].get("treaties") or []:
            # Don't filter by turns_left here — permanent treaties
            # (the majority) have no countdown and may report 0/null,
            # which is NOT the same as "expired" the way it is for wars.
            a1 = t.get("alliance1") or {}
            a2 = t.get("alliance2") or {}
            # Compare as strings — GraphQL ID scalars often serialize
            # as strings even when queried with an Int variable, so a
            # naive int comparison silently fails every time.
            other = a2 if str(a1.get("id")) == our_id_str else a1
            if other.get("id") and str(other["id"]) != our_id_str:
                results.append({
                    "treaty_type": t.get("treaty_type"),
                    "other_alliance_id": int(other["id"]),
                    "other_alliance_name": other.get("name"),
                })
        return results

    async def get_war(self, war_id: int) -> dict | None:
        """Single war lookup, for the war room pin and /war info."""
        war_fields = """
              id
              war_type
              turns_left
              att_id
              def_id
              att_resistance
              def_resistance
              att_points
              def_points
              attacker {
                id nation_name alliance_id alliance_position alliance { name }
                last_active soldiers tanks aircraft ships spies
              }
              defender {
                id nation_name alliance_id alliance_position alliance { name }
                last_active soldiers tanks aircraft ships spies
              }
        """
        query = f"""
        query($id: [Int]) {{
          wars(id: $id, first: 1) {{
            data {{ {war_fields} }}
          }}
        }}
        """
        data = await self._query(query, {"id": [war_id]})
        wars = data["wars"]["data"]
        return wars[0] if wars else None

    async def get_war_attacks(self, war_id: int) -> list[dict]:
        """Minimal attack feed — kept intentionally small since exact
        WarAttack field names (casualties, loot breakdown, etc.) are
        unverified against the live schema. Expand once this confirmed
        query works, rather than guessing many fields at once."""
        query = """
        query($id: [Int]) {
          wars(id: $id, first: 1) {
            data {
              id
              attacks {
                id
                date
                type
                success
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [war_id]})
        wars = data["wars"]["data"]
        return wars[0].get("attacks") or [] if wars else []

    async def get_nations_in_score_range(self, min_score: float, max_score: float) -> list[dict]:
        query = """
        query($min: Float, $max: Float) {
          nations(min_score: $min, max_score: $max, first: 500, vmode: false) {
            data {
              id
              nation_name
              alliance_id
              alliance { name }
              last_active
              score
            }
          }
        }
        """
        data = await self._query(query, {"min": min_score, "max": max_score})
        return data["nations"]["data"]

    async def get_active_wars(self, alliance_id: int) -> list[dict]:
        """
        IMPORTANT: the wars(alliance_id: ...) filter argument does NOT
        actually filter by alliance on the live API — confirmed by direct
        testing (returned unrelated wars). Do not reintroduce that query.

        Instead, this pulls each member nation's own offensive_wars /
        defensive_wars relation lists (confirmed reliable via /whois)
        nested inside the alliance query, tagging each war with which
        side our member is on.
        """
        war_fields = """
              id
              war_type
              turns_left
              att_id
              def_id
              att_resistance
              def_resistance
              att_points
              def_points
              naval_blockade
              attacker {
                id nation_name alliance_id alliance_position alliance { name }
                last_active soldiers tanks aircraft ships spies
              }
              defender {
                id nation_name alliance_id alliance_position alliance { name }
                last_active soldiers tanks aircraft ships spies
              }
        """
        query = f"""
        query($id: [Int]) {{
          alliances(id: $id, first: 1) {{
            data {{
              id
              nations {{
                id
                alliance_position
                offensive_wars {{ {war_fields} }}
                defensive_wars {{ {war_fields} }}
              }}
            }}
          }}
        }}
        """
        data = await self._query(query, {"id": [alliance_id]})
        alliances = data["alliances"]["data"]
        if not alliances:
            return []

        wars = []
        seen_ids = set()
        for nation in alliances[0]["nations"]:
            position = nation.get("alliance_position")
            for war in (nation.get("offensive_wars") or []):
                if (war.get("turns_left") or 0) <= 0 or war["id"] in seen_ids:
                    continue
                seen_ids.add(war["id"])
                war["_side"] = "offense"
                war["_our_position"] = position
                wars.append(war)
            for war in (nation.get("defensive_wars") or []):
                if (war.get("turns_left") or 0) <= 0 or war["id"] in seen_ids:
                    continue
                seen_ids.add(war["id"])
                war["_side"] = "defense"
                war["_our_position"] = position
                wars.append(war)
        return wars
