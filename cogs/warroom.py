"""
/warroom — one shared channel per ENEMY NATION (not per war), so if
multiple Evergreen members are fighting the same enemy, they all land
in the same room together. Categorized by the enemy's city count into
admin-configured brackets. Skips wars where the enemy has been inactive
7+ days. Every Evergreen member with an active war against that enemy
gets channel access automatically.

/warroom bracket — configure city-count brackets (e.g. C1-5, C10,
C20-30) to match your server's existing role naming.
/warroom config — enable/disable auto war rooms.
/warroom pin — manually (re)post the pinned info embed.
/admin syncwarrooms — force sync now.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone

from utils import database, embeds
from cogs.alerts import nation_block

ATTACK_TRACK_INTERVAL_SECONDS = 120
INACTIVITY_CUTOFF_DAYS = 7


def find_bracket_label(brackets: list[dict], num_cities: int) -> str:
    for b in brackets:
        if b["min_cities"] <= num_cities <= b["max_cities"]:
            return b["label"]
    return f"WARCAT {num_cities}"


def is_inactive(last_active_iso: str | None, days: int = INACTIVITY_CUTOFF_DAYS) -> bool:
    if not last_active_iso:
        return False
    try:
        last = datetime.fromisoformat(last_active_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).days >= days
    except Exception:
        return False


class WarRoomUpdateView(discord.ui.View):
    def __init__(self, bot: commands.Bot, enemy_nation_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.enemy_nation_id = enemy_nation_id

    @discord.ui.button(label="Update", style=discord.ButtonStyle.primary, custom_id="warroom_update")
    async def update(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            enemy = await self.bot.pw_client.get_nation(self.enemy_nation_id)
        except Exception as e:
            await interaction.followup.send(f"Error refreshing: `{e}`", ephemeral=True)
            return
        if not enemy:
            await interaction.followup.send("This nation no longer exists.", ephemeral=True)
            return

        embed = build_room_pin_embed(enemy)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("Updated.", ephemeral=True)


def build_room_pin_embed(enemy: dict) -> discord.Embed:
    embed = embeds.info(f"War Room: {enemy.get('nation_name', 'Unknown')}")
    embed.url = f"https://politicsandwar.com/nation/id={enemy.get('id')}"
    block = nation_block(enemy, None, None)
    embed.description = f"{block}\n\n*Tap Update for the latest figures.*"
    return embed


ATTACK_TYPE_LABELS = {
    "GROUND": "⚔️ GROUND",
    "AIRSTRIKE_INFRASTRUCTURE": "✈️ AIRSTRIKE (Infra)",
    "AIRSTRIKE_SOLDIERS": "✈️ AIRSTRIKE (Soldiers)",
    "AIRSTRIKE_TANKS": "✈️ AIRSTRIKE (Tanks)",
    "AIRSTRIKE_MONEY": "✈️ AIRSTRIKE (Money)",
    "AIRSTRIKE_SHIPS": "✈️ AIRSTRIKE (Ships)",
    "AIRSTRIKE_AIRCRAFT": "✈️ AIRSTRIKE (Dogfight)",
    "NAVAL": "🚢 NAVAL",
    "MISSILE": "🚀 MISSILE",
    "NUKE": "☢️ NUKE",
    "FORTIFY": "🛡️ FORTIFY",
    "PEACE": "🕊️ PEACE OFFER",
}


class WarRoom(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warroom_loop.start()

    def cog_unload(self):
        self.warroom_loop.cancel()

    warroom_group = app_commands.Group(
        name="warroom",
        description="War room configuration and controls",
        default_permissions=discord.Permissions(administrator=True),
    )
    bracket_group = app_commands.Group(
        name="bracket", description="Configure city-count brackets for war room categories", parent=warroom_group
    )
    admin_group = app_commands.Group(
        name="admin", description="Admin utilities", default_permissions=discord.Permissions(administrator=True)
    )

    @warroom_group.command(name="config", description="Enable or disable automatic war room creation")
    @app_commands.describe(enable_war_rooms="Whether to auto-create/manage war room channels")
    async def config(self, interaction: discord.Interaction, enable_war_rooms: bool):
        await database.set_warroom_enabled(interaction.guild_id, enable_war_rooms)
        await interaction.response.send_message(
            embed=embeds.success(
                "War Room Setting Updated",
                f"Automatic war rooms are now **{'enabled' if enable_war_rooms else 'disabled'}**.",
            )
        )

    @bracket_group.command(name="add", description="Add or update a city-count bracket")
    @app_commands.describe(min_cities="Lowest city count in this bracket", max_cities="Highest city count in this bracket", label="Category name, e.g. 'C1-5' or 'C20-30'")
    async def bracket_add(self, interaction: discord.Interaction, min_cities: int, max_cities: int, label: str):
        await database.add_bracket(interaction.guild_id, min_cities, max_cities, label)
        await interaction.response.send_message(
            embed=embeds.success("Bracket Saved", f"Cities {min_cities}-{max_cities} → **{label}**")
        )

    @bracket_group.command(name="remove", description="Remove a city-count bracket")
    @app_commands.describe(min_cities="Lowest city count of the bracket to remove", max_cities="Highest city count of the bracket to remove")
    async def bracket_remove(self, interaction: discord.Interaction, min_cities: int, max_cities: int):
        await database.remove_bracket(interaction.guild_id, min_cities, max_cities)
        await interaction.response.send_message(
            embed=embeds.success("Bracket Removed", f"Removed the {min_cities}-{max_cities} bracket.")
        )

    @bracket_group.command(name="list", description="Show configured city-count brackets")
    async def bracket_list(self, interaction: discord.Interaction):
        brackets = await database.list_brackets(interaction.guild_id)
        if not brackets:
            await interaction.response.send_message(
                embed=embeds.info("No Brackets Configured", "Add one with `/warroom bracket add`.")
            )
            return
        lines = [f"**{b['label']}** — cities {b['min_cities']}-{b['max_cities']}" for b in brackets]
        await interaction.response.send_message(embed=embeds.info("City-Count Brackets", "\n".join(lines)))

    @warroom_group.command(name="pin", description="Refresh the pinned info embed in the current war room")
    async def pin(self, interaction: discord.Interaction):
        await interaction.response.defer()
        room = await database.get_war_room_by_channel(interaction.channel_id)
        if not room:
            await interaction.followup.send(
                embed=embeds.error("Not a War Room", "This only works inside an auto-created war room channel.")
            )
            return

        try:
            enemy = await self.bot.pw_client.get_nation(room["enemy_nation_id"])
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return
        if not enemy:
            await interaction.followup.send(embed=embeds.error("Nation Not Found", "This nation no longer exists."))
            return

        embed = build_room_pin_embed(enemy)
        view = WarRoomUpdateView(self.bot, room["enemy_nation_id"])
        msg = await interaction.followup.send(embed=embed, view=view)
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        await database.set_pin_message(interaction.guild_id, room["enemy_nation_id"], msg.id)

    @admin_group.command(name="syncwarrooms", description="Force-sync war rooms now")
    async def sync_warrooms(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            created, closed = await self._sync_guild_warrooms(interaction.guild_id, interaction.guild)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Sync Failed", str(e)))
            return
        await interaction.followup.send(
            embed=embeds.success("War Rooms Synced", f"Created: {created} | Closed: {closed}")
        )

    async def _get_or_create_category(self, guild: discord.Guild, name: str) -> discord.CategoryChannel:
        for cat in guild.categories:
            if cat.name == name:
                return cat
        return await guild.create_category(name)

    async def _grant_access(self, channel: discord.TextChannel, discord_user_id: int) -> None:
        already = await database.is_room_participant(channel.id, discord_user_id)
        if already:
            return
        member = channel.guild.get_member(discord_user_id)
        if member:
            try:
                await channel.set_permissions(member, view_channel=True, send_messages=True)
            except discord.HTTPException:
                pass
        await database.add_room_participant(channel.id, discord_user_id)

    async def _create_room_for_enemy(self, guild: discord.Guild, guild_id: int, enemy: dict, brackets: list[dict]) -> discord.TextChannel:
        num_cities = len(enemy.get("cities") or [])
        category_name = find_bracket_label(brackets, num_cities)
        category = await self._get_or_create_category(guild, category_name)

        enemy_name = enemy.get("nation_name", "unknown").lower().replace(" ", "")
        channel_name = f"c{num_cities}-{enemy_name}"[:90]

        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
        await database.create_war_room(guild_id, enemy["id"], channel.id, category.id)

        embed = build_room_pin_embed(enemy)
        view = WarRoomUpdateView(self.bot, enemy["id"])
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        await database.set_pin_message(guild_id, enemy["id"], msg.id)
        return channel

    async def _sync_guild_warrooms(self, guild_id: int, guild: discord.Guild) -> tuple[int, int]:
        settings = await database.get_warroom_settings(guild_id)
        if not settings or not settings.get("enabled"):
            return 0, 0

        config = await database.get_alerts_config(guild_id)
        alliance_id = config.get("alliance_id") if config else None
        if not alliance_id:
            return 0, 0

        wars = await self.bot.pw_client.get_active_wars(alliance_id)
        brackets = await database.list_brackets(guild_id)

        by_enemy: dict[int, dict] = {}
        by_enemy_wars: dict[int, list] = {}
        for war in wars:
            enemy = war.get("attacker") if war.get("_side") == "defense" else war.get("defender")
            our_nation = war.get("defender") if war.get("_side") == "defense" else war.get("attacker")
            if not enemy or not enemy.get("id"):
                continue
            if is_inactive(enemy.get("last_active")):
                continue
            by_enemy[enemy["id"]] = enemy
            by_enemy_wars.setdefault(enemy["id"], []).append((war, our_nation))

        created = 0
        for enemy_id, enemy in by_enemy.items():
            room = await database.get_war_room(guild_id, enemy_id)
            if not room:
                channel = await self._create_room_for_enemy(guild, guild_id, enemy, brackets)
                created += 1
            else:
                channel = guild.get_channel(room["channel_id"])
                if not channel:
                    continue

            for war, our_nation in by_enemy_wars[enemy_id]:
                if not our_nation:
                    continue
                discord_id = await database.get_discord_id_for_nation(our_nation["id"])
                if discord_id:
                    await self._grant_access(channel, discord_id)

        closed = 0
        existing_rooms = await database.get_all_war_rooms(guild_id)
        for room in existing_rooms:
            if room["enemy_nation_id"] not in by_enemy:
                channel = guild.get_channel(room["channel_id"])
                if channel:
                    try:
                        await channel.delete(reason="War ended or enemy went inactive")
                    except discord.HTTPException:
                        pass
                await database.delete_war_room(guild_id, room["enemy_nation_id"])
                closed += 1

        return created, closed

    @tasks.loop(seconds=ATTACK_TRACK_INTERVAL_SECONDS)
    async def warroom_loop(self):
        try:
            configs = await database.get_all_alerts_configs()
            for config in configs:
                guild = self.bot.get_guild(config["guild_id"])
                if not guild:
                    continue
                try:
                    await self._sync_guild_warrooms(config["guild_id"], guild)
                except Exception as e:
                    print(f"[warroom] sync error: {e}")

                try:
                    await self._track_attacks_for_guild(config["guild_id"], guild, config.get("alliance_id"))
                except Exception as e:
                    print(f"[warroom] attack tracking error: {e}")
        except Exception as e:
            print(f"[warroom] loop error: {e}")

    @warroom_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    async def _track_attacks_for_guild(self, guild_id: int, guild: discord.Guild, alliance_id: int | None):
        if not alliance_id:
            return
        rooms = await database.get_all_war_rooms(guild_id)
        if not rooms:
            return

        wars = await self.bot.pw_client.get_active_wars(alliance_id)
        wars_by_enemy: dict[int, list] = {}
        for war in wars:
            enemy = war.get("attacker") if war.get("_side") == "defense" else war.get("defender")
            if enemy and enemy.get("id"):
                wars_by_enemy.setdefault(enemy["id"], []).append(war)

        for room in rooms:
            channel = guild.get_channel(room["channel_id"])
            if not channel:
                continue
            for war in wars_by_enemy.get(room["enemy_nation_id"], []):
                try:
                    attacks = await self.bot.pw_client.get_war_attacks(war["id"])
                except Exception as e:
                    print(f"[warroom] get_war_attacks failed for war {war['id']}: {e}")
                    continue
                for attack in attacks:
                    if await database.is_attack_seen(attack["id"]):
                        continue
                    label = ATTACK_TYPE_LABELS.get(attack.get("type"), attack.get("type", "Attack"))
                    success = attack.get("success", "Unknown")
                    await channel.send(f"**{label}** — Result: `{success}`")
                    await database.mark_attack_seen(attack["id"])


async def setup(bot: commands.Bot):
    await bot.add_cog(WarRoom(bot))
