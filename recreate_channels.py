"""
Recreates the categories/channels that were accidentally deleted, based
on the exact names printed by the earlier cleanup script's log. Names
only — no message history, permissions, or topics can be restored.
"""
import asyncio
import os

import discord
from dotenv import load_dotenv

load_dotenv()

GUILD_ID = int(os.environ["GUILD_ID"])

NEW_CATEGORIES = {
    "╔═▬▬๑ Milcom Staff ๑▬▬═╗": [
        "high-gov", "high-gov-bot", "announcements", "milcom-staff", "milcom-bot", "gov-to-milcom",
    ],
    "╔═▬▬๑ WAR ALERTS ๑▬▬═╗": [
        "〖🪖〗offensive-wars", "〖🪖〗defensive-wars", "〖🪖〗spy-tracking",
        "〖🪖〗militarization", "〖🪖〗activity",
    ],
}

EXISTING_CATEGORY_ADDITIONS = {
    "╔═▬▬๑ Military Affairs ๑▬▬═╗": ["〖🪖〗ask-a-question", "〖🏆〗beige-request"],
}

INTENTS = discord.Intents.default()
INTENTS.members = True


async def main():
    client = discord.Client(intents=INTENTS)

    @client.event
    async def on_ready():
        guild = client.get_guild(GUILD_ID)
        if not guild:
            print(f"Guild {GUILD_ID} not found.")
            await client.close()
            return

        print(f"Connected to guild: {guild.name}")

        for cat_name, channel_names in NEW_CATEGORIES.items():
            print(f"\nCreating category: {cat_name}")
            category = await guild.create_category(cat_name)
            await asyncio.sleep(1)
            for ch_name in channel_names:
                print(f"  Creating channel: {ch_name}")
                await guild.create_text_channel(ch_name, category=category)
                await asyncio.sleep(1)

        for cat_name, channel_names in EXISTING_CATEGORY_ADDITIONS.items():
            existing = discord.utils.get(guild.categories, name=cat_name)
            if not existing:
                print(f"\nWARNING: category '{cat_name}' not found — skipping {channel_names}")
                continue
            print(f"\nAdding channels back into existing category: {cat_name}")
            for ch_name in channel_names:
                print(f"  Creating channel: {ch_name}")
                await guild.create_text_channel(ch_name, category=existing)
                await asyncio.sleep(1)

        print("\nDone. Remember: /alerts config needs to be redone for the new channel IDs.")
        await client.close()

    await client.start(os.environ["DISCORD_TOKEN"])


asyncio.run(main())
