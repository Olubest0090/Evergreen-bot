"""Run from the repo root AFTER applying the patch:  python tests/test_spy_repeat.py

Fakes only (no Discord, network or database). Covers the "Repeat suspects" message:
nations from a spy alert that also appeared in the same member's earlier reports.
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeEmbed:
    def __init__(self, kind, title, description=None):
        self.kind, self.title, self.description, self.fields = kind, title, description, []

    def add_field(self, name, value, inline=False):
        self.fields.append((name, value))


class FakeResp:
    def __init__(self, status, rows):
        self.status, self._rows = status, rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._rows

    async def text(self):
        return "error"


class FakeSession:
    """Routes by table so the coalition lookup and the history lookup get their own rows."""

    def __init__(self, incidents=None, coalitions=None, incidents_status=200):
        self.incidents, self.coalitions = incidents or [], coalitions or []
        self.incidents_status = incidents_status
        self.requests = []

    def get(self, url, params=None):
        self.requests.append((url, params))
        if "spy_incidents" in url:
            return FakeResp(self.incidents_status, self.incidents)
        return FakeResp(200, self.coalitions)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class HTTPException(Exception):
    pass


database = _stub("utils.database", _session=None, _base_url="https://x.supabase.co")
_stub("utils.embeds",
      warning=lambda t, d=None: FakeEmbed("warning", t, d),
      info=lambda t, d=None: FakeEmbed("info", t, d))
_stub("discord", HTTPException=HTTPException)
SAVED = []


async def _save(*a, **k):
    SAVED.append(1)


_stub("utils.spy_incidents_db", save_spy_incident=_save)

from utils import spy_alerts, spy_history, spy_suspects  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
VICTIM = {"id": 1, "nation_name": "NigeriaA", "score": 4000, "alliance_id": 15590, "spies": 40}


def nation(i, alliance_id, alliance_name):
    return {"id": i, "nation_name": f"N{i}", "score": 5000, "last_active": NOW,
            "alliance_id": alliance_id, "alliance": {"name": alliance_name}}


NATIONS = [nation(10, 10659, "Panem"), nation(14, 15590, "Evergreen"),
           nation(15, 4567, "Golden Horde"), nation(16, 619, "Guardian"),
           nation(17, 777, "Camelot"), nation(18, 888, "Eclipse")]


class FakePW:
    async def get_alliance_treaties(self, alliance_id):
        return [{"turns_left": 30, "other_alliance_id": 10659, "other_alliance_name": "Panem"}]

    async def get_nations_in_score_range(self, lo, hi):
        return [dict(n) for n in NATIONS]


def cand(i, tier):
    return {"id": str(i), "name": f"N{i}", "tier": tier}


# Earlier reports of this member (as saved in spy_incidents).
REPORT_NEWER = {"id": 8, "detected_at": "2026-11-27T13:00:00+00:00",
                "candidates": [cand(15, "recent"), cand(18, "recent"), cand(16, "earlier")]}
REPORT_OLDER = {"id": 7, "detected_at": "2026-11-27T00:00:00+00:00",
                "candidates": [cand(15, "recent"), cand(16, "recent"), cand(17, "recent"), cand(18, "earlier")]}


def _report():
    return asyncio.run(spy_suspects.find_spy_suspect_report(FakePW(), dict(VICTIM)))


def _ids(items):
    return [n["id"] for n in items]


def test_repeats_across_two_earlier_reports():
    repeat = spy_history.find_repeat_suspects(_report(), [REPORT_NEWER, REPORT_OLDER])
    assert repeat["compared"] == 2
    assert _ids(repeat["all"]) == [15]                         # in this alert AND both earlier reports
    assert sorted(_ids([n for n, _ in repeat["some"]])) == [16, 17, 18]
    assert all(hits == 1 for _, hits in repeat["some"])


def test_only_the_recent_tier_counts():
    """16 was only in the 'earlier' tier of the newer report, 18 only in the older one's."""
    repeat = spy_history.find_repeat_suspects(_report(), [REPORT_NEWER, REPORT_OLDER])
    hits = {n["id"]: h for n, h in repeat["some"]}
    assert hits[16] == 1 and hits[18] == 1


def test_one_earlier_report():
    repeat = spy_history.find_repeat_suspects(_report(), [REPORT_OLDER])
    assert repeat["compared"] == 1
    assert sorted(_ids(repeat["all"])) == [15, 16, 17]        # with one report, "all" = in both
    assert repeat["some"] == []


def test_no_earlier_reports():
    repeat = spy_history.find_repeat_suspects(_report(), [])
    assert repeat == {"compared": 0, "all": [], "some": []}


def test_fetch_asks_for_this_members_last_two_reports_newest_first():
    database._session = FakeSession(incidents=[REPORT_NEWER, REPORT_OLDER])
    rows = asyncio.run(spy_history.fetch_previous_incidents(123, 1))
    assert rows == [REPORT_NEWER, REPORT_OLDER]
    url, params = database._session.requests[0]
    assert url.endswith("/rest/v1/spy_incidents")
    assert params["guild_id"] == "eq.123" and params["victim_nation_id"] == "eq.1"
    assert params["order"] == "detected_at.desc" and params["limit"] == "2"


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embeds=None, embed=None):
        self.sent.append({"content": content, "embeds": embeds, "embed": embed})


def _run_alert(incidents, incidents_status=200):
    SAVED.clear()
    database._session = FakeSession(incidents=incidents, incidents_status=incidents_status)

    async def get_last_spies(g, n):
        return 50

    async def set_last_spies(g, n, v):
        return None

    async def get_discord_id_for_nation(n):
        return None

    database.get_last_spies, database.set_last_spies = get_last_spies, set_last_spies
    database.get_discord_id_for_nation = get_discord_id_for_nation
    channel = FakeChannel()

    class Cog:
        bot = types.SimpleNamespace(pw_client=FakePW())

        async def _get_channel(self, cid):
            return channel

    asyncio.run(spy_alerts.handle_espionage_check(
        Cog(), 1547437707272265791, {"espionage_channel_id": 9}, dict(VICTIM), 15590))
    return channel


def test_alert_is_followed_by_a_separate_repeat_message():
    channel = _run_alert([REPORT_NEWER, REPORT_OLDER])
    assert len(channel.sent) == 2
    assert channel.sent[0]["embeds"] is not None               # the normal alert, untouched
    repeat = channel.sent[1]["embed"]                          # a separate message
    assert repeat.title == "🔁 Repeat suspects"
    text = repeat.description
    assert "both earlier reports" in text and "one earlier report" in text
    assert "N15" in text and "N17" in text
    assert "NigeriaA" in text
    assert SAVED == [1]                                        # snapshot still saved afterwards


def test_history_failure_never_breaks_the_alert():
    channel = _run_alert([], incidents_status=500)
    assert len(channel.sent) == 1                              # only the normal alert
    assert SAVED == [1]                                        # and the snapshot is still saved


def test_first_ever_report_says_there_is_nothing_to_compare():
    channel = _run_alert([])
    assert "No earlier spy reports" in channel.sent[1]["embed"].description


def test_no_overlap_says_so():
    other = {"id": 5, "detected_at": "2026-11-26T00:00:00+00:00", "candidates": [cand(99, "recent")]}
    channel = _run_alert([other])
    assert "No nation active before this alert" in channel.sent[1]["embed"].description


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
