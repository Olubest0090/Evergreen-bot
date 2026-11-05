import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from utils import database

log = logging.getLogger("evergreen-bot")

# 11:10pm Lagos (UTC+1) on Nov 5 = 22:10 UTC
RUN_AT = datetime(2026, 11, 5, 22, 10, tzinfo=timezone.utc)
CATCHUP_SECONDS = 20 * 60


class CleanupTimer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._task = None

    async def cog_load(self):
        self._task = asyncio.create_task(self._run())

    def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _enemies(self, alliance_id):
        ids = set()
        for w in await self.bot.pw_client.get_active_wars(alliance_id):
            e = w.get("attacker") if w.get("_side") == "defense" else w.get("defender")
            if e and e.get("id"):
                ids.add(int(e["id"]))
        return ids

    async def _run(self):
        try:
            await self.bot.wait_until_ready()
            delay = (RUN_AT - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                log.info("[cleanup_timer] armed, runs in %.0fs", delay)
                await asyncio.sleep(delay)
            elif -delay > CATCHUP_SECONDS:
                return
            await self._cleanup_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[cleanup_timer] failed")

    async def _cleanup_all(self):
        for cfg in await database.get_all_alerts_configs():
            gid, aid = int(cfg["guild_id"]), cfg.get("alliance_id")
            guild = self.bot.get_guild(gid)
            if not guild or not aid:
                continue
            try:  # two fetches; keep a room if the enemy appears in either
                a = await self._enemies(aid)
                await asyncio.sleep(20)
                active = a | await self._enemies(aid)
            except Exception as e:
                log.error("[cleanup_timer] guild %s skipped: %s", gid, e)
                continue
            rooms = await database.get_all_war_rooms(gid)
            if not isinstance(rooms, list):
                log.error("[cleanup_timer] bad rooms response: %s", str(rooms)[:200])
                continue
            closed = 0
            for room in rooms:
                try:
                    eid = int(room["enemy_nation_id"])
                    if eid in active:
                        continue
                    ch = guild.get_channel(int(room["channel_id"]))
                    if ch:
                        try:
                            await ch.delete(reason="Scheduled war room cleanup")
                        except discord.NotFound:
                            pass
                    await database.delete_war_room(gid, eid)
                    closed += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    log.error("[cleanup_timer] room error: %s", e)
            log.info("[cleanup_timer] guild %s: deleted %d room(s)", gid, closed)


async def setup(bot):
    await bot.add_cog(CleanupTimer(bot))
