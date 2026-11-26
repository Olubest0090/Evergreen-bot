"""
/audit and /roster — alliance compliance checks for MILCOM.

    /audit configure   pick the alliance to audit and the channel for results
    /audit daily       turn the automatic daily audit on or off
    /audit run         run the full audit now and post it to the audit channel
    /roster improvements   who is missing military improvements
    /roster readiness      who is below 100% military
    /roster activity       how active the members are

Allowed for administrators and the registered Military Affairs (MA) role.
The daily audit runs once per game day, 1 hour after the day change
(01:00 UTC). Check logic lives in utils/audit_checks.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import audit_checks as checks
from utils import audit_data, audit_db, database, embeds
from utils.pw_api import PWApiError

log = logging.getLogger("evergreen-bot.audit")

DAILY_RUN_HOUR_UTC = 1
MAX_EMBEDS_PER_REPORT = 4
CHUNK_LIMIT = 3800


# ---------------------------------------------------------------- embeds

def _chunk_lines(lines: list[str], limit: int = CHUNK_LIMIT) -> list[str]:
    chunks, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) + 1 > limit:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _paged(title: str, intro: str, lines: list[str], ok_title: str, ok_text: str,
           make=embeds.warning) -> list[discord.Embed]:
    if not lines:
        return [embeds.success(ok_title, ok_text)]
    chunks = _chunk_lines(lines)
    out = []
    for i, chunk in enumerate(chunks[:MAX_EMBEDS_PER_REPORT]):
        head = title if i == 0 else f"{title} (cont.)"
        body = f"{intro}\n\n{chunk}" if (i == 0 and intro) else chunk
        out.append(make(head, body))
    if len(chunks) > MAX_EMBEDS_PER_REPORT:
        shown = sum(c.count("\n") + 1 for c in chunks[:MAX_EMBEDS_PER_REPORT])
        out[-1].description += f"\n\n*…and {len(lines) - shown} more not shown.*"
    return out


def improvement_embeds(members: list[dict]) -> list[discord.Embed]:
    rows = []
    for n in members:
        gaps = checks.improvement_gaps(n)
        if gaps:
            rows.append((sum(gaps.values()), n, gaps))
    rows.sort(key=lambda r: -r[0])
    lines = [f"{checks.nation_link(n)} — {checks.format_gaps(g)}" for _, n, g in rows]
    return _paged(
        f"Missing Military Improvements ({len(rows)})",
        "Target per city: 5 Barracks, 5 Factories, 5 Hangars, 3 Drydocks.",
        lines,
        "Military Improvements Complete",
        "Every audited member has the full military improvements in all cities.",
    )


def readiness_embeds(members: list[dict], threshold: int = 100) -> list[discord.Embed]:
    rows = checks.below_military(members, threshold)
    lines = []
    for n, st in rows:
        u = st["per_unit"]
        detail = " ".join(f"{checks.UNIT_SHORT[k]} {checks.percent(v)}%" for k, v in u.items())
        lines.append(f"{checks.nation_link(n)} — **{checks.percent(st['overall'])}%** · {detail}")
    goal = "100%" if threshold >= 100 else f"{threshold}%"
    return _paged(
        f"Below {goal} Military ({len(rows)})",
        "S soldiers · T tanks · A aircraft · N ships, as a share of the maximum for the city count.",
        lines,
        "Military Fully Ready",
        f"Every audited member is at {goal} or higher.",
    )


def activity_embeds(members: list[dict], now: datetime) -> list[discord.Embed]:
    report = checks.activity_report(members, now)
    c = report["counts"]
    summary = (
        f"🟢 Active in last 24h: **{c['day']}**\n"
        f"🟡 1–{checks.WATCH_DAYS} days: **{c['three']}**\n"
        f"🟠 {checks.WATCH_DAYS}–{checks.INACTIVE_DAYS} days: **{c['week']}**\n"
        f"🔴 {checks.INACTIVE_DAYS}+ days: **{c['inactive']}**"
    )
    if c["unknown"]:
        summary += f"\n⚪ Unknown: **{c['unknown']}**"
    lines = [f"{checks.nation_link(n)} — {int(d)}d inactive" for n, d in report["watch"]]
    out = _paged(
        f"Member Activity ({len(members)} audited)",
        summary,
        lines,
        "Member Activity",
        summary,
        make=embeds.info,
    )
    return out


def summary_embed(alliance_name: str, active: list[dict], vacation: list[dict], now: datetime) -> discord.Embed:
    missing = sum(1 for n in active if checks.improvement_gaps(n))
    below = len(checks.below_military(active))
    inactive = checks.activity_report(active, now)["counts"]["inactive"]
    e = embeds.info(f"Daily Audit — {alliance_name}", f"{len(active)} members audited.")
    e.add_field(name="Missing improvements", value=str(missing), inline=True)
    e.add_field(name="Below 100% military", value=str(below), inline=True)
    e.add_field(name=f"Inactive {checks.INACTIVE_DAYS}d+", value=str(inactive), inline=True)
    if vacation:
        e.add_field(name="Skipped (vacation mode)", value=str(len(vacation)), inline=False)
    return e


# ------------------------------------------------------------------- cog

class Audit(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.daily_loop.start()

    async def cog_unload(self) -> None:
        self.daily_loop.cancel()

    audit_group = app_commands.Group(
        name="audit", description="Alliance audit setup and automation", guild_only=True
    )
    roster_group = app_commands.Group(
        name="roster", description="Alliance roster compliance reports", guild_only=True
    )

    # ---- helpers

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        ma_role = await database.get_guild_role(interaction.guild_id, "MA")
        return bool(ma_role and any(r.id == ma_role for r in interaction.user.roles))

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=embeds.error(
                "Not Allowed",
                "Only administrators and the Military Affairs (MA) role can use this.",
            ),
            ephemeral=True,
        )

    async def _alliance_for(self, guild_id: int) -> int | None:
        cfg = await audit_db.get_config(guild_id)
        if cfg:
            return int(cfg["alliance_id"])
        alerts = await database.get_alerts_config(guild_id)
        if alerts and alerts.get("alliance_id"):
            return int(alerts["alliance_id"])
        return None

    async def _load(self, alliance_id: int):
        name, nations = await audit_data.fetch_alliance(self.bot.pw_client, alliance_id)
        active, vacation = checks.split_members(nations)
        return name, active, vacation

    async def _full_audit(self, alliance_id: int) -> list[discord.Embed]:
        name, active, vacation = await self._load(alliance_id)
        now = datetime.now(timezone.utc)
        out = [summary_embed(name, active, vacation, now)]
        out += improvement_embeds(active)
        out += readiness_embeds(active)
        out += activity_embeds(active, now)
        return out

    async def _roster_reply(self, interaction: discord.Interaction, build) -> None:
        """Shared flow for the /roster commands: permission, defer, load, reply."""
        if not await self._allowed(interaction):
            return await self._deny(interaction)
        await interaction.response.defer()
        try:
            alliance_id = await self._alliance_for(interaction.guild_id)
            if not alliance_id:
                return await interaction.followup.send(embed=embeds.error(
                    "No Alliance Set", "Run `/audit configure` first."))
            _, active, _ = await self._load(alliance_id)
            for e in build(active):
                await interaction.followup.send(embed=e)
        except (PWApiError, RuntimeError) as exc:
            await interaction.followup.send(embed=embeds.error("Audit Failed", str(exc)[:500]))

    # ---- /audit

    @audit_group.command(name="configure", description="Choose the alliance to audit and the results channel")
    @app_commands.describe(alliance_id="P&W alliance ID to audit", channel="Channel where audit results are posted")
    async def configure(self, interaction: discord.Interaction, alliance_id: int, channel: discord.TextChannel):
        if not await self._allowed(interaction):
            return await self._deny(interaction)
        await interaction.response.defer()
        try:
            alliance = await self.bot.pw_client.get_alliance_by_id_or_name(str(alliance_id))
            if not alliance:
                return await interaction.followup.send(embed=embeds.error(
                    "Alliance Not Found", f"No alliance with ID `{alliance_id}`."))
            await audit_db.save_config(interaction.guild_id, alliance_id, channel.id)
        except (PWApiError, RuntimeError) as exc:
            return await interaction.followup.send(embed=embeds.error("Setup Failed", str(exc)[:500]))
        await interaction.followup.send(embed=embeds.success(
            "Audit Configured",
            f"Auditing **{alliance['name']}** (`{alliance_id}`).\nResults will post in {channel.mention}.\n"
            "Use `/audit daily` to automate it, or `/audit run` to run it now.",
        ))

    @audit_group.command(name="daily", description="Turn the automatic daily audit on or off")
    @app_commands.choices(state=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def daily(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        if not await self._allowed(interaction):
            return await self._deny(interaction)
        try:
            cfg = await audit_db.get_config(interaction.guild_id)
            if not cfg:
                return await interaction.response.send_message(embed=embeds.error(
                    "Not Configured", "Run `/audit configure` first."), ephemeral=True)
            enabled = state.value == "on"
            await audit_db.set_daily(interaction.guild_id, enabled)
        except RuntimeError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Update Failed", str(exc)[:500]), ephemeral=True)
        if enabled:
            text = (f"The audit will post in <#{cfg['channel_id']}> every day, "
                    f"{DAILY_RUN_HOUR_UTC} hour after the game day change ({DAILY_RUN_HOUR_UTC:02d}:00 UTC).")
            await interaction.response.send_message(embed=embeds.success("Daily Audit Enabled", text))
        else:
            await interaction.response.send_message(embed=embeds.success(
                "Daily Audit Disabled", "The automatic audit is off. `/audit run` still works."))

    @audit_group.command(name="run", description="Run the full audit now and post it to the audit channel")
    async def run(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return await self._deny(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            cfg = await audit_db.get_config(interaction.guild_id)
            if not cfg:
                return await interaction.followup.send(embed=embeds.error(
                    "Not Configured", "Run `/audit configure` first."))
            channel = self.bot.get_channel(int(cfg["channel_id"]))
            if channel is None:
                return await interaction.followup.send(embed=embeds.error(
                    "Channel Missing", "The audit channel no longer exists. Run `/audit configure` again."))
            for e in await self._full_audit(int(cfg["alliance_id"])):
                await channel.send(embed=e)
        except discord.Forbidden:
            return await interaction.followup.send(embed=embeds.error(
                "No Permission", "I can't send messages in the audit channel."))
        except (PWApiError, RuntimeError) as exc:
            return await interaction.followup.send(embed=embeds.error("Audit Failed", str(exc)[:500]))
        await interaction.followup.send(embed=embeds.success(
            "Audit Posted", f"Results are in <#{cfg['channel_id']}>."))

    # ---- /roster

    @roster_group.command(name="improvements", description="Members missing military improvements")
    async def improvements(self, interaction: discord.Interaction):
        await self._roster_reply(interaction, improvement_embeds)

    @roster_group.command(name="readiness", description="Members below full military")
    @app_commands.describe(below="Only show members under this percent (default 100)")
    async def readiness(self, interaction: discord.Interaction, below: app_commands.Range[int, 1, 100] = 100):
        await self._roster_reply(interaction, lambda members: readiness_embeds(members, below))

    @roster_group.command(name="activity", description="How active the alliance members are")
    async def activity(self, interaction: discord.Interaction):
        await self._roster_reply(
            interaction, lambda members: activity_embeds(members, datetime.now(timezone.utc)))

    # ---- daily loop

    @tasks.loop(minutes=5)
    async def daily_loop(self):
        now = datetime.now(timezone.utc)
        if now.hour != DAILY_RUN_HOUR_UTC:
            return
        today = now.date().isoformat()
        try:
            configs = await audit_db.list_daily_enabled()
        except Exception:
            log.exception("audit: could not load daily configs")
            return
        for cfg in configs:
            if str(cfg.get("last_run_date") or "") == today:
                continue
            guild_id = int(cfg["guild_id"])
            try:
                # Mark first so a slow run can never post twice.
                await audit_db.mark_run(guild_id, today)
                channel = self.bot.get_channel(int(cfg["channel_id"]))
                if channel is None:
                    log.warning("audit: channel %s missing for guild %s", cfg["channel_id"], guild_id)
                    continue
                for e in await self._full_audit(int(cfg["alliance_id"])):
                    await channel.send(embed=e)
                log.info("audit: daily audit posted for guild %s", guild_id)
            except Exception as exc:
                log.exception("audit: daily audit failed for guild %s", guild_id)
                try:
                    channel = self.bot.get_channel(int(cfg["channel_id"]))
                    if channel:
                        await channel.send(embed=embeds.error(
                            "Daily Audit Failed", f"{str(exc)[:300]}\nUse `/audit run` to retry."))
                except Exception:
                    pass

    @daily_loop.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Audit(bot))
