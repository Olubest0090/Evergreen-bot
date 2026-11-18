"""War counter detection — a port of Locutus' WarDB.updateCounter().

For a war W (attacker A in alliance aaA, defender D in alliance aaD):

* If either nation has no alliance -> UNCONTESTED.
* attCoalition = aaA + its treaty partners; defCoalition = aaD + its treaty partners
  (treaty types EXTENSION, MDP, MDOAP, ODP, ODOAP, PROTECTORATE).
* Look at every OTHER war of A or D that started from 5 days before W until W ends. Only wars
  launched by the defender's coalition count (never by the attacker's coalition or outsiders).
    - started BEFORE W, launched by D himself against the attacker's coalition -> W IS_COUNTER
      (D hit A's side first, so A's war on D is a counter).
    - started AFTER W (or same time), with A as the target -> W GETS_COUNTERED.
* Both -> ESCALATION, one of them -> that type, neither -> UNCONTESTED.
* While W is active and was declared in the last 15 minutes, ended wars are ignored.

Differences from Locutus (the P&W API gives no war date): "before / after" is decided by war id,
which only ever increases, and start times are derived from turns_left (2-hour turn precision).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

TURN_SECONDS = 7200
WAR_TURNS = 60
LOOKBACK_TURNS = 60  # Locutus: wars that started up to 5 days before this one


class CounterType(Enum):
    UNCONTESTED = "UNCONTESTED"
    GETS_COUNTERED = "GETS_COUNTERED"
    IS_COUNTER = "IS_COUNTER"
    ESCALATION = "ESCALATION"


# Same wording Locutus' WarCard puts on its embeds (uncontested shows nothing).
COUNTER_LABELS = {
    CounterType.IS_COUNTER: "This is a counter",
    CounterType.GETS_COUNTERED: "This has been countered",
    CounterType.ESCALATION: "This is an escalation",
}

COALITION_TREATIES = frozenset({"EXTENSION", "MDP", "MDOAP", "ODP", "ODOAP", "PROTECTORATE"})


def normalize_treaty_type(raw) -> str:
    """'MDoAP' / 'mdoap' / 'Protectorate' -> 'MDOAP' / 'MDOAP' / 'PROTECTORATE'."""
    return re.sub(r"[^A-Za-z]", "", str(raw or "")).upper()


def current_turn(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // TURN_SECONDS)


def turn_of(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() // TURN_SECONDS)


@dataclass(frozen=True)
class WarRec:
    war_id: int
    att_id: int
    def_id: int
    att_aa: int            # attacker alliance id (0 = none)
    def_aa: int
    active: bool
    start_turn: int | None  # None = unknown


@dataclass
class CounterResult:
    type: CounterType = CounterType.UNCONTESTED
    counters: list[int] = field(default_factory=list)    # later wars that hit the attacker back
    is_counter: list[int] = field(default_factory=list)  # earlier wars that make this a counter


def coalition(alliance_id: int, treaties: Iterable[dict]) -> set[int]:
    """The alliance plus its extension / defensive / protectorate partners."""
    out = {int(alliance_id)}
    for t in treaties or []:
        if normalize_treaty_type(t.get("treaty_type")) in COALITION_TREATIES:
            try:
                out.add(int(t.get("other_alliance_id")))
            except (TypeError, ValueError):
                continue
    return out


def classify_war(
    war: WarRec,
    others: Iterable[WarRec],
    att_coalition: set[int],
    def_coalition: set[int],
    *,
    fresh: bool,
) -> CounterResult:
    """Locutus' updateCounter. `fresh` = W was declared in the last ~15 minutes."""
    if not war.att_aa or not war.def_aa:
        return CounterResult(CounterType.UNCONTESTED)

    counters: list[int] = []
    is_counter: list[int] = []
    nations = {war.att_id, war.def_id}

    for o in others:
        if o.war_id == war.war_id:
            continue
        if o.att_id not in nations and o.def_id not in nations:
            continue
        # Locutus window: started within 5 days before W, and not after W would have ended
        if war.start_turn is not None and o.start_turn is not None:
            if o.start_turn < war.start_turn - LOOKBACK_TURNS or o.start_turn >= war.start_turn + WAR_TURNS:
                continue
        if war.active and not o.active and fresh:
            continue
        # only wars launched from the defender's coalition (never the attacker's) count
        if o.att_aa in att_coalition or o.att_aa not in def_coalition:
            continue
        if o.war_id < war.war_id:  # started earlier
            if o.att_id == war.def_id and o.def_aa in att_coalition:
                is_counter.append(o.war_id)
        elif o.def_id == war.att_id:  # started later (or at the same time)
            counters.append(o.war_id)

    if counters and is_counter:
        kind = CounterType.ESCALATION
    elif counters:
        kind = CounterType.GETS_COUNTERED
    elif is_counter:
        kind = CounterType.IS_COUNTER
    else:
        kind = CounterType.UNCONTESTED
    return CounterResult(kind, sorted(counters), sorted(is_counter))


# ---------------------------------------------------------------------------
# Builders: turn the data the bot already has into WarRec objects
# ---------------------------------------------------------------------------

def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def war_rec_from_api(war: dict, now_turn: int) -> WarRec:
    """A war dict as returned by get_active_wars (attacker / defender sub-objects)."""
    att = war.get("attacker") or {}
    dfn = war.get("defender") or {}
    turns_left = _int(war.get("turns_left"))
    return WarRec(
        war_id=_int(war.get("id")),
        att_id=_int(att.get("id") or war.get("att_id")),
        def_id=_int(dfn.get("id") or war.get("def_id")),
        att_aa=_int(att.get("alliance_id")),
        def_aa=_int(dfn.get("alliance_id")),
        active=turns_left > 0,
        start_turn=(now_turn - (WAR_TURNS - turns_left)) if turns_left > 0 else None,
    )


def war_rec_from_row(row: dict, now: datetime, active_window_seconds: float) -> WarRec | None:
    """A stored sphere_wars row. Start = last_seen turn minus the turns that had elapsed then."""
    try:
        seen = datetime.fromisoformat(str(row["last_seen_at"]).replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    turns_left = _int(row.get("turns_left"))
    age = (now - seen).total_seconds()
    return WarRec(
        war_id=_int(row.get("war_id")),
        att_id=_int(row.get("attacker_nation_id")),
        def_id=_int(row.get("defender_nation_id")),
        att_aa=_int(row.get("attacker_alliance_id")),
        def_aa=_int(row.get("defender_alliance_id")),
        active=turns_left > 0 and age <= active_window_seconds,
        start_turn=turn_of(seen) - (WAR_TURNS - turns_left) if turns_left > 0 else None,
    )


def war_recs_from_nation(nation: dict, now_turn: int) -> list[WarRec]:
    """Active wars from a get_nation result (its offensive_wars / defensive_wars lists)."""
    nid = _int(nation.get("id"))
    naa = _int(nation.get("alliance_id"))
    out: list[WarRec] = []
    for w in nation.get("offensive_wars") or []:
        tl = _int(w.get("turns_left"))
        if tl <= 0:
            continue
        d = w.get("defender") or {}
        out.append(WarRec(_int(w.get("id")), nid, _int(w.get("def_id") or d.get("id")), naa,
                          _int(d.get("alliance_id")), True, now_turn - (WAR_TURNS - tl)))
    for w in nation.get("defensive_wars") or []:
        tl = _int(w.get("turns_left"))
        if tl <= 0:
            continue
        a = w.get("attacker") or {}
        out.append(WarRec(_int(w.get("id")), _int(w.get("att_id") or a.get("id")), nid,
                          _int(a.get("alliance_id")), naa, True, now_turn - (WAR_TURNS - tl)))
    return out
