"""Run from the repo root AFTER applying the patch:  python tests/test_spy_allies.py

Uses fakes (no Discord, network or database). Checks that alliances in the
server's ALLIES / EXTENSION coalitions are left out of the spy suspect list and
named in the alert, using the alliances from the real incident (Rose and
The Fighting Pacifists were being listed as suspects).
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
    def __init__(self, rows, status=200):
        self.rows, self.status, self.last_params = rows, status, None

    def get(self, url, params=None):
        self.last_params = params
        return FakeResp(self.status, self.rows)


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
    SAVED.append((a, k))


_stub("utils.spy_incidents_db", save_spy_incident=_save)

from utils import spy_alerts, spy_friends, spy_suspects  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
VICTIM = {"id": 1, "nation_name": "NigeriaA", "score": 4000, "alliance_id": 15590, "spies": 40}


def nation(i, alliance_id, alliance_name):
    return {"id": i, "nation_name": f"N{i}", "score": 5000, "last_active": NOW,
            "alliance_id": alliance_id, "alliance": {"name": alliance_name}}


NATIONS = [
    nation(10, 10659, "Panem"),                  # in-game treaty partner
    nation(11, 790, "Rose"),                     # ALLIES (manual)
    nation(12, 1023, "The Fighting Pacifists"),  # ALLIES (auto)
    nation(13, 15120, "The Black Sheep"),        # EXTENSION
    nation(14, 15590, "Evergreen"),              # victim's own alliance
    nation(15, 4567, "The Golden Horde"),        # real suspect
    nation(16, 619, "Guardian"),                 # real suspect
]


class FakePW:
    async def get_alliance_treaties(self, alliance_id):
        return [{"turns_left": 30, "other_alliance_id": 10659, "other_alliance_name": "Panem"}]

    async def get_nations_in_score_range(self, lo, hi):
        return [dict(n) for n in NATIONS]


FRIENDS = {790: "Rose", 1023: "The Fighting Pacifists", 15120: "The Black Sheep", 15590: "Evergreen"}


def test_without_friends_the_allies_leak_into_the_list():
    """The bug: this is exactly what the old code did."""
    report = asyncio.run(spy_suspects.find_spy_suspect_report(FakePW(), dict(VICTIM)))
    names = {n["alliance"]["name"] for n in report.recent}
    assert "Rose" in names and "The Fighting Pacifists" in names


def test_friends_are_excluded_and_counted():
    report = asyncio.run(spy_suspects.find_spy_suspect_report(
        FakePW(), dict(VICTIM), extra_excluded=FRIENDS))
    names = {n["alliance"]["name"] for n in report.recent}
    assert names == {"The Golden Horde", "Guardian"}
    assert report.excluded_same_alliance == 1
    assert report.excluded_treaty == 4          # Panem, Rose, Fighting Pacifists, Black Sheep
    assert 15590 not in report.treaty_alliances  # victim's own alliance is not listed as a partner
    assert set(report.treaty_alliances) == {10659, 790, 1023, 15120}


def test_summary_names_the_excluded_allies():
    report = asyncio.run(spy_suspects.find_spy_suspect_report(
        FakePW(), dict(VICTIM), extra_excluded=FRIENDS))
    text = spy_alerts._summary_text(report)
    assert "Treaty partners & allies excluded" in text
    for name in ("Rose", "The Fighting Pacifists", "Panem", "The Black Sheep"):
        assert name in text


def test_fetch_friendly_alliances_reads_allies_and_extension():
    database._session = FakeSession([
        {"alliance_id": "790", "alliance_name": "Rose"},
        {"alliance_id": 15120, "alliance_name": "The Black Sheep"},
    ])
    friends = asyncio.run(spy_friends.fetch_friendly_alliances(123))
    assert friends == {790: "Rose", 15120: "The Black Sheep"}
    assert database._session.last_params["guild_id"] == "eq.123"
    assert database._session.last_params["bloc_type"] == "in.(ALLIES,EXTENSION)"


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embeds=None):
        self.sent.append((content, embeds))


def _run_alert(session_rows, session_status=200):
    database._session = FakeSession(session_rows, session_status)
    store = {}

    async def get_last_spies(g, n):
        return 50

    async def set_last_spies(g, n, v):
        store["spies"] = v

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


def test_alert_flow_excludes_allies_from_the_posted_list():
    channel = _run_alert([{"alliance_id": a, "alliance_name": n} for a, n in FRIENDS.items()])
    _, embeds = channel.sent[0]
    listing = embeds[1].description
    assert "Golden Horde" in listing and "Guardian" in listing
    assert "Rose" not in listing and "Fighting Pacifists" not in listing
    summary = embeds[0].fields[0][1]
    assert "Rose" in summary and "The Fighting Pacifists" in summary


def test_alert_still_posts_if_the_coalition_lookup_fails():
    channel = _run_alert([], session_status=500)
    assert len(channel.sent) == 1                   # alert not lost
    assert "Rose" in channel.sent[0][1][1].description  # inclusive fallback, like the treaty-failure rule


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
