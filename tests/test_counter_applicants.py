"""Run from the repo root AFTER applying the patch:  python tests/test_counter_applicants.py

Applicants must never be listed or asked to counter, in /counter nation or the
automatic DMs (both use list_candidates). Uses stubs: no Discord or network needed.
"""
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


class _Group:
    def __init__(self, **kw):
        pass

    def command(self, **kw):
        return lambda f: f


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


_commands = _mod("discord.ext.commands", Cog=type("Cog", (), {}), Bot=_Any())
_ext = _mod("discord.ext", commands=_commands)
_d = _mod("discord", app_commands=types.SimpleNamespace(Group=_Group, describe=lambda **k: (lambda f: f)),
          ext=_ext)
_d.__getattr__ = lambda name: _Any()
_mod("utils.database")
_mod("utils.embeds")
_mod("utils.formatting", nation_block=lambda *a: "")
_mod("cogs.link", resolve_nation=None)

from cogs import counter  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
ENEMY = {"id": 900, "score": 1000, "soldiers": 1000, "tanks": 100, "aircraft": 50, "ships": 10}


def member(i, position, soldiers=50000):
    m = {"id": i, "nation_name": f"M{i}", "score": 800, "soldiers": soldiers, "tanks": 0,
         "aircraft": 0, "ships": 0, "last_active": NOW, "offensive_wars": []}
    if position is not None:
        m["alliance_position"] = position
    return m


def test_applicants_are_not_candidates():
    assert not counter.list_candidates(member(1, "APPLICANT"), ENEMY)
    assert not counter.list_candidates(member(2, "applicant"), ENEMY)   # case-insensitive


def test_real_members_still_qualify():
    for pos in ("MEMBER", "OFFICER", "HEIR", "LEADER"):
        assert counter.list_candidates(member(3, pos), ENEMY), pos
    assert counter.list_candidates(member(4, None), ENEMY)               # missing position: not hidden


def test_rank_for_the_auto_dm_skips_applicants_and_keeps_order():
    members = [member(10, "APPLICANT", soldiers=999999),   # strongest, but an applicant
               member(11, "MEMBER", soldiers=90000),
               member(12, "OFFICER", soldiers=80000),
               member(13, "APPLICANT", soldiers=70000)]
    ranked = counter.rank_counter_candidates(members, ENEMY)
    assert [m["id"] for m in ranked] == [11, 12]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
