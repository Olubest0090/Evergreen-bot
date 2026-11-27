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
from cogs.counter import qualifies, military_total, rank_counter_candidates
from utils.formatting import format_duration, military_line, nation_block
from utils.spy_suspects import find_possible_spy_suspects
from utils.spy_alerts import handle_espionage_check
from utils.pw_spy import probe_spy_counters, describe_spy_probe

CHANNEL_TYPE_CHOICES = [
    app_commands.Choice(name="Defensive Wars", value="defense_channel_id"),
    app_commands.Choice(name="Offensive Wars", value="offensive_channel_id"),
    app_commands.Choice(name="Espionage", value="espionage_channel_id"),
    app_commands.Choice(name="MA Notifications", value="ma_notify_channel_id"),
]

POLL_INTERVAL_SECONDS = 90
MAX_COUNTER_DMS = 5  # auto-counter DMs per new defensive war


def is_inactive(last_active_iso: str | None, days: int = 7) -> bool:
    # Missing or unreadable data counts as active, so an alert is
    # never hidden because the API left a field out.
    if not last_active_iso:
        return False
    try:
        last = datetime.fromisoformat(last_active_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).days >= days
    except Exception:
        return False


class Alerts(commands.Cog):
    async def _get_channel(self, channel_id):
        """Cache lookup first, falls back to a direct API fetch. The
        cache can miss right after a restart before the gateway has
        finished syncing a server's channels, which previously caused
        alerts to silently vanish and get marked as sent anyway."""
        channel = self.bot.get_channel(channel_id)
        if channel:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._members_by_guild: dict = {}
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

    @alerts_group.command(name="includeallies", description="Include ALLIES-bloc alliances in this server's war/espionage alerts")
    @app_commands.describe(enabled="If true, alerts here cover Evergreen + every alliance in the ALLIES bloc")
    async def include_allies(self, interaction: discord.Interaction, enabled: bool):
        config = await database.get_alerts_config(interaction.guild_id) or {}
        alliance_id = config.get("alliance_id")
        if not alliance_id:
            await interaction.response.send_message(
                embed=embeds.error("No Alliance Set", "Set one first with /alerts alliance."), ephemeral=True
            )
            return
        await database._upsert_alerts_config(interaction.guild_id, include_allied_alliances=enabled)
        await interaction.response.send_message(
            embed=embeds.success(
                "Setting Updated",
                f"This server's war/espionage alerts will {'now cover Evergreen + the ALLIES bloc' if enabled else 'now cover Evergreen only'}.",
            )
        )

    @alerts_group.command(name="autocounter", description="Toggle automatic counter-request DMs on defensive wars")
    @app_commands.describe(enabled="True = send counter DMs, False = disable them")
    async def autocounter(self, interaction: discord.Interaction, enabled: bool):
        await database._upsert_alerts_config(interaction.guild_id, auto_counter_enabled=enabled)
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=embeds.success(
                "Auto-Counter Updated",
                f"Automatic counter-request DMs are now **{status}**.",
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
            f"**Auto-Counter DMs:** {'ON' if config.get('auto_counter_enabled', True) else 'OFF'}",
        ]
        await interaction.response.send_message(
            embed=embeds.info("Alert Configuration", "\n".join(lines))
        )

    @alerts_group.command(name="spykills", description="Test whether the API shows a nation's spy kill/loss counters")
    @app_commands.describe(nation_id="Politics & War nation ID to test")
    async def spykills(self, interaction: discord.Interaction, nation_id: int):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await probe_spy_counters(self.bot.pw_client, nation_id)
            await interaction.followup.send(
                embed=embeds.info("Spy Counter Test", describe_spy_probe(result)[:3500]),
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error("Spy Counter Test Failed", str(e)[:900]),
                ephemeral=True,
            )

    @alerts_group.command(name="refresh", description="Force an immediate check for new wars/espionage")
    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            config = await database.get_alerts_config(interaction.guild_id)
            alliance_id = config.get("alliance_id") if config else None
            if not alliance_id:
                await interaction.followup.send(embed=embeds.error("No Alliance Set", "Set one with /alerts alliance."))
                return

            wars = await self.bot.pw_client.get_active_wars(alliance_id, fresh=True)
            defense_count = sum(1 for w in wars if w.get("_side") == "defense")
            offense_count = sum(1 for w in wars if w.get("_side") == "offense")

            await self._poll_guild(interaction.guild_id, config)

            await interaction.followup.send(
                embed=embeds.success(
                    "Refreshed",
                    f"Found **{len(wars)}** active wars for alliance `{alliance_id}`.\n"
                    f"Defense (our member is defender): **{defense_count}**\n"
                    f"Offense (our member is attacker): **{offense_count}**",
                )
            )
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
        primary_alliance_id = config["alliance_id"]
        pw_client = self.bot.pw_client

        # Only expand to cover ALLIES-bloc alliances if this guild has
        # explicitly opted in (e.g. a shared coalition server). Every
        # other guild watches its own primary alliance only.
        alliance_ids = [primary_alliance_id]
        if config.get("include_allied_alliances"):
            ally_rows = await database.list_coalitions(guild_id, "ALLIES")
            alliance_ids += [r["alliance_id"] for r in ally_rows if r["alliance_id"] != primary_alliance_id]

        members = []
        wars_by_id: dict[int, dict] = {}
        for aid in alliance_ids:
            try:
                members.extend(await pw_client.get_alliance_members(aid))
                for w in await pw_client.get_active_wars(aid):
                    wars_by_id[w["id"]] = w  # last write wins on dupes, fine since data is identical
            except Exception as e:
                print(f"[alerts] failed to fetch alliance {aid} for guild {guild_id}: {e}")
        wars = list(wars_by_id.values())
        self._members_by_guild[int(guild_id)] = members

        # A defensive war is a "counter" if OUR member (the defender)
        # currently has an active offensive war against SOMEONE IN THE
        # SAME ALLIANCE as this new attacker.
        our_offense_by_member: dict[int, set[int]] = {}
        for w in wars:
            if w.get("_side") == "offense":
                our_nation_id = w.get("att_id")
                enemy_alliance_id = (w.get("defender") or {}).get("alliance_id")
                if our_nation_id and enemy_alliance_id:
                    our_offense_by_member.setdefault(our_nation_id, set()).add(enemy_alliance_id)

        for war in wars:
            is_counter = False
            if war.get("_side") == "defense":
                our_nation_id = war.get("def_id")
                enemy_alliance_id = (war.get("attacker") or {}).get("alliance_id")
                is_counter = enemy_alliance_id in our_offense_by_member.get(our_nation_id, set())
            await self._handle_war(guild_id, config, war, is_counter)

        await asyncio.gather(*[
            self._handle_espionage_check(guild_id, config, member, primary_alliance_id)
            for member in members
        ], return_exceptions=True)

    async def _handle_war(self, guild_id, config, war, is_counter=False):
        from cogs.coalitions import is_dnr_protected

        war_id = war["id"]
        side = war.get("_side")
        position = war.get("_our_position") or "APPLICANT"

        # Sphere Bloc only (servers that include allied alliances):
        # skip DEFENSIVE alerts for members inactive 7+ days. Not
        # marked as alerted, so it fires if they log back in mid-war.
        if side == "defense" and config.get("include_allied_alliances"):
            if is_inactive((war.get("defender") or {}).get("last_active")):
                return

        if side == "defense":
            if position == "APPLICANT":
                return
            if await database.is_war_alerted(guild_id, war_id, "alerted_defense"):
                return
            channel_id = config.get("defense_channel_id")
            if not channel_id:
                return
            sent = await self._send_war_alert(guild_id, channel_id, war, side="defense", is_counter=is_counter)
            if not sent:
                return  # channel unreachable this cycle, retry next time instead of marking as done
            await database.mark_war_alerted(guild_id, war_id, "alerted_defense")
            if config.get("auto_counter_enabled", True):
                await self._dispatch_counter_requests(guild_id, config, war)

        elif side == "offense":
            if await database.is_war_alerted(guild_id, war_id, "alerted_offensive"):
                return
            channel_id = config.get("offensive_channel_id")
            if not channel_id:
                return

            defender = war.get("defender") or {}
            defender_alliance_id = defender.get("alliance_id")
            defender_position = defender.get("alliance_position")
            is_violation = False
            if defender_alliance_id:
                try:
                    is_violation = await is_dnr_protected(self.bot, guild_id, defender_alliance_id, defender_position)
                except Exception:
                    pass

            sent = await self._send_war_alert(
                guild_id, channel_id, war, side="offense", is_dnr_violation=is_violation
            )
            if not sent:
                return
            await database.mark_war_alerted(guild_id, war_id, "alerted_offensive")

    async def _send_war_alert(self, guild_id, channel_id, war, side, is_counter=False, is_dnr_violation=False):
        channel = await self._get_channel(channel_id)
        if not channel:
            return False

        attacker = war.get("attacker") or {}
        defender = war.get("defender") or {}
        our_nation = defender if side == "defense" else attacker

        pings = []

        if side == "defense":
            our_discord_id = await database.get_discord_id_for_nation(our_nation.get("id"))
            if our_discord_id:
                pings.append(f"<@{our_discord_id}>")
            ma_role_id = await database.get_guild_role(guild_id, "MA")
            if ma_role_id:
                pings.append(f"<@&{ma_role_id}>")
            if is_counter:
                embed = embeds.warning("↩️ COUNTER-ATTACK — Defensive War Started")
            else:
                embed = embeds.warning("🛡️ Defensive War Started")
        else:
            if is_dnr_violation:
                our_discord_id = await database.get_discord_id_for_nation(our_nation.get("id"))
                if our_discord_id:
                    pings.append(f"<@{our_discord_id}>")
                fa_role_id = await database.get_guild_role(guild_id, "FA")
                if fa_role_id:
                    pings.append(f"<@&{fa_role_id}>")
                embed = embeds.error("🚫 DNR VIOLATION — Offensive War Started")
            else:
                embed = embeds.info("⚔️ Offensive War Started")

        war_type = war.get("war_type", "Unknown")
        turns_left = war.get("turns_left", "?")
        war_link = f"https://politicsandwar.com/nation/war/timeline/war={war['id']}"

        att_block = nation_block(attacker, war.get("att_resistance"), war.get("att_points"))
        def_block = nation_block(defender, war.get("def_resistance"), war.get("def_points"))

        counter_note = (
            "\n⚠️ **This appears to be a counter-attack** — the attacker is "
            "currently being fought by one of our members in an offensive war.\n"
            if is_counter else ""
        )
        violation_note = (
            "\n🚫 **This is a DNR VIOLATION** — the target is protected.\n"
            if is_dnr_violation else ""
        )

        embed.description = (
            f"**{attacker.get('nation_name', 'Unknown')} > {defender.get('nation_name', 'Unknown')}** "
            f"— {war_type} — ACTIVE\n"
            f"{counter_note}{violation_note}\n"
            f"Link: [Click here]({war_link})\n\n"
            f"{att_block}\n\n"
            f"{def_block}\n\n"
            f"**Turns Left:** {turns_left}/60"
        )
        content = " ".join(pings) if pings else None
        await channel.send(content=content, embed=embed)
        return True

    async def _dispatch_counter_requests(self, guild_id, config, war):
        """DM the strongest eligible counters (same ranking as /counter nation)
        and tell MA who was reached. Keeps going down the ranked list until
        MAX_COUNTER_DMS members have actually received a DM."""
        attacker = war.get("attacker") or {}
        defender = war.get("defender") or {}
        members = self._members_by_guild.get(int(guild_id), [])

        ranked = rank_counter_candidates(members, attacker, exclude_id=defender.get("id"))

        war_link = f"https://politicsandwar.com/nation/war/timeline/war={war['id']}"
        att_name = attacker.get("nation_name", "Unknown")
        att_link = f"https://politicsandwar.com/nation/id={attacker.get('id')}"
        att_alliance = (attacker.get("alliance") or {}).get("name", "None")

        reached = []     # (member dict, discord id)
        unreached = []   # readable reasons, in ranking order
        for m in ranked:
            if len(reached) >= MAX_COUNTER_DMS:
                break
            name = m.get("nation_name", "Unknown")
            discord_id = await database.get_discord_id_for_nation(m["id"])
            if not discord_id:
                unreached.append(f"{name} (no linked Discord)")
                continue
            try:
                member_obj = self.bot.get_user(discord_id) or await self.bot.fetch_user(discord_id)
            except discord.HTTPException:
                member_obj = None
            if not member_obj:
                unreached.append(f"{name} (Discord user not found)")
                continue

            embed = embeds.warning("🎯 Counter Request")
            embed.description = (
                f"**{defender.get('nation_name', 'Unknown')}** is under attack by "
                f"[{att_name}]({att_link}) ({att_alliance}).\n\n"
                f"You're one of our strongest members in range of them with a free offensive slot.\n\n"
                f"**Enemy:**\n"
                f"`{attacker.get('soldiers', 0):,} 💂 | {attacker.get('tanks', 0):,} ⚙️ | "
                f"{attacker.get('aircraft', 0):,} ✈️ | {attacker.get('ships', 0):,} 🚢`\n\n"
                f"**War reason:** Evergreen Counter\n\n"
                f"If you can declare on them, please do. If not, let MA know so someone else gets asked.\n\n"
                f"Enemy: [{att_name}]({att_link})\n"
                f"War link: [Click here]({war_link})"
            )
            try:
                await member_obj.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                unreached.append(f"{name} (DMs closed)")
                continue
            reached.append((m, discord_id))

        await self._post_counter_notice(guild_id, config, war, ranked, reached, unreached)

    async def _post_counter_notice(self, guild_id, config, war, ranked, reached, unreached):
        """Tell MA what happened with the counter DMs for this war."""
        channel_id = config.get("ma_notify_channel_id") or config.get("defense_channel_id")
        channel = await self._get_channel(channel_id) if channel_id else None
        if not channel:
            return

        attacker = war.get("attacker") or {}
        defender = war.get("defender") or {}
        att_name = attacker.get("nation_name", "Unknown")
        att_link = f"https://politicsandwar.com/nation/id={attacker.get('id')}"
        war_link = f"https://politicsandwar.com/nation/war/timeline/war={war['id']}"
        ma_role_id = await database.get_guild_role(guild_id, "MA")
        ping = f"<@&{ma_role_id}>" if ma_role_id else None

        header = (
            f"**{defender.get('nation_name', 'Unknown')}** was attacked by "
            f"[{att_name}]({att_link}) — [war link]({war_link})\n\n"
        )

        def listed(items, limit=10):
            lines = [f"- {x}" for x in items[:limit]]
            if len(items) > limit:
                lines.append(f"- …and {len(items) - limit} more")
            return "\n".join(lines)

        if not ranked:
            embed = embeds.warning(
                "No Qualifying Counters",
                header + "No member is in range with a free offensive slot and activity in the last "
                "7 days. MA needs to find a counter manually.",
            )
            content = ping
        elif not reached:
            embed = embeds.warning(
                "No Counters Reached",
                header + f"{len(ranked)} member(s) qualified but none could be DMed:\n"
                + listed(unreached),
            )
            content = ping
        else:
            lines = []
            for m, discord_id in reached:
                url = f"https://politicsandwar.com/nation/id={m.get('id')}"
                lines.append(
                    f"✅ [{m.get('nation_name', 'Unknown')}]({url}) (<@{discord_id}>) — DM sent successfully"
                )
            body = header + f"**DM sent successfully to {len(reached)} counter(s):**\n" + "\n".join(lines)
            if len(reached) < MAX_COUNTER_DMS:
                body += f"\n\nOnly {len(reached)} of {MAX_COUNTER_DMS} could be reached."
            if unreached:
                body += "\n\n**Could not reach:**\n" + listed(unreached)
                embed = embeds.warning("Counter DMs Sent (some not reached)", body)
                content = ping
            else:
                embed = embeds.success("Counter DMs Sent", body)
                content = None

        try:
            await channel.send(content=content, embed=embed)
        except discord.HTTPException as e:
            print(f"[alerts] could not post counter notice for guild {guild_id}: {e}")

    async def _handle_espionage_check(self, guild_id, config, member, our_alliance_id):
        await handle_espionage_check(self, guild_id, config, member, our_alliance_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Alerts(bot))
