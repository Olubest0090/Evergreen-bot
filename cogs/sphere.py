"""Federal Sphere administration and centralized war/spy monitoring."""
from __future__ import annotations
import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands, tasks
from utils import sphere_db

log=logging.getLogger("evergreen.sphere")
POLL_SECONDS=120


class Sphere(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self._poll_lock=asyncio.Lock()
        self._last_discord_send=0.0
        self.monitor.start()

    def cog_unload(self):
        self.monitor.cancel()

    sphere=app_commands.Group(name="sphere",description="Federal Sphere administration")
    alliance=app_commands.Group(name="alliance",description="Manage registered sphere alliances",parent=sphere)

    async def _send_channel(self, channel_id: int | None, *, content: str | None=None, embed: discord.Embed | None=None):
        if not channel_id:
            return False
        channel=self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel=await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound,discord.Forbidden,discord.HTTPException):
                return False
        try:
            # Pace outbound messages across all three sphere channels. This
            # avoids bursty notification traffic when several wars are found.
            now=asyncio.get_running_loop().time()
            wait=max(0.0,1.25-(now-self._last_discord_send))
            if wait:
                await asyncio.sleep(wait)
            await channel.send(content=content,embed=embed,allowed_mentions=discord.AllowedMentions.none())
            self._last_discord_send=asyncio.get_running_loop().time()
            return True
        except (discord.Forbidden,discord.HTTPException):
            log.exception("Could not send sphere notification to channel %s",channel_id)
            return False

    @alliance.command(name="add",description="Register an alliance in the Federal Sphere")
    @app_commands.describe(alliance="Politics & War alliance name or ID")
    @app_commands.checks.has_permissions(administrator=True)
    async def add_alliance(self, interaction: discord.Interaction, alliance: str):
        await interaction.response.defer(ephemeral=True)
        try:
            found=await self.bot.pw_client.get_alliance_by_id_or_name(alliance)
            if not found:
                await interaction.followup.send("I couldn't find that alliance in Politics & War.",ephemeral=True)
                return
            await sphere_db.add_alliance(interaction.guild_id,int(found["id"]),found["name"],found.get("score"))
            await interaction.followup.send(f"Registered **{found['name']}** (`{found['id']}`) in the Federal Sphere. Monitoring will include this alliance on the next cycle.",ephemeral=True)
        except Exception as exc:
            log.exception("Sphere alliance registration failed")
            await interaction.followup.send(f"Registration failed: `{str(exc)[:500]}`. Check that the Sphere database migration has been applied.",ephemeral=True)

    @alliance.command(name="remove",description="Stop monitoring an alliance without deleting its history")
    @app_commands.describe(alliance_id="Politics & War alliance ID")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_alliance(self, interaction: discord.Interaction, alliance_id: int):
        try:
            await sphere_db.remove_alliance(interaction.guild_id,alliance_id)
            await interaction.response.send_message(f"Alliance `{alliance_id}` has been deactivated. Historical records are preserved.",ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"Could not remove alliance: `{str(exc)[:400]}`",ephemeral=True)

    @alliance.command(name="list",description="List active sphere alliances")
    async def list_alliances(self, interaction: discord.Interaction):
        try:
            rows=await sphere_db.list_alliances(interaction.guild_id)
            rows=[r for r in rows if r.get("active",True)]
            if not rows:
                await interaction.response.send_message("No sphere alliances registered yet.",ephemeral=True)
                return
            embed=discord.Embed(title="Federal Sphere — Registered Alliances",color=discord.Color.blurple())
            embed.description="\n".join(f"• **{r['alliance_name']}** — `{r['alliance_id']}`" for r in rows)
            await interaction.response.send_message(embed=embed,ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"Could not load alliance registry: `{str(exc)[:400]}`",ephemeral=True)

    @sphere.command(name="channel",description="Configure a sphere notification channel")
    @app_commands.choices(channel_type=[app_commands.Choice(name="Offensive wars",value="offensive_channel_id"),app_commands.Choice(name="Defensive wars",value="defensive_channel_id"),app_commands.Choice(name="Spy activity",value="spy_channel_id")])
    @app_commands.checks.has_permissions(administrator=True)
    async def set_channel(self, interaction: discord.Interaction, channel_type: app_commands.Choice[str], channel: discord.TextChannel):
        try:
            await sphere_db.set_channel(interaction.guild_id,channel_type.value,channel.id)
            await interaction.response.send_message(f"Sphere **{channel_type.name}** notifications will be sent to {channel.mention}.",ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"Channel setup failed: `{str(exc)[:400]}`. Apply the Sphere database migration first.",ephemeral=True)

    @sphere.command(name="status",description="Show sphere monitoring configuration")
    async def status(self, interaction: discord.Interaction):
        try:
            rows=await sphere_db.list_alliances(interaction.guild_id)
            rows=[r for r in rows if r.get("active",True)]
            cfg=await sphere_db.get_config(interaction.guild_id) or {}
            def mention(k): return f"<#{cfg[k]}>" if cfg.get(k) else "Not configured"
            e=discord.Embed(title="Federal Sphere Monitor",color=discord.Color.blurple())
            e.add_field(name="Registered alliances",value=str(len(rows)),inline=True)
            e.add_field(name="Monitor interval",value=f"{POLL_SECONDS} seconds",inline=True)
            e.add_field(name="Offensive war channel",value=mention("offensive_channel_id"),inline=False)
            e.add_field(name="Defensive war channel",value=mention("defensive_channel_id"),inline=False)
            e.add_field(name="Spy activity channel",value=mention("spy_channel_id"),inline=False)
            await interaction.response.send_message(embed=e,ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"Could not load sphere status: `{str(exc)[:400]}`",ephemeral=True)

    @tasks.loop(seconds=POLL_SECONDS)
    async def monitor(self):
        if self._poll_lock.locked():
            return
        async with self._poll_lock:
            try:
                # This first version is intentionally scoped to the guild where
                # the sphere commands are configured. Multiple guild support is
                # retained in the database key design for a later phase.
                for guild in self.bot.guilds:
                    await self._monitor_guild(guild.id)
            except Exception:
                log.exception("Sphere monitor cycle failed")

    @monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    async def _monitor_guild(self,guild_id: int):
        alliances=await sphere_db.list_alliances(guild_id)
        alliances=[a for a in alliances if a.get("active",True)]
        if not alliances:
            return
        cfg=await sphere_db.get_config(guild_id) or {}
        for alliance in alliances:
            aid=int(alliance["alliance_id"])
            try:
                members=await self.bot.pw_client.get_alliance_members(aid)
                wars=await self.bot.pw_client.get_active_wars(aid)
            except Exception:
                log.exception("Could not retrieve sphere data for alliance %s",aid)
                continue
            for war in wars:
                try:
                    await sphere_db.upsert_war(guild_id,aid,alliance["alliance_name"],war)
                    side=war.get("_side")
                    field="offensive_alerted" if side=="offense" else "defensive_alerted"
                    channel_key="offensive_channel_id" if side=="offense" else "defensive_channel_id"
                    if await sphere_db.was_war_alerted(guild_id,aid,int(war["id"]),field):
                        continue
                    attacker=war.get("attacker") or {}; defender=war.get("defender") or {}
                    sphere_nation=attacker if side=="offense" else defender
                    opponent=defender if side=="offense" else attacker
                    label="OFFENSIVE WAR" if side=="offense" else "DEFENSIVE WAR"
                    embed=discord.Embed(title=f"⚔️ Sphere {label}",color=discord.Color.orange() if side=="offense" else discord.Color.red())
                    embed.description=(f"**Sphere alliance:** {alliance['alliance_name']} (`{aid}`)\n"
                        f"**Sphere nation:** {sphere_nation.get('nation_name','Unknown')}\n"
                        f"**Opponent:** {opponent.get('nation_name','Unknown')}\n"
                        f"**Opponent alliance ID:** {opponent.get('alliance_id') or 'None'}\n"
                        f"**War type:** {war.get('war_type','Unknown')}\n"
                        f"**Turns left:** {war.get('turns_left','?')}\n"
                        f"[Open war](https://politicsandwar.com/nation/war/timeline/war={war['id']})")
                    sent=await self._send_channel(cfg.get(channel_key),embed=embed)
                    if sent:
                        await sphere_db.mark_war_alerted(guild_id,aid,int(war["id"]),field)
                except Exception:
                    log.exception("Failed processing sphere war %s",war.get("id"))
            # Spy monitoring: a drop in a member's reported spy count creates
            # one alert. First observation only establishes a baseline.
            if cfg.get("spy_channel_id"):
                for member in members:
                    try:
                        nation_id=int(member["id"]); current=int(member.get("spies") or 0)
                        previous=await sphere_db.get_spy_baseline(guild_id,nation_id)
                        if previous is not None and current < previous:
                            lost=previous-current
                            e=discord.Embed(title="🕵️ Sphere Spy Activity Detected",color=discord.Color.dark_gold())
                            e.description=(f"**Alliance:** {alliance['alliance_name']} (`{aid}`)\n"
                                f"**Nation:** {member.get('nation_name','Unknown')} (`{nation_id}`)\n"
                                f"**Reported spy count:** {previous} → {current}\n"
                                f"**Change:** -{lost} spies\n"
                                "A decrease was detected in the latest Politics & War data. This does not identify the attacker or prove which operation caused it.")
                            await self._send_channel(cfg.get("spy_channel_id"),embed=e)
                        await sphere_db.set_spy_baseline(guild_id,aid,nation_id,member.get("nation_name","Unknown"),current)
                    except Exception:
                        log.exception("Spy tracking failed for alliance %s",aid)

    @alliance.error
    async def alliance_error(self,interaction: discord.Interaction,error: app_commands.AppCommandError):
        if isinstance(error,app_commands.MissingPermissions):
            msg="You need server Administrator permission to manage the sphere alliance registry."
        else:
            msg=f"Sphere command failed: {error}"
        if interaction.response.is_done(): await interaction.followup.send(msg,ephemeral=True)
        else: await interaction.response.send_message(msg,ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Sphere(bot))
