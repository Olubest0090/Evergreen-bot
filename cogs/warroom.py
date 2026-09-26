"""
/warroom — auto-creates a channel for every active war involving our
members, under a bot-managed category grouped by city count. Posts a
pinned info embed with an Update button, and logs new attacks live.
/war info — full war rundown for a nation.
/admin sync warrooms — force-create missing rooms, close ended ones.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import database, embeds
from cogs.alerts import nation_block, format_duration

ATTACK_TRACK_INTERVAL_SECONDS = 120


def city_bracket_name(num_cities: int, prefix: str = "WARCAT") -> str:
    low = ((num_cities - 1) // 5) * 5 + 1
    high = low + 4
    return f"{prefix} {low}-{high}"


class WarRoomUpdateView(discord.ui.View):
    def __init__(self, bot: commands.Bot, war_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.war_id = war_id

    @discord.ui.button(label="Update", style=discord.ButtonStyle.primary, custom_id="warroom_update")
    async def update(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            war = await self.bot.pw_client.get_war(self.war_id)
        except Exception as e:
            await interaction.followup.send(f"Error refreshing: `{e}`", ephemeral=True)
            return
        if not war:
            await interaction.followup.send("This war no longer exists.", ephemeral=True)
            return

        embed = build_war_pin_embed(war)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("Updated.", ephemeral=True)


def build_war_pin_embed(war: dict) -> discord.Embed:
    attacker = war.get("attacker") or {}
    defender = war.get("defender") or {}
    war_type = war.get("war_type", "Unknown")
    turns_left = war.get("turns_left", "?")
    war_link = f"https://politicsandwar.com/nation/war/timeline/war={war['id']}"

    att_block = nation_block(attacker, war.get("att_resistance"), war.get("att_points"))
    def_block = nation_block(defender, war.get("def_resistance"), war.get("def_points"))

    embed = embeds.info(f"{attacker.get('nation_name', 'Unknown')} > {defender.get('nation_name', 'Unknown')}")
    embed.description = (
        f"**{war_type}** — Turns Left: {turns_left}/60\n\n"
        f"Link: [Click here]({war_link})\n\n"
        f"{att_block}\n\n"
        f"{def_block}\n\n"
        f"*Tap Update for the latest figures.*"
    )
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
        self.track_attacks.start()

    def cog_unload(self):
        self.track_attacks.cancel()

    warroom_group = app_commands.Group(
        name="warroom",
        description="War room configuration and controls",
        default_permissions=discord.Permissions(administrator=True),
    )
    admin_group = app_commands.Group(
        name="admin", description="Admin utilities", default_permissions=discord.Permissions(administrator=True)
    )

    @warroom_group.command(name="config", description="Enable or disable automatic war room creation")
    @app_commands.describe(enable_war_rooms="Whether to auto-create a channel for every active war")
    async def config(self, interaction: discord.Interaction, enable_war_rooms: bool):
        await database.set_warroom_enabled(interaction.guild_id, enable_war_rooms)
        await interaction.response.send_message(
            embed=embeds.success(
                "War Room Setting Updated",
                f"Automatic war room creation is now **{'enabled' if enable_war_rooms else 'disabled'}**.",
            )
        )

    @warroom_group.command(name="pin", description="Create/refresh the pinned info embed in the current war room")
    async def pin(self, interaction: discord.Interaction):
        await interaction.response.defer()
        room = None
        rooms = await database.get_all_war_rooms(interaction.guild_id)
        for r in rooms:
            if r["channel_id"] == interaction.channel_id:
                room = r
                break
        if not room:
            await interaction.followup.send(
                embed=embeds.error("Not a War Room", "This command only works inside an auto-created war room channel.")
            )
            return

        try:
            war = await self.bot.pw_client.get_war(room["war_id"])
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Lookup Failed", f"P&W API error: `{e}`"))
            return
        if not war:
            await interaction.followup.send(embed=embeds.error("War Not Found", "This war no longer exists."))
            return

        embed = build_war_pin_embed(war)
        view = WarRoomUpdateView(self.bot, room["war_id"])
        msg = await interaction.followup.send(embed=embed, view=view)
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        await database.set_pin_message(room["war_id"], msg.id)

    @admin_group.command(name="syncwarrooms", description="Force-create missing war rooms and close ended ones")
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

    async def _create_room_for_war(self, guild: discord.Guild, war: dict, our_nation: dict, settings: dict) -> None:
        prefix = settings.get("war_category_prefix") or "WARCAT"
        num_cities = len(our_nation.get("cities") or []) or 1
        category_name = city_bracket_name(num_cities, prefix)
        category = await self._get_or_create_category(guild, category_name)

        enemy = war.get("attacker") if war.get("_side") == "defense" else war.get("defender")
        enemy_name = (enemy or {}).get("nation_name", "unknown")
        channel_name = f"war-{our_nation.get('nation_name', 'nation')}-vs-{enemy_name}"[:90]
        channel_name = channel_name.lower().replace(" ", "-")

        channel = await guild.create_text_channel(channel_name, category=category)
        await database.create_war_room(guild.id, war["id"], channel.id, category.id)

        embed = build_war_pin_embed(war)
        view = WarRoomUpdateView(self.bot, war["id"])
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        await database.set_pin_message(war["id"], msg.id)

    async def _sync_guild_warrooms(self, guild_id: int, guild: discord.Guild) -> tuple[int, int]:
        settings = await database.get_warroom_settings(guild_id)
        if not settings or not settings.get("enabled"):
            return 0, 0

        config = await database.get_alerts_config(guild_id)
        alliance_id = config.get("alliance_id") if config else None
        if not alliance_id:
            return 0, 0

        wars = await self.bot.pw_client.get_active_wars(alliance_id)
        active_war_ids = {w["id"] for w in wars}

        created = 0
        for war in wars:
            existing_room = await database.get_war_room(war["id"])
            if existing_room:
                continue
            our_nation = war.get("defender") if war.get("_side") == "defense" else war.get("attacker")
            await self._create_room_for_war(guild, war, our_nation or {}, settings)
            created += 1

        closed = 0
        existing_rooms = await database.get_all_war_rooms(guild_id)
        for room in existing_rooms:
            if room["war_id"] not in active_war_ids:
                channel = guild.get_channel(room["channel_id"])
                if channel:
                    try:
                        await channel.delete(reason="War ended")
                    except discord.HTTPException:
                        pass
                await database.delete_war_room(room["war_id"])
                closed += 1

        return created, closed

    @tasks.loop(seconds=ATTACK_TRACK_INTERVAL_SECONDS)
    async def track_attacks(self):
        try:
            configs = await database.get_all_alerts_configs()
            for config in configs:
                guild = self.bot.get_guild(config["guild_id"])
                if not guild:
                    continue
                settings = await database.get_warroom_settings(config["guild_id"])
                if not settings or not settings.get("enabled"):
                    continue

                try:
                    await self._sync_guild_warrooms(config["guild_id"], guild)
                except Exception as e:
                    print(f"[warroom] sync error: {e}")

                rooms = await database.get_all_war_rooms(config["guild_id"])
                for room in rooms:
                    try:
                        await self._post_new_attacks(guild, room)
                    except Exception as e:
                        print(f"[warroom] attack track error for war {room['war_id']}: {e}")
        except Exception as e:
            print(f"[warroom] track_attacks error: {e}")

    @track_attacks.before_loop
    async def before_track(self):
        await self.bot.wait_until_ready()

    async def _post_new_attacks(self, guild: discord.Guild, room: dict) -> None:
        channel = guild.get_channel(room["channel_id"])
        if not channel:
            return
        attacks = await self.bot.pw_client.get_war_attacks(room["war_id"])
        for attack in attacks:
            if await database.is_attack_seen(attack["id"]):
                continue
            label = ATTACK_TYPE_LABELS.get(attack.get("type"), attack.get("type", "Attack"))
            success = attack.get("success", "Unknown")
            await channel.send(f"**{label}** — Result: `{success}`")
            await database.mark_attack_seen(attack["id"])


async def setup(bot: commands.Bot):
    await bot.add_cog(WarRoom(bot))
