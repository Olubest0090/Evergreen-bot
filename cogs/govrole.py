"""
/govrole set — registers which Discord role corresponds to each
Evergreen government position (Military, Economic, Foreign, Internal
Affairs), so every other part of the bot knows who to ping.
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds

ROLE_CHOICES = [
    app_commands.Choice(name="Military Affairs (MA)", value="MA"),
    app_commands.Choice(name="Economic Affairs (EA)", value="EA"),
    app_commands.Choice(name="Foreign Affairs (FA)", value="FA"),
    app_commands.Choice(name="Internal Affairs (IA)", value="IA"),
]

FULL_NAMES = {
    "MA": "Military Affairs",
    "EA": "Economic Affairs",
    "FA": "Foreign Affairs",
    "IA": "Internal Affairs",
}


class GovRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    govrole_group = app_commands.Group(
        name="govrole",
        description="Register which Discord role maps to each government position",
        default_permissions=discord.Permissions(administrator=True),
    )

    @govrole_group.command(name="set", description="Set the role for a government position")
    @app_commands.choices(role_type=ROLE_CHOICES)
    @app_commands.describe(
        role_type="Which government position this role represents",
        role="The Discord role to register",
    )
    async def set_role(
        self,
        interaction: discord.Interaction,
        role_type: app_commands.Choice[str],
        role: discord.Role,
    ):
        await database.set_guild_role(interaction.guild_id, role_type.value, role.id)
        await interaction.response.send_message(
            embed=embeds.success(
                "Role Registered",
                f"**{FULL_NAMES[role_type.value]}** is now mapped to {role.mention}.\n"
                f"The bot will use this role for pings and permission checks going forward.",
            )
        )

    @govrole_group.command(name="list", description="Show all registered government roles")
    async def list_roles(self, interaction: discord.Interaction):
        roles = await database.get_all_guild_roles(interaction.guild_id)
        if not roles:
            await interaction.response.send_message(
                embed=embeds.info(
                    "No Roles Registered",
                    "Use `/govrole set` to register MA, EA, FA, and IA roles.",
                )
            )
            return

        lines = []
        for role_type, name in FULL_NAMES.items():
            role_id = roles.get(role_type)
            value = f"<@&{role_id}>" if role_id else "*not set*"
            lines.append(f"**{name} ({role_type}):** {value}")

        await interaction.response.send_message(
            embed=embeds.info("Registered Government Roles", "\n".join(lines))
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GovRole(bot))
