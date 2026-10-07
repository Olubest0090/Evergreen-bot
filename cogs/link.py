"""
/link — connects a Discord member to their Politics & War nation.
/whois — shows a full nation profile card for a linked member or any nation.
"""
import asyncio
import re
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button

from utils import database, embeds

NATION_URL_PATTERN = re.compile(r"nation[/=]id=(\d+)|nation/(\d+)")


def normalize_discord_tag(s: str) -> str:
    return s.strip().lower().lstrip("@").split("#")[0]


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


def format_timer_seconds(seconds: int | float | None) -> str:
    if not seconds or seconds <= 0:
        return "Ready"
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def build_nation_embed(
    nation: dict,
    off_count: int = 0,
    def_count: int = 0,
    discord_user: discord.User | discord.Member | None = None,
) -> discord.Embed:
    nation_id = nation["id"]
    name = nation.get("nation_name", "Unknown")
    leader = nation.get("leader_name", "Unknown")
    score = nation.get("score", 0)
    color = (nation.get("color") or "None").title()
    alliance = nation.get("alliance") or {}
    alliance_name = alliance.get("name", "None")
    position = (nation.get("alliance_position") or "None").title()
    cities = nation.get("cities") or []
    num_cities = len(cities)
    total_infra = sum(c.get("infrastructure", 0) or 0 for c in cities)

    if nation.get("advanced_pirate_economy"):
        max_off = 7
    elif nation.get("pirate_economy"):
        max_off = 6
    else:
        max_off = 5
    max_def = 3

    domestic_policy = (nation.get("domestic_policy") or "None").replace("_", " ").title()
    war_policy = (nation.get("war_policy") or "None").replace("_", " ").title()
    project_count = nation.get("projects", 0) or 0

    status_parts = []
    if nation.get("vacation_mode_turns", 0) > 0:
        status_parts.append(f"🌴 Vacation ({nation['vacation_mode_turns']} turns)")
    if nation.get("beige_turns", 0) > 0:
        status_parts.append(f"🔶 Beige ({nation['beige_turns']} turns)")

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

    embed = discord.Embed(
        title=name,
        url=f"https://politicsandwar.com/nation/id={nation_id}",
        color=0x2E7D32,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Evergreen MILCOM")

    if discord_user:
        embed.set_author(
            name=str(discord_user.display_name),
            icon_url=discord_user.display_avatar.url,
        )
        embed.description = f"**Discord:** {discord_user.mention}"

    embed.add_field(
        name="Overview",
        value=(
            f"**Leader:** {leader}\n"
            f"**Alliance:** {alliance_name} ({position})\n"
            f"**Color:** {color}\n"
            f"**Cities:** {num_cities}  |  **Infra:** {total_infra:,.2f}\n"
            f"**Score:** {score:,.2f}  |  **Projects:** {project_count}\n"
            f"**Policies:** {domestic_policy} / {war_policy}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Military",
        value=(
            f"`{nation.get('soldiers', 0):,} 💂 | {nation.get('tanks', 0):,} ⚙️ | "
            f"{nation.get('aircraft', 0):,} ✈️ | {nation.get('ships', 0):,} 🚢`\n"
            f"`{nation.get('missiles', 0)} 🚀 | {nation.get('nukes', 0)} ☢️ | "
            f"{nation.get('spies', 0):,} 🔍`"
        ),
        inline=False,
    )

    embed.add_field(
        name="WarSlots & Status",
        value=(
            f"**Offense:** {off_count}/{max_off}   **Defense:** {def_count}/{max_def}\n"
            f"**Last Active:** {format_duration(nation.get('last_active'))}\n"
            f"**Status:** {status}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Ranges",
        value=(
            f"**Attack:** {war_att_low:,.2f} – {war_att_high:,.2f}\n"
            f"**Defense:** {war_def_low:,.2f} – {war_def_high:,.2f}\n"
            f"**Spies:** {spy_low:,.2f} – {spy_high:,.2f}"
        ),
        inline=False,
    )

    return embed


class WhoisView(View):
    def __init__(self, nation: dict, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.nation = nation

    @discord.ui.button(label="Revenue", style=discord.ButtonStyle.primary, emoji="💰")
    async def revenue_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)

        try:
            nation = self.nation
            cities = nation.get("cities") or []

            has_mass_irrigation = bool(nation.get("mass_irrigation"))

            # PRODUCTION (wiki rates)
            prod_food = 0.0
            prod_coal = 0.0
            prod_oil = 0.0
            prod_uranium = 0.0
            prod_iron = 0.0
            prod_bauxite = 0.0
            prod_steel = 0.0

            for c in cities:
                land = c.get("land", 0) or 0
                if has_mass_irrigation:
                    prod_food += (land * 3 / 100)
                else:
                    prod_food += (land * 3 / 125)

                prod_coal     += (c.get("coalmine", 0) or 0) * 3
                prod_oil      += (c.get("oilwell", 0) or 0) * 3
                prod_uranium  += (c.get("uramine", 0) or 0) * 3
                prod_iron     += (c.get("ironmine", 0) or 0) * 3
                prod_bauxite  += (c.get("bauxitemine", 0) or 0) * 3
                prod_steel    += (c.get("steelmill", 0) or 0) * 9

            # CONSUMPTION
            cons_food = 0.0
            cons_coal = 0.0
            cons_oil = 0.0
            cons_uranium = 0.0
            cons_iron = 0.0

            for c in cities:
                cons_coal     += (c.get("coalpower", 0) or 0) * 1.2
                cons_oil      += (c.get("oilpower", 0) or 0) * 1.2
                cons_oil      += (c.get("gasrefinery", 0) or 0) * 3
                cons_uranium  += (c.get("nuclearpower", 0) or 0) * 2.4
                cons_iron     += (c.get("steelmill", 0) or 0) * 3

            total_infra = sum(c.get("infrastructure", 0) or 0 for c in cities)
            cons_food += (total_infra * 100 / 1000) * 0.5

            # NET
            net_food     = prod_food - cons_food
            net_coal     = prod_coal - cons_coal
            net_oil      = prod_oil - cons_oil
            net_uranium  = prod_uranium - cons_uranium
            net_iron     = prod_iron - cons_iron
            net_bauxite  = prod_bauxite
            net_steel    = prod_steel
            net_money    = 0.0

            text = (
                f"**Production:**\n"
                f"```\n"
                f"{{FOOD={prod_food:,.2f}, COAL={prod_coal:,.2f}, OIL={prod_oil:,.2f}, "
                f"URANIUM={prod_uranium:,.2f}, IRON={prod_iron:,.2f}, BAUXITE={prod_bauxite:,.2f}, STEEL={prod_steel:,.2f}}}\n"
                f"```\n"
                f"**Consumption:**\n"
                f"```\n"
                f"{{FOOD={-cons_food:,.2f}, COAL={-cons_coal:,.2f}, OIL={-cons_oil:,.2f}, "
                f"URANIUM={-cons_uranium:,.2f}, IRON={-cons_iron:,.2f}}}\n"
                f"```\n"
                f"**Net:**\n"
                f"```\n"
                f"{{MONEY={net_money:,.2f}, FOOD={net_food:,.2f}, COAL={net_coal:,.2f}, OIL={net_oil:,.2f}, "
                f"URANIUM={net_uranium:,.2f}, IRON={net_iron:,.2f}, BAUXITE={net_bauxite:,.2f}, STEEL={net_steel:,.2f}}}\n"
                f"```"
            )

            embed = embeds.info("Nation Revenue", text)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Error calculating revenue: `{str(e)}`", ephemeral=True)

    @discord.ui.button(label="Timers", style=discord.ButtonStyle.secondary, emoji="⏱️")
    async def timers_button(self, interaction: discord.Interaction, button: Button):
        nation = self.nation
        projects = nation.get("projects", 0) or 0
        beige = nation.get("beige_turns", 0) or 0
        vacation = nation.get("vacation_mode_turns", 0) or 0

        max_projects = 20
        if nation.get("urban_planning"):
            max_projects += 5
        if nation.get("advanced_urban_planning"):
            max_projects += 5

        text = (
            f"**City:** Ready (full turn tracking coming)\n"
            f"**Project:** Ready | ({projects}/{max_projects} slots)\n"
            f"**Color:** Ready (full turn tracking coming)\n"
            f"**Domestic Policy:** Ready (full turn tracking coming)\n"
            f"**War Policy:** Ready (full turn tracking coming)\n"
            f"**Beige Turns:** {beige} turns\n"
            f"**Vacation:** {vacation} turns"
        )

        embed = embeds.info("Nation Timers", text)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class Link(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _fetch_all_members(self, guild: discord.Guild) -> list[discord.Member]:
        return [m async for m in guild.fetch_members(limit=None)]

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
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
                        pass
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

        existing_owner_id = await database.get_discord_id_for_nation(nation_data["id"])
        if existing_owner_id and existing_owner_id == target.id:
            await interaction.followup.send(
                embed=embeds.success(
                    "Already Linked",
                    f"{target.mention} is already linked to **{nation_data['nation_name']}**. Nothing changed.",
                )
            )
            return
        if existing_owner_id and existing_owner_id != target.id:
            await interaction.followup.send(
                embed=embeds.error(
                    "Already Linked",
                    f"**{nation_data['nation_name']}** is already linked to <@{existing_owner_id}>. "
                    f"Unlink that account first with `/unlink` before linking it here.",
                )
            )
            return

        if target.id == interaction.user.id:
            stored_tag = nation_data.get("discord")
            candidates = {normalize_discord_tag(target.name)}
            if target.global_name:
                candidates.add(normalize_discord_tag(target.global_name))
            if not stored_tag or normalize_discord_tag(stored_tag) not in candidates:
                await interaction.followup.send(
                    embed=embeds.error(
                        "Discord Tag Mismatch",
                        f"**{nation_data['nation_name']}**'s P&W profile has "
                        f"`{stored_tag or 'no Discord set'}` on file, which doesn't match your account. "
                        f"Update your Discord username on your P&W nation page, or ask an Admin/MA to link it for you.",
                    )
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

    @app_commands.command(name="whois", description="Show a full nation profile")
    @app_commands.describe(
        member="Discord member (must be linked)",
        nation="Nation ID, name, or URL (for unlinked / enemy nations)",
    )
    async def whois(
        self,
        interaction: discord.Interaction,
        member: discord.Member = None,
        nation: str = None,
    ):
        await interaction.response.defer()

        if not member and not nation:
            await interaction.followup.send(
                embed=embeds.error(
                    "Missing Input",
                    "Provide either a **Discord member** or a **nation ID / name / URL**.",
                )
            )
            return

        if member and nation:
            await interaction.followup.send(
                embed=embeds.error(
                    "Too Many Inputs",
                    "Provide either a member **or** a nation, not both.",
                )
            )
            return

        discord_user = None
        nation_data = None

        try:
            print("=== WHOIS STARTED ===")
            if member:
                nation_id = await database.get_nation_for_user(member.id)
                if not nation_id:
                    await interaction.followup.send(
                        embed=embeds.info("Not Linked", f"{member.mention} has not linked a nation.")
                    )
                    return
                print(f"=== Fetching nation {nation_id} ===")
                nation_data = await self.bot.pw_client.get_nation(nation_id)
                print("=== Nation fetched successfully ===")
                discord_user = member
            else:
                print(f"=== Resolving nation {nation} ===")
                nation_data = await resolve_nation(self.bot.pw_client, nation)
                print("=== Nation resolved ===")

            if not nation_data:
                await interaction.followup.send(
                    embed=embeds.error("Nation Not Found", "Could not find that nation.")
                )
                return

            off_wars = nation_data.get("offensive_wars") or []
            def_wars = nation_data.get("defensive_wars") or []
            off_count = sum(1 for w in off_wars if (w.get("turns_left") or 0) > 0)
            def_count = sum(1 for w in def_wars if (w.get("turns_left") or 0) > 0)

            result = build_nation_embed(nation_data, discord_user)
            if isinstance(result, tuple):
                embed, view = result[0], result[1]
                await interaction.followup.send(embed=embed, view=view) if view else await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(embed=result)

        except Exception as e:
            print(f"=== WHOIS ERROR: {e} ===")
            await interaction.followup.send(
                embed=embeds.error("Lookup Failed", f"Error contacting the P&W API: `{e}`")
            )
            return

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

        try:
            guild_members = await asyncio.wait_for(
                self._fetch_all_members(interaction.guild), timeout=30
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                embed=embeds.error(
                    "Timed Out",
                    "Fetching the member list took too long (30s+). This may be a "
                    "Discord API or bot-permissions issue — check that the bot has "
                    "'View Server Members' permission and Members Intent is enabled.",
                )
            )
            return
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error("Member Fetch Failed", f"`{type(e).__name__}: {e}`")
            )
            return

        def normalize(s: str) -> str:
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
