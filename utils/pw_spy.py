"""Probe for the P&W API's spy counters (spy_kills / spy_casualties).

Locutus matches the attacker by watching their spy_kills counter rise by the same amount
the victim lost. Before building the same thing, this checks whether Evergreen's API key
can actually read those fields for a given nation.
"""

import asyncio

BASE_QUERY = """
query($id: [Int]) {
  nations(id: $id, first: 1) {
    data {
      id
      nation_name
      alliance_id
      alliance { name }
      score
      spies
      last_active
    }
  }
}
"""

COUNTER_QUERY = """
query($id: [Int]) {
  nations(id: $id, first: 1) {
    data {
      id
      spy_kills
      spy_casualties
    }
  }
}
"""


async def probe_spy_counters(pw_client, nation_id: int) -> dict:
    """Run the two queries separately so a denied field cannot hide the rest."""
    result = {"base": None, "base_error": None, "counters": None, "counter_error": None}

    try:
        data = await pw_client._query(BASE_QUERY, {"id": [int(nation_id)]})
        rows = (data.get("nations") or {}).get("data") or []
        result["base"] = rows[0] if rows else None
    except Exception as exc:
        result["base_error"] = str(exc)

    # Small gap so the second query does not land straight after the first.
    await asyncio.sleep(2)

    try:
        data = await pw_client._query(COUNTER_QUERY, {"id": [int(nation_id)]})
        rows = (data.get("nations") or {}).get("data") or []
        result["counters"] = rows[0] if rows else None
    except Exception as exc:
        result["counter_error"] = str(exc)

    return result


def _is_rate_limited(error) -> bool:
    return bool(error) and "rate limit" in str(error).lower()


def describe_spy_probe(result: dict) -> str:
    lines = []

    if _is_rate_limited(result.get("base_error")) or _is_rate_limited(result.get("counter_error")):
        return (
            "**Inconclusive: the Politics & War API rate limited this test.**\n"
            "That says nothing about whether spy_kills is readable. "
            "Wait a couple of minutes and run the command again."
        )

    base = result.get("base")
    if result.get("base_error"):
        lines.append(f"Basic lookup failed: `{result['base_error'][:300]}`")
    elif base is None:
        lines.append("Nation not found.")
    else:
        lines.append(
            f"**{base.get('nation_name')}** (`{base.get('id')}`) — "
            f"{(base.get('alliance') or {}).get('name') or 'No alliance'}"
        )
        lines.append(f"Score: {base.get('score')} | Spies field: `{base.get('spies')}`")

    counters = result.get("counters")
    if result.get("counter_error"):
        lines.append(
            f"\n**spy_kills / spy_casualties: NOT readable.**\n`{result['counter_error'][:400]}`"
        )
    elif counters is None:
        lines.append("\nCounter query returned no nation.")
    else:
        lines.append(
            f"\n**spy_kills:** `{counters.get('spy_kills')}` | "
            f"**spy_casualties:** `{counters.get('spy_casualties')}`"
        )
        if counters.get("spy_kills") is None and counters.get("spy_casualties") is None:
            lines.append("Both are null: the API accepts the fields but hides the values for this nation.")
        else:
            lines.append("Readable: attacker-side matching is possible for this nation.")

    return "\n".join(lines)
