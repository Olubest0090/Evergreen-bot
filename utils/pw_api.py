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
        # Lighter query so /whois works again
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
              cities {
                id
                name
                infrastructure
                land
                powered
                coalmine
                oilwell
                uramine
                bauxitemine
                ironmine
                farm
                oilpower
                coalpower
                nuclearpower
                gasrefinery
                steelmill
                hangar
                drydock
                supermarket
                bank
                mall
                stadium
                subway
                hospital
              }
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
              projects
              mass_irrigation
              international_trade_center
              telecommunications_satellite
              urban_planning
              advanced_urban_planning
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
                cities { id }
                pirate_economy
                advanced_pirate_economy
                offensive_wars { turns_left }
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
            a1 = t.get("alliance1") or {}
            a2 = t.get("alliance2") or {}
            other = a2 if str(a1.get("id")) == our_id_str else a1
            if other.get("id") and str(other["id"]) != our_id_str:
                results.append({
                    "treaty_type": t.get("treaty_type"),
                    "other_alliance_id": int(other["id"]),
                    "other_alliance_name": other.get("name"),
                })
        return results

    async def get_war(self, war_id: int) -> dict | None:
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
                att_id
                def_id
                victor
                moneystolen
                loot_info
                infradestroyed
                infra_destroyed_value
                city_infra_before
                improvementslost
                resistance_eliminated
              }
            }
          }
        }
        """
        data = await self._query(query, {"id": [war_id]})
        wars = data["wars"]["data"]
        return (wars[0].get("attacks") or []) if wars else []

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


    async def get_nations_for_raid(self, min_score: float, max_score: float) -> list[dict]:
        """Richer query used by /raid."""
        query = """
        query($min: Float, $max: Float) {
          nations(min_score: $min, max_score: $max, first: 500, vmode: false) {
            data {
              id
              nation_name
              leader_name
              score
              alliance_id
              alliance_position
              alliance { id name }
              last_active
              vacation_mode_turns
              beige_turns
              soldiers
              tanks
              aircraft
              ships
              num_cities
              cities { infrastructure }
              defensive_wars { turns_left }
            }
          }
        }
        """
        data = await self._query(query, {"min": min_score, "max": max_score})
        nations = data["nations"]["data"]
        # Pre-calculate total infra
        for n in nations:
            n["total_infra"] = sum(c.get("infrastructure", 0) or 0 for c in (n.get("cities") or []))
        return nations

    async def get_active_wars(self, alliance_id: int) -> list[dict]:
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
                last_active soldiers tanks aircraft ships spies cities { id }
              }
              defender {
                id nation_name alliance_id alliance_position alliance { name }
                last_active soldiers tanks aircraft ships spies cities { id }
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
