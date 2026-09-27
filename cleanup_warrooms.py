"""
Deletes ONLY categories with these EXACT names (case-insensitive) and
everything inside them. No pattern matching, no regex — exact string
comparison only, to prevent any repeat of the earlier incident.
"""
import asyncio
import os

import discord
from dotenv import load_dotenv

load_dotenv()

GUILD_ID = int(os.environ["GUILD_ID"])

# EXACT category names to delete — nothing else, no matter what.
TARGET_CATEGORY_NAMES = {"warcat-0", "warcat 1-5"}

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

        print(f"Connected to guild: {guild.name} (ID: {guild.id})")

        all_channels = await guild.fetch_channels()
        categories = [c for c in all_channels if isinstance(c, discord.CategoryChannel)]
        text_channels = [c for c in all_channels if isinstance(c, discord.TextChannel)]

        target_categories = [c for c in categories if c.name.strip().lower() in TARGET_CATEGORY_NAMES]

        print(f"\nMatched EXACTLY these categories (and only these will be touched):")
        for cat in target_categories:
            channels_in_cat = [ch for ch in text_channels if ch.category_id == cat.id]
            print(f"  - '{cat.name}' ({len(channels_in_cat)} channels)")

        if not target_categories:
            print("\nNo exact matches found. Nothing deleted.")
            await client.close()
            return

        confirm = input("\nType YES to proceed with deletion: ")
        if confirm.strip() != "YES":
            print("Cancelled — nothing deleted.")
            await client.close()
            return

        deleted_channels = 0
        deleted_categories = 0
        for cat in target_categories:
            channels_in_cat = [ch for ch in text_channels if ch.category_id == cat.id]
            for channel in channels_in_cat:
                print(f"Deleting channel: {channel.name}")
                try:
                    await channel.delete(reason="War room cleanup")
                    deleted_channels += 1
                except discord.HTTPException as e:
                    print(f"  Failed: {e}")
                await asyncio.sleep(1)

            print(f"Deleting category: {cat.name}")
            try:
                await cat.delete(reason="War room cleanup")
                deleted_categories += 1
            except discord.HTTPException as e:
                print(f"  Failed: {e}")
            await asyncio.sleep(1)

        print(f"\nDone. Deleted {deleted_channels} channels, {deleted_categories} categories.")
        await client.close()

    await client.start(os.environ["DISCORD_TOKEN"])


asyncio.run(main())
