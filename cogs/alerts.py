"""
/alerts — configures which alliance to monitor and which channels
receive defensive/offensive war alerts and espionage alerts. Also runs
the background loop that polls the P&W API and posts those alerts.
"""

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import database, embeds

CHANNEL_TYPE_CHOICES = [
    app_commands.Choice(name="Defensive Wars", value="defense_channel_id"),
    app_commands.Choice(name="Offensive Wars", value="offensive_channel_id"),
    app_commands.Choice(name="Espionage", value="espionage_channel_id"),
]

POLL_INTERVAL_SECONDS = 90


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
            return f"{days}d{hours}h"
        if hours:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"
    except Exception:
        return "unknown"


def military_line(nation: dict) -> str:
    return (
        f"`{nation.get('soldiers', 0)} 💂 | {nation.get('tanks', 0)} ⚙️ | "
        f"{nation.get('aircraft', 0)} ✈️ | {nation.get('ships', 0)} 🚢 | "
        f"{nation.get('spies', 0)} 🔍`"
    )


def nation_block(nation: dict, resistance, maps) -> str:
    name = nation.get("nation_name", "Unknown")
    nation_id = nation.get("id")
    alliance_name = (nation.get("alliance") or {}).get("name", "None")
    position = (nation.get("alliance_position") or "").title() or "None"
    active = format_duration(nation.get("last_active"))
    nation_link = f"https://politicsandwar.com/nation/id={nation_id}" if nation_id else ""

    lines = [
        f"[**{name}**]({nation_link}) — *{alliance_name}* — {active} — {position}",
        military_line(nation),
    ]
    if resistance is not None:
        lines.append(f"Resistance: {resistance}/100")
    if maps is not None:
        lines.append(f"MAPs available: {maps}/12")
    return "\n".join(lines)


class Alerts(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_wars.start()

    def cog_unload(self):
        self.poll_wars.cancel()

    alerts_group = app_commands.Group(
        name="alerts",
        description="Configure war and espionage alerts",
        default_permissions=discord.Permissions(administrator=True),
    )

    @alerts_group.command(name="alliance", description="Set the P&W alliance ID to monitor")
    @app_commands.describe(alliance_id="Evergreen's Politics & War alliance ID")
    async def set_alliance(self, interaction: discord.Interaction, alliance_id: int):
        await database.set_alliance_id(interaction.guild_id, alliance_id)
        await interaction.response.send_message(
            embed=embeds.success(
                "Alliance Set", f"Now monitoring alliance ID `{alliance_id}` for wars and espionage."
            )
        )

    @alerts_group.command(name="config", description="Set the channel for a type of alert")
    @app_commands.choices(alert_type=CHANNEL_TYPE_CHOICES)
    @app_commands.describe(alert_type="Which alert type this channel receives", channel="The channel to post alerts in")
    async def config(
        self,
        interaction: discord.Interaction,
        alert_type: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        await database.set_alert_channel(interaction.guild_id, alert_type.value, channel.id)
        await interaction.response.send_message(
            embed=embeds.success(
                "Alert Channel Set", f"**{alert_type.name}** alerts will now be posted in {channel.mention}."
            )
        )

    @alerts_group.command(name="list", description="Show current alert configuration")
    async def list_config(self, interaction: discord.Interaction):
        config = await database.get_alerts_config(interaction.guild_id)
        if not config:
            await interaction.response.send_message(
                embed=embeds.info("No Configuration", "Use `/alerts alliance` and `/alerts config` to get started.")
            )
            return

        def fmt_channel(cid):
            return f"<#{cid}>" if cid else "*not set*"

        lines = [
            f"**Alliance ID:** {config.get('alliance_id') or '*not set*'}",
            f"**Defensive War Alerts:** {fmt_channel(config.get('defense_channel_id'))}",
            f"**Offensive War Alerts:** {fmt_channel(config.get('offensive_channel_id'))}",
            f"**Espionage Alerts:** {fmt_channel(config.get('espionage_channel_id'))}",
        ]
        await interaction.response.send_message(
            embed=embeds.info("Alert Configuration", "\n".join(lines))
        )

    @alerts_group.command(name="refresh", description="Force an immediate check for new wars/espionage")
    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self._poll_guild(interaction.guild_id)
            await interaction.followup.send(embed=embeds.success("Refreshed", "Checked for new wars and espionage."))
        except Exception as e:
            await interaction.followup.send(embed=embeds.error("Refresh Failed", str(e)))

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_wars(self):
        try:
            configs = await database.get_all_alerts_configs()
            for config in configs:
                if config.get("alliance_id"):
                    await self._poll_guild(config["guild_id"], config)
        except Exception as e:
            print(f"[alerts] poll error: {e}")

    @poll_wars.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _poll_guild(self, guild_id: int, config: dict | None = None):
        config = config or await database.get_alerts_config(guild_id)
        if not config or not config.get("alliance_id"):
            return
        alliance_id = config["alliance_id"]
        pw_client = self.bot.pw_client

        members = await pw_client.get_alliance_members(alliance_id)
        member_positions = {m["id"]: m.get("alliance_position") for m in members}

        wars = await pw_client.get_active_wars(alliance_id)
        for war in wars:
            await self._handle_war(guild_id, config, alliance_id, war, member_positions)

        await asyncio.gather(*[
            self._handle_espionage_check(guild_id, config, member)
            for member in members
        ], return_exceptions=True)

    async def _handle_war(self, guild_id, config, alliance_id, war, member_positions):
        war_id = war["id"]
        is_defense = war.get("def_alliance_id") == alliance_id
        is_offense = war.get("att_alliance_id") == alliance_id

        if is_defense:
            defender = war.get("defender") or {}
            position = member_positions.get(defender.get("id"), "APPLICANT")
            if position == "APPLICANT":
                return
            if await database.is_war_alerted(war_id, "alerted_defense"):
                return
            channel_id = config.get("defense_channel_id")
            if not channel_id:
                return
            await self._send_war_alert(guild_id, channel_id, war, side="defense")
            await database.mark_war_alerted(war_id, "alerted_defense")

        elif is_offense:
            if await database.is_war_alerted(war_id, "alerted_offensive"):
                return
            channel_id = config.get("offensive_channel_id")
            if not channel_id:
                return
            await self._send_war_alert(guild_id, channel_id, war, side="offense")
            await database.mark_war_alerted(war_id, "alerted_offensive")

    async def _send_war_alert(self, guild_id, channel_id, war, side):
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        attacker = war.get("attacker") or {}
        defender = war.get("defender") or {}
        our_nation = defender if side == "defense" else attacker

        pings = []
        our_discord_id = await database.get_discord_id_for_nation(our_nation.get("id"))
        if our_discord_id:
            pings.append(f"<@{our_discord_id}>")

        if side == "defense":
            ma_role_id = await database.get_guild_role(guild_id, "MA")
            if ma_role_id:
                pings.append(f"<@&{ma_role_id}>")
            embed = embeds.warning("🛡️ Defensive War Started")
        else:
            embed = embeds.info("⚔️ Offensive War Started")

        war_type = war.get("war_type", "Unknown")
        turns_left = war.get("turns_left", "?")
        war_link = f"https://politicsandwar.com/nation/war/timeline/war={war['id']}"

        att_block = nation_block(attacker, war.get("att_resistance"), war.get("att_points"))
        def_block = nation_block(defender, war.get("def_resistance"), war.get("def_points"))

        embed.description = (
            f"**{attacker.get('nation_name', 'Unknown')} > {defender.get('nation_name', 'Unknown')}** "
            f"— {war_type} — ACTIVE\n\n"
            f"Link: [Click here]({war_link})\n\n"
            f"{att_block}\n\n"
            f"{def_block}\n\n"
            f"**Turns Left:** {turns_left}/60"
        )
        content = " ".join(pings) if pings else None
        await channel.send(content=content, embed=embed)

    async def _handle_espionage_check(self, guild_id, config, member):
        channel_id = config.get("espionage_channel_id")
        if not channel_id:
            return

        nation_id = member["id"]
        current_spies = member.get("spies", 0)
        last_spies = await database.get_last_spies(nation_id)

        if last_spies is not None and current_spies < last_spies:
            channel = self.bot.get_channel(channel_id)
            if channel:
                discord_id = await database.get_discord_id_for_nation(nation_id)
                ping = f"<@{discord_id}> " if discord_id else ""
                embed = embeds.warning(
                    "🕵️ Possible Espionage Loss",
                    f"**{member.get('nation_name', 'Unknown')}** lost spies: "
                    f"{last_spies} → {current_spies}.\n"
                    f"This may indicate a successful enemy spy operation.",
                )
                await channel.send(content=ping or None, embed=embed)

        await database.set_last_spies(nation_id, current_spies)


async def setup(bot: commands.Bot):
    await bot.add_cog(Alerts(bot))
