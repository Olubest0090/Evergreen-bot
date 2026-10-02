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
from cogs.counter import qualifies, military_total
from utils.formatting import format_duration, military_line, nation_block

CHANNEL_TYPE_CHOICES = [
    app_commands.Choice(name="Defensive Wars", value="defense_channel_id"),
    app_commands.Choice(name="Offensive Wars", value="offensive_channel_id"),
    app_commands.Choice(name="Espionage", value="espionage_channel_id"),
    app_commands.Choice(name="MA Notifications", value="ma_notify_channel_id"),
]

POLL_INTERVAL_SECONDS = 90


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
            config = await database.get_alerts_config(interaction.guild_id)
            alliance_id = config.get("alliance_id") if config else None
            if not alliance_id:
                await interaction.followup.send(embed=embeds.error("No Alliance Set", "Set one with /alerts alliance."))
                return

            wars = await self.bot.pw_client.get_active_wars(alliance_id)
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
            self._handle_espionage_check(guild_id, config, member, alliance_id)
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
            await self._send_war_alert(guild_id, channel_id, war, side="defense", is_counter=is_counter)
            await database.mark_war_alerted(guild_id, war_id, "alerted_defense")
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

            await self._send_war_alert(
                guild_id, channel_id, war, side="offense", is_dnr_violation=is_violation
            )
            await database.mark_war_alerted(guild_id, war_id, "alerted_offensive")

    async def _send_war_alert(self, guild_id, channel_id, war, side, is_counter=False, is_dnr_violation=False):
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

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

    async def _dispatch_counter_requests(self, guild_id, config, war):
        attacker = war.get("attacker") or {}
        defender = war.get("defender") or {}
        members = getattr(self, "_cached_members", None) or []

        candidates = [
            m for m in members
            if m.get("id") != defender.get("id") and qualifies(m, attacker)
        ]
        candidates.sort(key=military_total, reverse=True)

        war_link = f"https://politicsandwar.com/nation/war/timeline/war={war['id']}"
        att_name = attacker.get("nation_name", "Unknown")
        att_alliance = (attacker.get("alliance") or {}).get("name", "None")

        unlinked = []
        sent = 0
        for m in candidates:
            discord_id = await database.get_discord_id_for_nation(m["id"])
            if not discord_id:
                unlinked.append(m.get("nation_name", "Unknown"))
                continue

            member_obj = self.bot.get_user(discord_id) or await self.bot.fetch_user(discord_id)
            if not member_obj:
                unlinked.append(m.get("nation_name", "Unknown"))
                continue

            embed = embeds.warning("🎯 Counter Request")
            embed.description = (
                f"**{defender.get('nation_name', 'Unknown')}** is under attack by "
                f"[{att_name}](https://politicsandwar.com/nation/id={attacker.get('id')}) ({att_alliance}).\n\n"
                f"You've been picked because your military meets or beats theirs and you have a free offensive slot.\n\n"
                f"**Enemy:**\n"
                f"`{attacker.get('soldiers', 0):,} 💂 | {attacker.get('tanks', 0):,} ⚙️ | "
                f"{attacker.get('aircraft', 0):,} ✈️ | {attacker.get('ships', 0):,} 🚢`\n\n"
                f"**War reason:** Evergreen Counter\n\n"
                f"If you can declare on them, please do. If not, let MA know so someone else gets asked.\n\n"
                f"War link: [Click here]({war_link})"
            )
            try:
                await member_obj.send(embed=embed)
                sent += 1
            except discord.Forbidden:
                unlinked.append(f"{m.get('nation_name', 'Unknown')} (DMs closed)")

        if unlinked:
            channel_id = config.get("ma_notify_channel_id") or config.get("defense_channel_id")
            channel = self.bot.get_channel(channel_id) if channel_id else None
            if channel:
                ma_role_id = await database.get_guild_role(guild_id, "MA")
                ping = f"<@&{ma_role_id}> " if ma_role_id else ""
                lines = "\n".join(f"- {n}" for n in unlinked)
                await channel.send(
                    content=ping,
                    embed=embeds.warning(
                        "Could Not DM These Qualifying Counters",
                        f"{lines}\n\nThese members qualify to counter **{att_name}** but have no linked "
                        f"Discord (or DMs closed). DMed **{sent}** others successfully.",
                    ),
                )

    async def _handle_espionage_check(self, guild_id, config, member, our_alliance_id):
        channel_id = config.get("espionage_channel_id")
        if not channel_id:
            return

        nation_id = member["id"]
        current_spies = member.get("spies", 0)
        last_spies = await database.get_last_spies(guild_id, nation_id)

        if last_spies is not None and current_spies < last_spies:
            channel = self.bot.get_channel(channel_id)
            if channel:
                discord_id = await database.get_discord_id_for_nation(nation_id)
                ping = f"<@{discord_id}> " if discord_id else ""

                score = member.get("score", 0)
                min_score, max_score = score / 2.5, score * 2.5

                suspects_text = "Couldn't determine suspects."
                suspects = []
                try:
                    candidates = await self.bot.pw_client.get_nations_in_score_range(min_score, max_score)
                    now = datetime.now(timezone.utc)
                    for c in candidates:
                        if c.get("alliance_id") == our_alliance_id:
                            continue
                        last_active_raw = c.get("last_active")
                        if not last_active_raw:
                            continue
                        try:
                            last_active = datetime.fromisoformat(last_active_raw.replace("Z", "+00:00"))
                        except Exception:
                            continue
                        if (now - last_active).total_seconds() <= 900:
                            c["_last_active_dt"] = last_active
                            suspects.append(c)

                    # Most recently active first — the most likely spy
                    # is whoever acted closest to the moment of the loss.
                    suspects.sort(key=lambda s: s["_last_active_dt"], reverse=True)
                except Exception as e:
                    suspects = None
                    suspect_error = str(e)

                embed = embeds.warning(
                    "🕵️ Possible Espionage Loss",
                    f"**{member.get('nation_name', 'Unknown')}** lost spies: "
                    f"{last_spies} → {current_spies}.\n"
                    f"This may indicate a successful enemy spy operation.",
                )

                if suspects is None:
                    embed.add_field(
                        name="Possible Suspects", value=f"Error looking up suspects: `{suspect_error}`", inline=False
                    )
                elif not suspects:
                    embed.add_field(
                        name="Possible Suspects (defensive spy range, active in last 15 min)",
                        value="None found.",
                        inline=False,
                    )
                else:
                    lines = [
                        f"[{s['nation_name']}](https://politicsandwar.com/nation/id={s['id']}) "
                        f"— *{(s.get('alliance') or {}).get('name', 'None')}* — "
                        f"{format_duration(s.get('last_active'))} ago"
                        for s in suspects
                    ]
                    # Split into embed fields of ~10 lines each so the
                    # FULL list always fits (a field caps at 1024 chars;
                    # multiple fields let us show everyone, not just a
                    # truncated "+N more").
                    chunk_size = 10
                    for i in range(0, len(lines), chunk_size):
                        chunk = lines[i : i + chunk_size]
                        field_name = (
                            "Possible Suspects (defensive spy range, active in last 15 min, most recent first)"
                            if i == 0 else "\u200b"
                        )
                        embed.add_field(name=field_name, value="\n".join(chunk), inline=False)

                await channel.send(content=ping or None, embed=embed)

        await database.set_last_spies(guild_id, nation_id, current_spies)


async def setup(bot: commands.Bot):
    await bot.add_cog(Alerts(bot))
