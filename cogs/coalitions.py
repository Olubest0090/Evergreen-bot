"""
/bloc — manage coalitions (DNR, DNR_MEMBERS, ENEMIES, ALLIES, EXTENSION)
/fa — Foreign Affairs settings, currently the DNR top-X threshold
/war checkdnr — check if a nation is protected under DNR
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database, embeds

BLOC_CHOICES = [
    app_commands.Choice(name="DNR", value="DNR"),
    app_commands.Choice(name="DNR Members", value="DNR_MEMBERS"),
    app_commands.Choice(name="Enemies", value="ENEMIES"),
    app_commands.Choice(name="Allies", value="ALLIES"),
    app_commands.Choice(name="Extension", value="EXTENSION"),
]


async def is_dnr_protected(bot, guild_id: int, alliance_id: int) -> bool:
    # Never raidable: our own allies, their known extension/offshore
    # alliances, manually-flagged DNR alliances, or anyone in the
    # auto-computed top-X by score. P&W's API doesn't expose which
    # alliances are whose extension/offshore, so those get added
    # manually to the EXTENSION bloc via /bloc add and are protected
    # the same as a direct ally.
    for bloc_type in ("ALLIES", "EXTENSION", "DNR"):
        if await database.is_alliance_in_bloc(guild_id, alliance_id, bloc_type):
            return True

    top_x = await database.get_dnr_top_x(guild_id)
    if top_x <= 0:
        return False

    top_alliances = await bot.pw_client.get_top_alliances(top_x)
    return any(a["id"] == alliance_id for a in top_alliances)


class Coalitions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    bloc_group = app_commands.Group(
        name="bloc",
        description="Manage alliance coalitions (DNR, allies, enemies, etc.)",
        default_permissions=discord.Permissions(administrator=True),
    )
    fa_group = app_commands.Group(
        name="fa",
        description="Foreign Affairs settings",
        default_permissions=discord.Permissions(administrator=True),
    )
    war_group = app_commands.Group(name="war", description="War-related lookups")

    @bloc_group.command(name="add", description="Add an alliance to a coalition")
    @app_commands.choices(bloc=BLOC_CHOICES)
    @app_commands.describe(alliance="Alliance ID or exact name", bloc="Which coalition to add it to")
    async def bloc_add(self, interaction: discord.Interaction, alliance: str, bloc: app_commands.Choice[str]):
        await interaction.response.defer()
        try:
            alliance_data = await self.bot.pw_client.get_alliance_by_id_or_name(alliance)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return
        if not alliance_data:
            await interaction.followup.send(embed=embeds.error("Alliance Not Found", f"No alliance matching `{alliance}`."))
            return

        await database.add_coalition_alliance(
            interaction.guild_id, alliance_data["id"], alliance_data["name"], bloc.value
        )
        await interaction.followup.send(
            embed=embeds.success(
                "Added to Coalition", f"**{alliance_data['name']}** added to **{bloc.name}**."
            )
        )

    @bloc_group.command(name="remove", description="Remove an alliance from a coalition")
    @app_commands.choices(bloc=BLOC_CHOICES)
    @app_commands.describe(alliance="Alliance ID or exact name", bloc="Which coalition to remove it from")
    async def bloc_remove(self, interaction: discord.Interaction, alliance: str, bloc: app_commands.Choice[str]):
        await interaction.response.defer()
        try:
            alliance_data = await self.bot.pw_client.get_alliance_by_id_or_name(alliance)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return
        if not alliance_data:
            await interaction.followup.send(embed=embeds.error("Alliance Not Found", f"No alliance matching `{alliance}`."))
            return

        await database.remove_coalition_alliance(interaction.guild_id, alliance_data["id"], bloc.value)
        await interaction.followup.send(
            embed=embeds.success(
                "Removed from Coalition", f"**{alliance_data['name']}** removed from **{bloc.name}**."
            )
        )

    @bloc_group.command(name="list", description="List coalitions and their alliances")
    @app_commands.choices(bloc=BLOC_CHOICES)
    @app_commands.describe(bloc="Filter to one coalition (optional — shows all if left blank)")
    async def bloc_list(self, interaction: discord.Interaction, bloc: app_commands.Choice[str] = None):
        await interaction.response.defer()
        rows = await database.list_coalitions(interaction.guild_id, bloc.value if bloc else None)
        if not rows:
            await interaction.followup.send(embed=embeds.info("No Entries", "No alliances in this coalition yet."))
            return

        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["bloc_type"], []).append(row["alliance_name"] or str(row["alliance_id"]))

        lines = []
        for bloc_type, names in grouped.items():
            lines.append(f"**{bloc_type}:**\n" + "\n".join(f"- {n}" for n in names))

        await interaction.followup.send(embed=embeds.info("Coalitions", "\n\n".join(lines)))

    @fa_group.command(name="config", description="Set the DNR top-X threshold")
    @app_commands.describe(do_not_raid_top_x="Number of top-ranked alliances protected under DNR (0 disables)")
    async def fa_config(self, interaction: discord.Interaction, do_not_raid_top_x: int):
        await database.set_dnr_top_x(interaction.guild_id, do_not_raid_top_x)
        await interaction.response.send_message(
            embed=embeds.success(
                "DNR Threshold Set",
                f"Top **{do_not_raid_top_x}** alliances (by score) are now protected under DNR, "
                f"plus any alliances manually added to the DNR bloc.",
            )
        )

    @war_group.command(name="checkdnr", description="Check if declaring on a nation is allowed under DNR")
    @app_commands.describe(nation="Nation ID, URL, or exact name")
    async def checkdnr(self, interaction: discord.Interaction, nation: str):
        await interaction.response.defer()
        from cogs.link import resolve_nation

        try:
            nation_data = await resolve_nation(self.bot.pw_client, nation)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return
        if not nation_data:
            await interaction.followup.send(embed=embeds.error("Nation Not Found", f"No nation matching `{nation}`."))
            return

        alliance_id = nation_data.get("alliance_id")
        alliance_name = (nation_data.get("alliance") or {}).get("name", "None")

        if not alliance_id:
            await interaction.followup.send(
                embed=embeds.success("Allowed", f"**{nation_data['nation_name']}** is unaligned — no DNR restriction.")
            )
            return

        protected = await is_dnr_protected(self.bot, interaction.guild_id, alliance_id)
        if protected:
            await interaction.followup.send(
                embed=embeds.error(
                    "🚫 Protected Under DNR",
                    f"**{nation_data['nation_name']}** ({alliance_name}) is protected. "
                    f"Declaring war on this nation is a DNR violation.",
                )
            )
        else:
            await interaction.followup.send(
                embed=embeds.success(
                    "✅ Allowed", f"**{nation_data['nation_name']}** ({alliance_name}) is not DNR-protected."
                )
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Coalitions(bot))
