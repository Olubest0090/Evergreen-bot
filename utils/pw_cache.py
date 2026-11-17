"""Short-lived cache in front of the P&W API client, used by the AI cog only.

Wraps the existing PWApiClient. Only the heavy per-nation / per-war lookups are
cached (a few seconds to under a minute); every other method passes straight
through to the real client. Other cogs keep using bot.pw_client directly, so
nothing else in the bot changes behaviour.

Why: the AI's stack advice loads every Evergreen member fighting the same enemy
(get_nation + get_war + get_war_attacks each). Without a cache that is a dozen
API calls per mention and trips P&W rate limits.

Returned objects are shared between callers: treat them as read-only.
"""

import asyncio
import time

TTL_NATION = 45.0   # seconds
TTL_WAR = 20.0
TTL_ATTACKS = 20.0
MAX_ENTRIES = 400


class CachedPWClient:
    def __init__(self, client, max_entries: int = MAX_ENTRIES):
        self._client = client
        self._max_entries = max_entries
        self._cache: dict = {}   # key -> (stored_at, value)
        self._locks: dict = {}   # key -> asyncio.Lock (de-dupes simultaneous calls)

    def __getattr__(self, name):
        # Only called for attributes not defined here -> pass through.
        return getattr(self._client, name)

    @property
    def wrapped(self):
        return self._client

    def clear(self) -> None:
        self._cache.clear()

    def _prune(self) -> None:
        if len(self._cache) <= self._max_entries:
            return
        now = time.monotonic()
        longest = max(TTL_NATION, TTL_WAR, TTL_ATTACKS)
        for key in [k for k, (ts, _) in self._cache.items() if now - ts > longest]:
            self._cache.pop(key, None)
        if len(self._cache) > self._max_entries:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])
            for key, _ in oldest[: len(self._cache) - self._max_entries]:
                self._cache.pop(key, None)
        for key in [k for k, lock in self._locks.items() if k not in self._cache and not lock.locked()]:
            self._locks.pop(key, None)

    async def _cached(self, key, ttl: float, factory):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = await factory()  # errors propagate and are never cached
            self._cache[key] = (time.monotonic(), value)
            self._prune()
            return value

    async def get_nation(self, nation_id: int):
        nid = int(nation_id)
        return await self._cached(("nation", nid), TTL_NATION, lambda: self._client.get_nation(nid))

    async def get_war(self, war_id: int):
        wid = int(war_id)
        return await self._cached(("war", wid), TTL_WAR, lambda: self._client.get_war(wid))

    async def get_war_attacks(self, war_id: int):
        wid = int(war_id)
        return await self._cached(("attacks", wid), TTL_ATTACKS, lambda: self._client.get_war_attacks(wid))
