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

    # naval_blockade is an ID pointing to WHICHEVER side currently holds
    # the blockade — not a simple yes/no flag. It can be either party in
    # either an offensive or defensive war. We only want to surface it
    # when the ENEMY holds it over this nation, not the reverse.
    blockading_nations = []
    all_wars = [
        (war, war.get("defender") or {"id": war.get("def_id")})
        for war in (nation.get("offensive_wars") or [])
    ] + [
        (war, war.get("attacker") or {"id": war.get("att_id")})
        for war in (nation.get("defensive_wars") or [])
    ]
    for war, enemy in all_wars:
        blockade_holder_id = war.get("naval_blockade")
        if (
            blockade_holder_id
            and (war.get("turns_left") or 0) > 0
            and str(blockade_holder_id) == str(enemy.get("id"))
        ):
            blockading_nations.append(enemy)
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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """When someone joins, check if their Discord username matches
        a stored username on any alliance nation, and auto-link them.
        This works even where bulk /autolink can't, since it only needs
        this one member object — no guild-wide cache/chunk required."""
        config = await database.get_alerts_config(member.guild.id)
        alliance_id = config.get("alliance_id") if config else None
        if not alliance_id:
            return

        try:
            nations = await self.bot.pw_client.get_alliance_members(alliance_id)
        except Exception:
            return

        def normalize(s: str) -> str:
            return s.strip().lower().lstrip("@").split("#")[0]

        candidates = {normalize(member.name)}
        if member.global_name:
            candidates.add(normalize(member.global_name))

        for nation in nations:
            tag = nation.get("discord")
            if tag and normalize(tag) in candidates:
                try:
                    await database.link_nation(member.guild.id, member.id, nation["id"])
                    try:
                        await member.send(
                            f"Welcome to Evergreen! I automatically linked your Discord "
                            f"to your nation **{nation['nation_name']}** based on your "
                            f"P&W profile. Use `/whois` anytime to check it, or `/link` "
                            f"if this was wrong."
                        )
                    except discord.Forbidden:
                        pass  # DMs closed — link still succeeded, just no notice sent
                except Exception:
                    pass
                return

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


    @app_commands.command(
        name="autolink",
        description="Bulk-link every alliance member by matching P&W's stored Discord username",
    )
    @app_commands.describe(alliance_id="P&W alliance ID to pull members from (defaults to the configured one)")
    async def autolink(self, interaction: discord.Interaction, alliance_id: int = None):
        if not await user_can_link_others(self.bot, interaction):
            await interaction.response.send_message(
                embed=embeds.error(
                    "Permission Denied",
                    "Only Administrators or the registered MA role can run bulk linking.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if alliance_id is None:
            config = await database.get_alerts_config(interaction.guild_id)
            alliance_id = config.get("alliance_id") if config else None
        if not alliance_id:
            await interaction.followup.send(
                embed=embeds.error(
                    "No Alliance Set", "Provide `alliance_id`, or set one first with `/alerts alliance`."
                )
            )
            return

        try:
            members = await self.bot.pw_client.get_alliance_members(alliance_id)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return

        # Fetch members directly via Discord's REST API rather than
        # relying on the gateway member cache/chunk() — more reliable
        # regardless of whatever's causing the cache to under-populate.
        guild_members = [m async for m in interaction.guild.fetch_members(limit=None)]

        def normalize(s: str) -> str:
            # Always strip anything after '#' — modern Discord usernames
            # have no discriminator, but P&W profiles may still store an
            # old-style tag (legacy "name#1234" or the "name#0" that
            # Discord's own migration left on many old-format profiles).
            # Comparing bare usernames only avoids both cases silently
            # failing to match.
            bare = s.strip().lower().lstrip("@").split("#")[0]
            return bare

        member_lookup: dict[str, discord.Member] = {}
        for m in guild_members:
            member_lookup[normalize(m.name)] = m
            if m.global_name:
                member_lookup[normalize(m.global_name)] = m

        linked, skipped_no_discord, not_found = [], [], []

        for nation in members:
            discord_tag = nation.get("discord")
            if not discord_tag or not discord_tag.strip():
                skipped_no_discord.append(nation["nation_name"])
                continue

            match = member_lookup.get(normalize(discord_tag))
            if not match:
                not_found.append(f"{nation['nation_name']} (`{discord_tag}`)")
                continue

            try:
                await database.link_nation(interaction.guild_id, match.id, nation["id"])
                linked.append(f"{match.mention} → **{nation['nation_name']}**")
            except Exception:
                not_found.append(f"{nation['nation_name']} (`{discord_tag}`) — save failed")

        lines = [
            f"**Searched {len(guild_members)} server members** against {len(members)} alliance nations.\n"
            f"**Linked:** {len(linked)} | **No Discord set on nation:** {len(skipped_no_discord)} | **Not found in server:** {len(not_found)}"
        ]

        if linked:
            lines.append("\n**Linked:**\n" + "\n".join(linked[:20]))
            if len(linked) > 20:
                lines.append(f"*+{len(linked) - 20} more*")

        if not_found:
            lines.append("\n**Could not match (check manually):**\n" + "\n".join(not_found[:15]))
            if len(not_found) > 15:
                lines.append(f"*+{len(not_found) - 15} more*")

        await interaction.followup.send(embed=embeds.success("Auto-Link Complete", "\n".join(lines)))


async def setup(bot: commands.Bot):
    await bot.add_cog(Link(bot))
