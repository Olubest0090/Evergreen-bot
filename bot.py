"""
Evergreen MILCOM Bot — entry point.

Deployed on Render as a Background Worker so it stays connected 24/7
without the sleep-on-idle behavior of Render's free Web Service tier.
"""

import asyncio
import logging
import os

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils import database

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("evergreen-bot")

INTENTS = discord.Intents.default()
INTENTS.members = True

GUILD_ID = os.environ.get("GUILD_ID")
TEST_GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

INITIAL_COGS = (
    "cogs.govrole",
)


class EvergreenBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        await database.init_client()
        log.info("Supabase client ready")

        self.http_session = aiohttp.ClientSession()

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
