"""Run from the repo root AFTER applying the patch:  python tests/test_counter_retract.py

Uses fakes (no Discord, network or database). Covers:
  - counter-attack wars send NO counter DMs and no "DMs sent" notice
  - normal defensive wars still DM the top counters, and log each DM
  - /alerts retractcounter: from the log, by nation names, and edge cases
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Any:
    def __getattr__(self, name):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __getitem__(self, k):
        return _Any()


class FakeEmbed:
    def __init__(self, kind, title, description=None):
        self.kind, self.title, self.description = kind, title, description


class FakeChoice:
    def __init__(self, **kw):
        pass

    def __class_getitem__(cls, item):
        return cls


class FakeGroup:
    def __init__(self, **kw):
        pass

    def command(self, **kw):
        return lambda f: f


def _identity(*a, **k):
    return lambda f: f


def _install_stubs():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class HTTPException(Exception):
        pass

    class Forbidden(HTTPException):
        pass

    app_commands = types.SimpleNamespace(
        Group=FakeGroup, Choice=FakeChoice, describe=_identity, choices=_identity)
    commands = mod("discord.ext.commands", Cog=type("Cog", (), {}), Bot=_Any())
    tasks = mod("discord.ext.tasks", loop=lambda *a, **k: (lambda f: _Any()))
    ext = mod("discord.ext", commands=commands, tasks=tasks)
    d = mod("discord", HTTPException=HTTPException, Forbidden=Forbidden,
            app_commands=app_commands, ext=ext)
    d.__getattr__ = lambda name: _Any()

    db = mod("utils.database", _session=None, _base_url="https://x.supabase.co")
    mod("utils.embeds",
        warning=lambda t, d=None: FakeEmbed("warning", t, d),
        success=lambda t, d=None: FakeEmbed("success", t, d),
        error=lambda t, d=None: FakeEmbed("error", t, d),
        info=lambda t, d=None: FakeEmbed("info", t, d))
    mod("utils.formatting", nation_block=lambda *a: "", format_duration=lambda *a: "",
        military_line=lambda *a: "")
    mod("utils.spy_suspects", find_possible_spy_suspects=None)
    mod("utils.spy_alerts", handle_espionage_check=None)
    mod("utils.pw_spy", probe_spy_counters=None, describe_spy_probe=None)
    mod("cogs.link", resolve_nation=None)
    mod("cogs.coalitions", is_dnr_protected=None)
    return db, HTTPException, Forbidden


database, HTTPException, Forbidden = _install_stubs()

from cogs import alerts as alerts_mod  # noqa: E402
from utils import counter_dm_db  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
ENEMY = {"id": 900, "nation_name": "Infernum", "soldiers": 1000, "tanks": 100, "aircraft": 50,
         "ships": 10, "alliance": {"name": "Citadel"}}   # real war data has no score
ENEMY_SCORE = 1000


def member(i, soldiers=50000):
    return {"id": i, "nation_name": f"M{i}", "score": 800, "soldiers": soldiers, "tanks": 0,
            "aircraft": 0, "ships": 0, "last_active": NOW, "offensive_wars": []}


class FakeResp:
    def __init__(self, status=200, rows=None):
        self.status, self._rows = status, rows or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._rows

    async def text(self):
        return ""


class FakeSession:
    def __init__(self, rows=None):
        self.rows, self.posts, self.patches, self.gets = rows or [], [], [], []

    def get(self, url, params=None):
        self.gets.append((url, params))
        return FakeResp(200, self.rows)

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        return FakeResp(201)

    def patch(self, url, params=None, json=None):
        self.patches.append((url, params, json))
        return FakeResp(204)


class FakeUser:
    def __init__(self, closed=False):
        self.closed, self.sent = closed, []

    async def send(self, embed=None):
        if self.closed:
            raise Forbidden("dms closed")
        self.sent.append(embed)


class FakeChannel:
    def __init__(self):
        self.posts = []

    async def send(self, content=None, embed=None):
        self.posts.append((content, embed))


class FakePW:
    async def _query(self, query, variables=None):
        return {"nations": {"data": [{"id": variables["id"][0], "score": ENEMY_SCORE}]}}

    async def get_war(self, war_id):
        return {"id": war_id, "attacker": {"id": 900, "nation_name": "Infernum"},
                "defender": {"id": 6, "nation_name": "Republic of Breckem"}}

    async def get_nation_by_name(self, name):
        known = {"Zenithia": 21, "Aurusmayotte": 22, "Jrex Assault": 23,
                 "Republic of Walcent": 24, "Northern Michigan": 25}
        return {"id": known[name], "nation_name": name} if name in known else None


class FakeBot:
    def __init__(self, users):
        self.users, self.channel, self.pw_client = users, FakeChannel(), FakePW()

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        return self.users.get(uid)

    def get_channel(self, cid):
        return self.channel


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, **kw):
        self.sent.append(embed)


class FakeInteraction:
    def __init__(self):
        async def defer(**kw):
            return None
        self.guild_id = 1
        self.response = types.SimpleNamespace(defer=defer)
        self.followup = Followup()


def make_cog(users, links, members=()):
    async def get_discord_id_for_nation(nid):
        return links.get(nid)

    async def get_guild_role(gid, role):
        return 999

    async def is_war_alerted(*a):
        return False

    async def mark_war_alerted(*a):
        return None

    database.get_discord_id_for_nation = get_discord_id_for_nation
    database.get_guild_role = get_guild_role
    database.is_war_alerted = is_war_alerted
    database.mark_war_alerted = mark_war_alerted
    cog = alerts_mod.Alerts.__new__(alerts_mod.Alerts)
    cog.bot = FakeBot(users)
    cog._members_by_guild = {1: list(members)}
    cog._alerts_sent = []

    async def fake_send_war_alert(guild_id, channel_id, war, side, is_counter=False, is_dnr_violation=False):
        # Stands in for the real channel alert so this test does not depend on its code.
        cog._alerts_sent.append(war["id"])
        return True

    cog._send_war_alert = fake_send_war_alert
    return cog


def defensive_war(war_id=2749275):
    return {"id": war_id, "_side": "defense", "_our_position": "MEMBER",
            "attacker": dict(ENEMY), "defender": member(6, soldiers=1)}


CONFIG = {"defense_channel_id": 5, "ma_notify_channel_id": 5, "auto_counter_enabled": True}


def test_counter_attack_war_sends_no_dms_and_no_notice():
    members = [member(10), member(11)]
    users = {5010: FakeUser(), 5011: FakeUser()}
    cog = make_cog(users, {10: 5010, 11: 5011}, members)
    database._session = FakeSession()
    asyncio.run(cog._handle_war(1, dict(CONFIG), defensive_war(), is_counter=True))
    assert cog._alerts_sent == [2749275]                       # the channel alert still goes out
    assert not users[5010].sent and not users[5011].sent       # but nobody is asked to counter
    assert cog.bot.channel.posts == []                         # and no "Counter DMs Sent" notice
    assert database._session.posts == []                       # and nothing is logged


def test_normal_defensive_war_still_dms_and_logs_each_dm():
    members = [member(10, 90000), member(11, 80000), member(12, 70000)]
    users = {5010: FakeUser(), 5011: FakeUser(), 5012: FakeUser()}
    cog = make_cog(users, {10: 5010, 11: 5011, 12: 5012}, members)
    database._session = FakeSession()
    asyncio.run(cog._handle_war(1, dict(CONFIG), defensive_war(), is_counter=False))
    assert all(u.sent for u in users.values())
    logged = [p[1] for p in database._session.posts]
    assert [(r["war_id"], r["nation_id"], r["discord_id"]) for r in logged] == [
        (2749275, 10, 5010), (2749275, 11, 5011), (2749275, 12, 5012)]
    assert all(r["guild_id"] == 1 for r in logged)


def test_logging_failure_never_blocks_the_dms():
    users = {5010: FakeUser()}
    cog = make_cog(users, {10: 5010}, [member(10)])
    session = FakeSession()

    def broken_post(url, json=None, headers=None):
        raise RuntimeError("db down")
    session.post = broken_post
    database._session = session
    asyncio.run(cog._handle_war(1, dict(CONFIG), defensive_war(), is_counter=False))
    assert users[5010].sent


def _run_retract(cog, war_id, nations=None):
    interaction = FakeInteraction()
    asyncio.run(cog.retract_counter(interaction, war_id, nations))
    return interaction.followup.sent


def test_retract_uses_the_dm_log_and_marks_rows_done():
    users = {5010: FakeUser(), 5011: FakeUser()}
    cog = make_cog(users, {})
    database._session = FakeSession(rows=[
        {"id": 7, "nation_id": 10, "nation_name": "M10", "discord_id": 5010},
        {"id": 8, "nation_id": 11, "nation_name": "M11", "discord_id": 5011}])
    sent = _run_retract(cog, 2749275)
    for u in users.values():
        dm = u.sent[0]
        assert "Do not declare" in dm.description
        assert "request peace immediately" in dm.description
        assert "Infernum" in dm.description and "Republic of Breckem" in dm.description
    assert sent[0].kind == "success" and "2" in sent[0].description
    (url, params, body), = database._session.patches
    assert params == {"id": "in.(7,8)"} and body["retracted_at"]
    _, get_params = database._session.gets[0]
    assert get_params["war_id"] == "eq.2749275" and get_params["retracted_at"] == "is.null"


def test_retract_by_names_for_dms_that_were_never_logged():
    """The real incident: 5 members were DMed before the bot logged anything."""
    names = ["Zenithia", "Aurusmayotte", "Jrex Assault", "Republic of Walcent", "Northern Michigan"]
    links = {21: 5021, 22: 5022, 23: 5023, 24: 5024, 25: 5025}
    users = {d: FakeUser() for d in links.values()}
    cog = make_cog(users, links)
    database._session = FakeSession(rows=[])
    sent = _run_retract(cog, 2749275, ", ".join(names))
    assert all(u.sent and "request peace immediately" in u.sent[0].description for u in users.values())
    assert sent[0].kind == "success"
    assert sent[0].description.count("correction DM sent") == 5
    assert database._session.patches == []              # nothing to mark: no log rows


def test_retract_reports_problems_instead_of_hiding_them():
    links = {21: 5021, 22: 5022}
    users = {5021: FakeUser(), 5022: FakeUser(closed=True)}
    cog = make_cog(users, links)
    database._session = FakeSession(rows=[])
    sent = _run_retract(cog, 5, "Zenithia, Aurusmayotte, Nonexistent Land, Jrex Assault")
    embed = sent[0]
    assert embed.kind == "warning"
    assert "Zenithia" in embed.description and "correction DM sent" in embed.description
    assert "Aurusmayotte" in embed.description and "DMs closed" in embed.description
    assert "Nonexistent Land (nation not found)" in embed.description
    assert "Jrex Assault (no linked Discord)" in embed.description


def test_retract_with_nobody_to_notify_says_so():
    cog = make_cog({}, {})
    database._session = FakeSession(rows=[])
    sent = _run_retract(cog, 5)
    assert sent[0].kind == "error" and sent[0].title == "Nobody To Notify"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
