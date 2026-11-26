"""Run with:  python tests/test_audit_checks.py   (or pytest)"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import audit_checks as c  # noqa: E402

FULL_CITY = {"barracks": 5, "factory": 5, "hangar": 5, "drydock": 3}


def nation(cities, **units):
    base = {"id": 1, "nation_name": "Test [X]", "alliance_position": "MEMBER",
            "vacation_mode_turns": 0, "cities": cities}
    base.update(units)
    return base


def test_full_military_is_compliant():
    n = nation([FULL_CITY] * 2, soldiers=30000, tanks=2500, aircraft=150, ships=30)
    assert c.improvement_gaps(n) == {}
    assert c.military_status(n)["is_full"]
    assert c.below_military([n]) == []


def test_gaps_summed_over_cities():
    n = nation([{"barracks": 3, "factory": 5, "hangar": 4, "drydock": 3}, FULL_CITY])
    assert c.improvement_gaps(n) == {"barracks": 2, "hangar": 1}
    assert c.format_gaps(c.improvement_gaps(n)) == "2× Barracks, 1× Hangar"


def test_partial_military_floors_percent():
    n = nation([FULL_CITY], soldiers=14999, tanks=1250, aircraft=75, ships=15)
    st = c.military_status(n)
    assert not st["is_full"]
    assert c.percent(st["per_unit"]["soldiers"]) == 99
    assert len(c.below_military([n])) == 1


def test_threshold_filter():
    n = nation([FULL_CITY], soldiers=7500, tanks=625, aircraft=37, ships=7)  # ~50%
    assert len(c.below_military([n], 40)) == 0
    assert len(c.below_military([n], 60)) == 1


def test_split_members_drops_applicants_and_vm():
    ns = [nation([]), {**nation([]), "alliance_position": "APPLICANT"},
          {**nation([]), "vacation_mode_turns": 12}]
    active, vac = c.split_members(ns)
    assert len(active) == 1 and len(vac) == 1


def test_activity_buckets():
    now = datetime(2026, 11, 27, 1, 0, tzinfo=timezone.utc)
    ns = [{"last_active": "2026-11-27T00:30:00+00:00"},
          {"last_active": "2026-11-25 01:00:00"},
          {"last_active": "2026-11-22T01:00:00+00:00"},
          {"last_active": "2026-11-10T01:00:00+00:00"},
          {"last_active": None}]
    r = c.activity_report(ns, now)["counts"]
    assert r == {"day": 1, "three": 1, "week": 1, "inactive": 1, "unknown": 1}


def test_link_escapes_brackets():
    assert "[" not in c.nation_link({"id": 5, "nation_name": "A[B]"}).split("](")[0][1:]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
