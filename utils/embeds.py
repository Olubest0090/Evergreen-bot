"""
Shared embed styling so every command looks like it belongs to the same
bot instead of a patchwork of default discord.py embeds.
"""

import discord
from datetime import datetime, timezone

BRAND_COLOR = 0x2E7D32
SUCCESS_COLOR = 0x2E7D32
ERROR_COLOR = 0xC62828
INFO_COLOR = 0x1565C0
WARNING_COLOR = 0xF9A825

FOOTER_TEXT = "Evergreen MILCOM"


def _base_embed(title: str, description: str, color: int) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def success(title: str, description: str = "") -> discord.Embed:
    return _base_embed(f"✅ {title}", description, SUCCESS_COLOR)


def error(title: str, description: str = "") -> discord.Embed:
    return _base_embed(f"⚠️ {title}", description, ERROR_COLOR)


def info(title: str, description: str = "") -> discord.Embed:
    return _base_embed(f"ℹ️ {title}", description, INFO_COLOR)


def warning(title: str, description: str = "") -> discord.Embed:
    return _base_embed(f"🔶 {title}", description, WARNING_COLOR)
