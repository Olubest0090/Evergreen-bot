"""
Evergreen MILCOM Bot — entry point.

Deployed on Render as a free Web Service. Render's free tier only sleeps
web services after 15 minutes with no HTTP traffic — so this file runs
a tiny web server alongside the Discord bot purely to answer keep-alive
pings (from UptimeRobot or similar) and stop it from sleeping.
"""

import asyncio
import logging
import os

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils import database
from utils.pw_api import PWApiClient

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("evergreen-bot")

INTENTS = discord.Intents.default()
INTENTS.presences = True
INTENTS.members = True
INTENTS.message_content = True

GUILD_ID = os.environ.get("GUILD_ID")
TEST_GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
PORT = int(os.environ.get("PORT", 8080))

INITIAL_COGS = (
    "cogs.govrole",
    "cogs.link",
    "cogs.alerts",
    "cogs.coalitions",
    "cogs.warroom",
    "cogs.counter",
    "cogs.raid",
    "cogs.ai",
)


class EvergreenBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.http_session: aiohttp.ClientSession | None = None
        self.pw_client: PWApiClient | None = None

    async def setup_hook(self) -> None:
        await database.init_client()
        log.info("Supabase client ready")

        self.http_session = aiohttp.ClientSession()
        self.pw_client = PWApiClient(self.http_session)

        for cog in INITIAL_COGS:
            await self.load_extension(cog)
            log.info("Loaded cog: %s", cog)

        if TEST_GUILD:
            self.tree.copy_global_to(guild=TEST_GUILD)
            await self.tree.sync(guild=TEST_GUILD)
            log.info("Synced commands to guild %s (instant)", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally (can take up to ~1 hour to appear)")

        asyncio.create_task(self._start_webserver())

    async def _start_webserver(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info("Keep-alive web server listening on port %s", PORT)

    async def _health_check(self, request: web.Request) -> web.Response:
        status = "connected" if self.is_ready() else "starting"
        return web.Response(text=f"Evergreen MILCOM bot: {status}")

    async def close(self) -> None:
        if self.http_session:
            await self.http_session.close()
        await database.close_client()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)


async def main():
    bot = EvergreenBot()
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    asyncio.run(main())
