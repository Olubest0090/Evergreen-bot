"""Run from the repo root AFTER applying the patch:  python tests/test_counter_pick.py

Stubs out discord and the project's heavy modules so it needs no bot, token or
network, then tests the real cogs/counter.py ranking and the real
cogs/alerts.py counter-DM + MA-notice flow with fake users and channels.
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

    commands = mod("discord.ext.commands", Cog=type("Cog", (), {}), Bot=_Any())
    tasks = mod("discord.ext.tasks", loop=lambda *a, **k: (lambda f: _Any()))
    ext = mod("discord.ext", commands=commands, tasks=tasks)
    d = mod("discord", HTTPException=HTTPException, Forbidden=Forbidden,
            app_commands=_Any(), ext=ext)
    d.__getattr__ = lambda name: _Any()

    db = mod("utils.database")
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
    return db, HTTPException, Forbidden


database, HTTPException, Forbidden = _install_stubs()

from cogs import counter as counter_mod  # noqa: E402
from cogs import alerts as alerts_mod  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
OLD = "2020-01-01T00:00:00+00:00"
ENEMY = {"id": 900, "nation_name": "Enemy", "score": 1000, "soldiers": 1000, "tanks": 100,
         "aircraft": 50, "ships": 10, "alliance": {"name": "Foes"}}


def member(i, soldiers=50000, score=800, last_active=NOW, wars=()):
    return {"id": i, "nation_name": f"M{i}", "score": score, "soldiers": soldiers, "tanks": 0,
            "aircraft": 0, "ships": 0, "last_active": last_active,
            "offensive_wars": [{"turns_left": 5} for _ in range(wars)] if isinstance(wars, int) else list(wars)}


def test_rank_matches_counter_command_rules():
    strong = member(1, soldiers=100000)
    weak = member(2, soldiers=10)                     # loses to the enemy but must still be listed
    full = member(3, wars=5)                          # no free offensive slot
    idle = member(4, last_active=OLD)                 # inactive 7+ days
    far = member(5, score=100)                        # enemy out of range
    defender = member(6)
    ranked = counter_mod.rank_counter_candidates(
        [weak, strong, full, idle, far, defender, ENEMY], ENEMY, exclude_id=6)
    assert [m["id"] for m in ranked] == [1, 2]
    assert not counter_mod.qualifies(weak, ENEMY)     # old strict rule would have dropped this one


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
    """Stands in for utils.pw_api.PWApiClient._query (score lookup)."""

    def __init__(self, fails=False):
        self.fails = fails

    async def _query(self, query, variables=None):
        if self.fails:
            raise RuntimeError("P&W API down")
        return {"nations": {"data": [{"id": variables["id"][0], "score": ENEMY["score"]}]}}


class FakeBot:
    def __init__(self, users, lookup_fails=False):
        self.users, self.channel = users, FakeChannel()
        self.pw_client = FakePW(fails=lookup_fails)

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        return self.users.get(uid)

    def get_channel(self, cid):
        return self.channel


ATTACKER_FROM_WAR = {k: v for k, v in ENEMY.items() if k != "score"}  # real alert data has no score


def run_flow(members, links, closed_dm_ids=(), lookup_fails=False):
    """links: nation_id -> discord_id (missing = unlinked)."""
    users = {d: FakeUser(closed=(n in closed_dm_ids)) for n, d in links.items()}

    async def get_discord_id_for_nation(nid):
        return links.get(nid)

    async def get_guild_role(gid, role):
        return 999

    database.get_discord_id_for_nation = get_discord_id_for_nation
    database.get_guild_role = get_guild_role

    cog = alerts_mod.Alerts.__new__(alerts_mod.Alerts)
    cog.bot = FakeBot(users, lookup_fails=lookup_fails)
    cog._members_by_guild = {1: members}
    war = {"id": 77, "attacker": dict(ATTACKER_FROM_WAR), "defender": member(6, soldiers=1)}
    asyncio.run(cog._dispatch_counter_requests(1, {"ma_notify_channel_id": 5}, war))
    return users, cog.bot.channel.posts


def test_keeps_going_until_five_dm_sent_and_notifies_ma():
    members = [member(i, soldiers=100000 - i) for i in range(10, 20)]  # M10 strongest ... M19
    links = {i: 5000 + i for i in range(11, 20)}                       # M10 unlinked
    users, posts = run_flow(members, links, closed_dm_ids={11})        # M11 has DMs closed
    got_dm = sorted(n for n, d in links.items() if users[d].sent)
    assert got_dm == [12, 13, 14, 15, 16]                              # exactly 5, skipping the 2 failures
    assert len(posts) == 1
    content, embed = posts[0]
    assert content == "<@&999>"                                        # MA pinged because 2 were not reached
    assert embed.kind == "warning"
    for n in (12, 13, 14, 15, 16):
        assert f"M{n}" in embed.description
    assert embed.description.count("DM sent successfully") == 6        # heading + 5 names
    assert "M10 (no linked Discord)" in embed.description
    assert "M11 (DMs closed)" in embed.description
    assert "M17" not in embed.description                              # never went past the 5th success


def test_all_reached_is_a_success_notice_without_ping():
    members = [member(i, soldiers=100000 - i) for i in range(10, 17)]
    links = {i: 5000 + i for i in range(10, 17)}
    users, posts = run_flow(members, links)
    assert sum(1 for u in users.values() if u.sent) == 5
    content, embed = posts[0]
    assert content is None and embed.kind == "success"
    assert "Could not reach" not in embed.description


def test_nobody_qualifies_pings_ma():
    users, posts = run_flow([member(10, score=50)], {10: 5010})
    content, embed = posts[0]
    assert content == "<@&999>"
    assert embed.title == "No Qualifying Counters"
    assert not users[5010].sent


def test_qualified_but_nobody_reachable_pings_ma():
    users, posts = run_flow([member(10), member(11)], {})
    content, embed = posts[0]
    assert content == "<@&999>" and embed.title == "No Counters Reached"
    assert "M10 (no linked Discord)" in embed.description


def test_enemy_score_is_looked_up_when_war_data_has_none():
    """Regression: war alert data has no score, which used to make everyone 'out of range'."""
    members = [member(10), member(11)]
    users, posts = run_flow(members, {10: 5010, 11: 5011})
    assert users[5010].sent and users[5011].sent
    assert posts[0][1].kind == "success"


def test_score_lookup_failure_says_so_instead_of_claiming_nobody_qualifies():
    users, posts = run_flow([member(10)], {10: 5010}, lookup_fails=True)
    content, embed = posts[0]
    assert content == "<@&999>"
    assert embed.title == "Counter Check Failed"
    assert not users[5010].sent


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
