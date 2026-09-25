"""
/bloc — manage coalitions (DNR, DNR_MEMBERS, ENEMIES, ALLIES, EXTENSION)
/fa — Foreign Affairs settings, currently the DNR top-X threshold
/war checkdnr — check if a nation is protected under DNR

Also runs a background loop that auto-syncs the ALLIES bloc from
Evergreen's actual in-game treaties, adding new allies and removing
ones whose treaty was cancelled — without touching anything a human
added manually via /bloc add (tracked via the auto_synced flag).
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import database, embeds

BLOC_CHOICES = [
    app_commands.Choice(name="DNR", value="DNR"),
    app_commands.Choice(name="DNR Members", value="DNR_MEMBERS"),
    app_commands.Choice(name="Enemies", value="ENEMIES"),
    app_commands.Choice(name="Allies", value="ALLIES"),
    app_commands.Choice(name="Extension", value="EXTENSION"),
]

TREATY_SYNC_INTERVAL_SECONDS = 600


async def is_dnr_protected(bot, guild_id: int, alliance_id: int) -> bool:
    # Never raidable: our own allies, their known extension/offshore
    # alliances, manually-flagged DNR alliances, or anyone in the
    # auto-computed top-X by score.
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
        self.sync_treaties.start()

    def cog_unload(self):
        self.sync_treaties.cancel()

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

        # Manual additions are never auto_synced, so the treaty sync
        # loop will never remove them even if no treaty exists.
        await database.add_coalition_alliance(
            interaction.guild_id, alliance_data["id"], alliance_data["name"], bloc.value, auto_synced=False
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
            tag = " *(auto)*" if row.get("auto_synced") else ""
            grouped.setdefault(row["bloc_type"], []).append(
                f"{row['alliance_name'] or row['alliance_id']}{tag}"
            )

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
                f"plus any alliances manually added to DNR, Allies, or Extension.",
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

    @tasks.loop(seconds=TREATY_SYNC_INTERVAL_SECONDS)
    async def sync_treaties(self):
        try:
            configs = await database.get_all_alerts_configs()
            for config in configs:
                alliance_id = config.get("alliance_id")
                if not alliance_id:
                    continue
                await self._sync_guild_treaties(config["guild_id"], alliance_id)
        except Exception as e:
            print(f"[coalitions] treaty sync error: {e}")

    @sync_treaties.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()

    async def _sync_guild_treaties(self, guild_id: int, alliance_id: int):
        treaties = await self.bot.pw_client.get_alliance_treaties(alliance_id)
        current_ally_ids = {t["other_alliance_id"] for t in treaties}

        # Add any new treaty partners not already in ALLIES.
        for t in treaties:
            already_present = await database.is_alliance_in_bloc(guild_id, t["other_alliance_id"], "ALLIES")
            if not already_present:
                await database.add_coalition_alliance(
                    guild_id, t["other_alliance_id"], t["other_alliance_name"], "ALLIES", auto_synced=True
                )

        # Remove only auto-synced ALLIES entries whose treaty no longer
        # exists — manual entries (auto_synced=False) are left alone.
        existing = await database.list_coalitions(guild_id, "ALLIES")
        for row in existing:
            if row.get("auto_synced") and row["alliance_id"] not in current_ally_ids:
                await database.remove_coalition_alliance(guild_id, row["alliance_id"], "ALLIES")


async def setup(bot: commands.Bot):
    await bot.add_cog(Coalitions(bot))
