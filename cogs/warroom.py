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


async def get_our_nations_for_enemy(bot: commands.Bot, guild_id: int, enemy_nation_id: int) -> list[dict]:
    """Re-derives, fresh each call, which Evergreen/ally nations are
    currently fighting this specific enemy — used both when creating a
    room and when the Update button is pressed, so it always reflects
    who's actually in the fight right now."""
    config = await database.get_alerts_config(guild_id)
    alliance_id = config.get("alliance_id") if config else None
    if not alliance_id:
        return []
    try:
        wars = await bot.pw_client.get_active_wars(alliance_id)
    except Exception:
        return []
    result = []
    for war in wars:
        enemy = war.get("attacker") if war.get("_side") == "defense" else war.get("defender")
        our_nation = war.get("defender") if war.get("_side") == "defense" else war.get("attacker")
        if enemy and enemy.get("id") == enemy_nation_id and our_nation:
            result.append(our_nation)
    return result


class WarRoomUpdateView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, enemy_nation_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
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

        our_nations = await get_our_nations_for_enemy(self.bot, self.guild_id, self.enemy_nation_id)
        embed = build_room_pin_embed(enemy, our_nations)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("Updated.", ephemeral=True)


def build_room_pin_embed(enemy: dict, our_nations: list[dict] = None) -> discord.Embed:
    our_nations = our_nations or []
    embed = embeds.info(f"War Room: {enemy.get('nation_name', 'Unknown')}")
    embed.url = f"https://politicsandwar.com/nation/id={enemy.get('id')}"

    parts = [f"**Enemy:**\n{nation_block(enemy, None, None)}"]

    if our_nations:
        our_blocks = "\n\n".join(nation_block(n, None, None) for n in our_nations)
        parts.append(f"**Our Members In This Fight:**\n{our_blocks}")
    else:
        parts.append("*No members currently tracked in this fight.*")

    parts.append("*Tap Update for the latest figures.*")
    embed.description = "\n\n".join(parts)
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


ATTACK_FEED_MAX_AGE_MINUTES = 30
ATTACK_FEED_MAX_PER_CYCLE = 10

ATTACK_LABELS = {
    "GROUND": "⚔️ Ground Attack",
    "AIRVAIR": "✈️ Airstrike (Aircraft)",
    "AIRVINFRA": "✈️ Airstrike (Infrastructure)",
    "AIRVMONEY": "✈️ Airstrike (Money)",
    "AIRVSHIPS": "✈️ Airstrike (Ships)",
    "AIRVSOLDIERS": "✈️ Airstrike (Soldiers)",
    "AIRVTANKS": "✈️ Airstrike (Tanks)",
    "NAVAL": "🚢 Naval Attack",
    "NAVALVINFRA": "🚢 Naval Attack (Infrastructure)",
    "MISSILE": "🚀 Missile Strike",
    "MISSILEFAIL": "🚀 Missile Strike (Failed)",
    "NUKE": "☢️ Nuclear Strike",
    "NUKEFAIL": "☢️ Nuclear Strike (Failed)",
    "FORTIFY": "🛡️ Fortify",
    "PEACE": "🕊️ Peace Offer",
    "VICTORY": "🏁 Victory",
    "ALLIANCELOOT": "💰 Alliance Loot",
}

ATTACK_RESULTS = {
    0: "Utter Failure",
    1: "Pyrrhic Victory",
    2: "Moderate Success",
    3: "Immense Triumph",
}


ATTACK_LABELS.update({
    "NAVALVAIR": "🚢 Naval Attack (Aircraft)",
    "NAVALVGROUND": "🚢 Naval Attack (Ground)",
    "NAVALVSHIPS": "🚢 Naval Attack (Ships)",
})

NON_COMBAT_ATTACKS = {"FORTIFY", "PEACE", "VICTORY", "ALLIANCELOOT"}


def build_attack_embed(war: dict, attack: dict, when) -> discord.Embed:
    attacker = war.get("attacker") or {}
    defender = war.get("defender") or {}

    # The attack record only carries IDs, so work out which of the two
    # nations in this war actually made it.
    if str(attack.get("att_id")) == str(attacker.get("id")):
        actor, target = attacker, defender
    else:
        actor, target = defender, attacker

    # Green when our member made the attack, red when the enemy did.
    our_nation = attacker if war.get("_side") == "offense" else defender
    ours = str(actor.get("id")) == str(our_nation.get("id"))

    def link(n):
        return f"[{n.get('nation_name', '?')}](https://politicsandwar.com/nation/id={n.get('id')})"

    def ally(n):
        return (n.get("alliance") or {}).get("name") or "No alliance"

    attack_type = attack.get("type") or "Attack"
    label = ATTACK_LABELS.get(attack_type, f"⚔️ {attack_type}")

    lines = [f"{link(actor)} of **{ally(actor)}** → {link(target)} of **{ally(target)}**"]

    if attack_type not in NON_COMBAT_ATTACKS:
        lines.append(f"Result: **{ATTACK_RESULTS.get(attack.get('success'), 'Unknown')}**")

        loot = attack.get("moneystolen") or 0
        if loot > 0:
            lines.append(f"Looted: **${loot:,.0f}**")

        infra = attack.get("infradestroyed") or 0
        if infra > 0:
            value = attack.get("infra_destroyed_value") or 0
            before = attack.get("city_infra_before") or 0
            lines.append(
                f"Infrastructure destroyed: **{infra:,.2f}** (worth ${value:,.0f}), "
                f"previously {before:,.2f}"
            )

        lost = attack.get("improvementslost") or 0
        if lost > 0:
            lines.append(f"Improvements destroyed: **{lost}**")

    embed = discord.Embed(
        title=label,
        description="\n".join(lines),
        color=0x2E7D32 if ours else 0xC62828,
        timestamp=when,
    )
    embed.set_footer(text=f"{embeds.FOOTER_TEXT} · {'our attack' if ours else 'enemy attack'}")
    return embed


class WarRoom(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._missing_cycles: dict[tuple[int, int], int] = {}
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

        our_nations = await get_our_nations_for_enemy(self.bot, interaction.guild_id, room["enemy_nation_id"])
        embed = build_room_pin_embed(enemy, our_nations)
        view = WarRoomUpdateView(self.bot, interaction.guild_id, room["enemy_nation_id"])
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
                await channel.send(f"{member.mention} you're now in this war room — coordinate here.")
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
        ma_role_id = await database.get_guild_role(guild_id, "MA")
        if ma_role_id:
            ma_role = guild.get_role(ma_role_id)
            if ma_role:
                overwrites[ma_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
        await database.create_war_room(guild_id, enemy["id"], channel.id, category.id)

        our_nations = await get_our_nations_for_enemy(self.bot, guild_id, enemy["id"])
        embed = build_room_pin_embed(enemy, our_nations)
        view = WarRoomUpdateView(self.bot, guild_id, enemy["id"])
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

        # Grace period: an enemy must be missing for 3 CONSECUTIVE
        # sync cycles (~6 min) before we close its room. A single
        # missing cycle is treated as likely API flakiness/lag rather
        # than the war actually ending, to stop rooms from flapping
        # closed-then-recreated on a transient data hiccup.
        MISSING_THRESHOLD = 3
        closed = 0
        existing_rooms = await database.get_all_war_rooms(guild_id)
        
        # Force all active enemy IDs to int for safe lookups
        active_enemy_ids = {int(eid) for eid in by_enemy.keys()}

        for room in existing_rooms:
            enemy_id = int(room["enemy_nation_id"])
            
            # If enemy is actively fighting, reset missing count in Supabase
            if enemy_id in active_enemy_ids:
                if room.get("missing_count", 0) > 0:
                    await database.update_war_room_missing_count(guild_id, enemy_id, 0)
                continue

            # Enemy missing: increment persistent count
            current_missing = room.get("missing_count", 0) + 1
            if current_missing < MISSING_THRESHOLD:
                await database.update_war_room_missing_count(guild_id, enemy_id, current_missing)
                continue

            # Grace period expired (3 consecutive fails stored in DB) — safely close room
            channel = guild.get_channel(int(room["channel_id"]))
            if channel:
                try:
                    await channel.delete(reason="War ended: missing for 3 consecutive polls")
                except discord.HTTPException:
                    pass
            await database.delete_war_room(guild_id, enemy_id)
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
        from datetime import datetime, timezone, timedelta

        if not alliance_id:
            return
        rooms = await database.get_all_war_rooms(guild_id)
        if not rooms:
            return

        wars = await self.bot.pw_client.get_active_wars(alliance_id)

        # The P&W API returns IDs as text while our database stores them
        # as numbers, so everything is keyed as int here.
        wars_by_enemy: dict[int, list] = {}
        for war in wars:
            enemy = war.get("attacker") if war.get("_side") == "defense" else war.get("defender")
            if enemy and enemy.get("id"):
                wars_by_enemy.setdefault(int(enemy["id"]), []).append(war)

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ATTACK_FEED_MAX_AGE_MINUTES)

        for room in rooms:
            channel = guild.get_channel(int(room["channel_id"]))
            if not channel:
                continue

            fresh = []
            for war in wars_by_enemy.get(int(room["enemy_nation_id"]), []):
                try:
                    attacks = await self.bot.pw_client.get_war_attacks(war["id"])
                except Exception as e:
                    print(f"[warroom] get_war_attacks failed for war {war['id']}: {e}")
                    continue

                for attack in attacks:
                    try:
                        when = datetime.fromisoformat(attack["date"].replace("Z", "+00:00"))
                    except Exception:
                        continue
                    # Old history is skipped without being marked, so a
                    # new room never gets flooded with past attacks.
                    if when < cutoff:
                        continue
                    if await database.is_attack_seen(guild_id, int(attack["id"])):
                        continue
                    fresh.append((when, war, attack))

            if not fresh:
                continue

            fresh.sort(key=lambda item: item[0])
            batch = fresh[:ATTACK_FEED_MAX_PER_CYCLE]
            embeds_to_send = [build_attack_embed(war, attack, when) for when, war, attack in batch]
            try:
                await channel.send(embeds=embeds_to_send)
            except discord.HTTPException as e:
                print(f"[warroom] failed to post attack batch in {channel.name}: {e}")
                continue
            for _, _, attack in batch:
                await database.mark_attack_seen(guild_id, int(attack["id"]))



async def setup(bot: commands.Bot):
    await bot.add_cog(WarRoom(bot))
