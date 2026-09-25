"""
/link — connects a Discord member to their Politics & War nation.
/whois — shows a full nation profile card for a linked member.
"""

import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds

NATION_URL_PATTERN = re.compile(r"nation[/=]id=(\d+)|nation/(\d+)")


async def resolve_nation(pw_client, nation_input: str) -> dict | None:
    text = nation_input.strip()

    if text.isdigit():
        return await pw_client.get_nation(int(text))

    url_match = NATION_URL_PATTERN.search(text)
    if url_match:
        nation_id = int(url_match.group(1) or url_match.group(2))
        return await pw_client.get_nation(nation_id)

    return await pw_client.get_nation_by_name(text)


async def user_can_link_others(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    if interaction.permissions.administrator:
        return True

    ma_role_id = await database.get_guild_role(interaction.guild_id, "MA")
    if not ma_role_id:
        return False

    try:
        return any(role.id == ma_role_id for role in interaction.user.roles)
    except AttributeError:
        return False


def format_duration(last_active_iso: str | None) -> str:
    if not last_active_iso:
        return "unknown"
    try:
        last = datetime.fromisoformat(last_active_iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - last
        days, rem = divmod(int(delta.total_seconds()), 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h ago"
        if hours:
            return f"{hours}h {minutes}m ago"
        return f"{minutes}m ago"
    except Exception:
        return "unknown"


def build_nation_embed(nation: dict, off_count: int = 0, def_count: int = 0) -> discord.Embed:
    nation_id = nation["id"]
    name = nation.get("nation_name", "Unknown")
    leader = nation.get("leader_name", "Unknown")
    score = nation.get("score", 0)
    color = (nation.get("color") or "None").title()
    alliance = nation.get("alliance") or {}
    alliance_name = alliance.get("name", "None")
    position = (nation.get("alliance_position") or "None").title()
    num_cities = len(nation.get("cities") or [])

    # Base offensive slots is 5; pirate economy raises it to 6, and
    # advanced pirate economy raises it to 7. Defensive slots are always
    # a fixed 3 regardless of projects.
    if nation.get("advanced_pirate_economy"):
        max_off = 7
    elif nation.get("pirate_economy"):
        max_off = 6
    else:
        max_off = 5
    max_def = 3

    domestic_policy = (nation.get("domestic_policy") or "None").replace("_", " ").title()
    war_policy = (nation.get("war_policy") or "None").replace("_", " ").title()

    status_parts = []
    if nation.get("vacation_mode_turns", 0) > 0:
        status_parts.append(f"🌴 Vacation ({nation['vacation_mode_turns']} turns)")
    if nation.get("beige_turns", 0) > 0:
        status_parts.append(f"🔶 Beige ({nation['beige_turns']} turns)")

    blockading_nations = []
    for war in (nation.get("defensive_wars") or []):
        attacker = war.get("attacker") or {"id": war.get("att_id")}
        # A blockade only holds while the attacker still has ships to
        # enforce it — if their navy was wiped out (by us or by anyone
        # else they're at war with), the blockade is effectively broken
        # even if this war's naval_blockade flag hasn't reset.
        still_has_ships = (attacker.get("ships") or 0) > 0
        if war.get("naval_blockade") and (war.get("turns_left") or 0) > 0 and still_has_ships:
            blockading_nations.append(attacker)
    if blockading_nations:
        links = ", ".join(
            f"[{b.get('nation_name', 'Unknown')}](https://politicsandwar.com/nation/id={b.get('id')})"
            for b in blockading_nations
        )
        status_parts.append(f"🚫 Blockaded by {links}")

    status = " · ".join(status_parts) if status_parts else "✅ Active"

    war_att_low, war_att_high = score * 0.75, score * 2.5
    war_def_low, war_def_high = score / 2.5, score / 0.75
    spy_low, spy_high = score / 2.5, score * 2.5

    embed = embeds.info(name)
    embed.url = f"https://politicsandwar.com/nation/id={nation_id}"
    embed.description = (
        f"**Leader:** {leader}\n"
        f"**Alliance:** {alliance_name} ({position})\n"
        f"**Color Bloc:** {color}\n"
        f"**Domestic Policy:** {domestic_policy} | **War Policy:** {war_policy}\n"
        f"**Cities:** {num_cities} | **Score:** {score:,.2f}\n"
        f"**War Slots:** Offense {off_count}/{max_off} · Defense {def_count}/{max_def}\n"
        f"**Last Active:** {format_duration(nation.get('last_active'))}\n"
        f"**Status:** {status}\n\n"
        f"**Military**\n"
        f"`{nation.get('soldiers', 0):,} 💂 | {nation.get('tanks', 0):,} ⚙️ | "
        f"{nation.get('aircraft', 0):,} ✈️ | {nation.get('ships', 0):,} 🚢 | "
        f"{nation.get('missiles', 0)} 🚀 | {nation.get('nukes', 0)} ☢️ | "
        f"{nation.get('spies', 0):,} 🔍`\n\n"
        f"**War Range (Attack):** {war_att_low:,.2f} – {war_att_high:,.2f}\n"
        f"**War Range (Defense):** {war_def_low:,.2f} – {war_def_high:,.2f}\n"
        f"**Spy Range:** {spy_low:,.2f} – {spy_high:,.2f}"
    )
    return embed


class Link(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="link", description="Link a Discord member to their P&W nation")
    @app_commands.describe(
        nation="Nation ID, full nation URL, or exact nation name",
        member="Who to link (defaults to yourself; linking others needs Admin or MA)",
    )
    async def link(self, interaction: discord.Interaction, nation: str, member: discord.Member = None):
        await interaction.response.defer()
        target = member or interaction.user

        if target.id != interaction.user.id:
            if not await user_can_link_others(self.bot, interaction):
                await interaction.followup.send(
                    embed=embeds.error(
                        "Permission Denied",
                        "Only Administrators or the registered MA role can link other members.",
                    )
                )
                return

        try:
            nation_data = await resolve_nation(self.bot.pw_client, nation)
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error("Lookup Failed", f"Error contacting the P&W API: `{e}`")
            )
            return

        if not nation_data:
            await interaction.followup.send(
                embed=embeds.error("Nation Not Found", f"Couldn't find a nation matching `{nation}`.")
            )
            return

        try:
            await database.link_nation(interaction.guild_id, target.id, nation_data["id"])
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error("Save Failed", f"Found the nation but couldn't save the link: `{e}`")
            )
            return

        await interaction.followup.send(
            embed=embeds.success(
                "Account Linked",
                f"{target.mention} is now linked to **{nation_data['nation_name']}** "
                f"(led by {nation_data['leader_name']}).",
            )
        )

    @app_commands.command(name="unlink", description="Remove a nation link")
    @app_commands.describe(member="Who to unlink (defaults to yourself; unlinking others needs Admin or MA)")
    async def unlink(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user

        if target.id != interaction.user.id:
            if not await user_can_link_others(self.bot, interaction):
                await interaction.followup.send(
                    embed=embeds.error(
                        "Permission Denied",
                        "Only Administrators or the registered MA role can unlink other members.",
                    )
                )
                return

        try:
            await database.unlink_nation(target.id)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Unlink Failed", str(e)))
            return

        await interaction.followup.send(
            embed=embeds.success("Unlinked", f"{target.mention}'s nation link has been removed.")
        )

    @app_commands.command(name="whois", description="Show a full nation profile for a member")
    @app_commands.describe(member="The member to look up")
    async def whois(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        nation_id = await database.get_nation_for_user(member.id)
        if not nation_id:
            await interaction.followup.send(
                embed=embeds.info("Not Linked", f"{member.mention} has not linked a nation.")
            )
            return

        try:
            nation = await self.bot.pw_client.get_nation(nation_id)
            off_wars = (nation.get("offensive_wars") or []) if nation else []
            def_wars = (nation.get("defensive_wars") or []) if nation else []
            off_count = sum(1 for w in off_wars if (w.get("turns_left") or 0) > 0)
            def_count = sum(1 for w in def_wars if (w.get("turns_left") or 0) > 0)
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error("Lookup Failed", f"Error contacting the P&W API: `{e}`")
            )
            return

        if not nation:
            await interaction.followup.send(
                embed=embeds.error("Nation Not Found", f"Linked nation ID `{nation_id}` no longer exists.")
            )
            return

        await interaction.followup.send(embed=build_nation_embed(nation, off_count, def_count))


async def setup(bot: commands.Bot):
    await bot.add_cog(Link(bot))
