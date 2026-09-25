"""
/link — connects a Discord member to their Politics & War nation.
Accepts a nation ID, a full nation URL, or an exact nation name.
Anyone can link themselves; linking someone else requires
Administrator or the registered MA role.
"""

import re

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
    if interaction.user.guild_permissions.administrator:
        return True
    ma_role_id = await database.get_guild_role(interaction.guild_id, "MA")
    if ma_role_id and any(role.id == ma_role_id for role in interaction.user.roles):
        return True
    return False


class Link(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="link", description="Link a Discord member to their P&W nation")
    @app_commands.describe(
        nation="Nation ID, full nation URL, or exact nation name",
        member="Who to link (defaults to yourself; linking others needs Admin or MA)",
    )
    async def link(self, interaction: discord.Interaction, nation: str, member: discord.Member = None):
        target = member or interaction.user

        if target.id != interaction.user.id:
            if not await user_can_link_others(self.bot, interaction):
                await interaction.response.send_message(
                    embed=embeds.error(
                        "Permission Denied",
                        "Only Administrators or the registered MA role can link other members.",
                    ),
                    ephemeral=True,
                )
                return

        nation_data = await resolve_nation(self.bot.pw_client, nation)
        if not nation_data:
            await interaction.response.send_message(
                embed=embeds.error("Nation Not Found", f"Couldn't find a nation matching `{nation}`."),
                ephemeral=True,
            )
            return

        await database.link_nation(interaction.guild_id, target.id, nation_data["id"])
        await interaction.response.send_message(
            embed=embeds.success(
                "Account Linked",
                f"{target.mention} is now linked to **{nation_data['nation_name']}** "
                f"(led by {nation_data['leader_name']}).",
            )
        )

    @app_commands.command(name="unlink", description="Remove a nation link")
    @app_commands.describe(member="Who to unlink (defaults to yourself; unlinking others needs Admin or MA)")
    async def unlink(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user

        if target.id != interaction.user.id:
            if not await user_can_link_others(self.bot, interaction):
                await interaction.response.send_message(
                    embed=embeds.error(
                        "Permission Denied",
                        "Only Administrators or the registered MA role can unlink other members.",
                    ),
                    ephemeral=True,
                )
                return

        await database.unlink_nation(target.id)
        await interaction.response.send_message(
            embed=embeds.success("Unlinked", f"{target.mention}'s nation link has been removed."),
            ephemeral=True,
        )

    @app_commands.command(name="whois", description="Show which nation a member is linked to")
    @app_commands.describe(member="The member to look up")
    async def whois(self, interaction: discord.Interaction, member: discord.Member):
        nation_id = await database.get_nation_for_user(member.id)
        if not nation_id:
            await interaction.response.send_message(
                embed=embeds.info("Not Linked", f"{member.mention} has not linked a nation."),
            )
            return

        nation = await self.bot.pw_client.get_nation(nation_id)
        name = nation["nation_name"] if nation else f"ID {nation_id} (nation data unavailable)"
        await interaction.response.send_message(
            embed=embeds.info("Linked Nation", f"{member.mention} is linked to **{name}**.")
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Link(bot))
