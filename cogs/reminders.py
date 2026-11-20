"""Reminders and AI-triggered channel actions.

The AI cog turns "@bot remind me in 1 hour to check this channel" / "@bot delete this channel"
into an Intent and hands it to Reminders.handle(). Everything that touches Discord lives here, so
every permission check is in one place:

* The requester must hold the matching Discord permission themselves, and so must the bot.
* Deleting a channel or purging messages needs a Confirm button from the same person.
* Scheduled deletes / locks check the requester's permission again when they fire.
* Reminders are kept in memory, loaded once at startup and written to the database only when
  created, fired or cancelled (no polling, no extra egress).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from utils import database, reminders_db
from utils.ai_actions import (
    ERROR_TEXT,
    MAX_PENDING_PER_USER,
    MAX_PURGE,
    Intent,
    ResolvedTZ,
    clean_text,
    compute_due,
    fmt_delta,
    fmt_local,
    resolve_timezone,
)

log = logging.getLogger("evergreen.reminders")

TICK_SECONDS = 10
# A scheduled delete / lock that is overdue by more than this (bot was offline) is skipped, not run late.
DESTRUCTIVE_OVERDUE_LIMIT = timedelta(minutes=10)
LATE_NOTE_AFTER = timedelta(minutes=2)
SCHEDULED_CHANNEL_KINDS = {"delete", "lock", "unlock"}

_PERMS_NEEDED = {
    "delete": ("manage_channels",),
    "rename": ("manage_channels",),
    "set_topic": ("manage_channels",),
    "slowmode": ("manage_channels",),
    "lock": ("manage_channels", "manage_roles"),
    "unlock": ("manage_channels", "manage_roles"),
    "purge": ("manage_messages",),
}
_PERM_LABELS = {
    "manage_channels": "Manage Channels",
    "manage_roles": "Manage Permissions",
    "manage_messages": "Manage Messages",
    "read_message_history": "Read Message History",
}


@dataclass
class Reminder:
    id: int
    guild_id: int
    channel_id: int
    requester_id: int
    kind: str                       # remind | ping | delete | lock | unlock
    targets: list[int] = field(default_factory=list)
    message: str = ""
    due_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _protected_ids() -> set[int]:
    """Channels the AI must never delete: knowledge + spy channels and AI_PROTECTED_CHANNEL_IDS."""
    ids: set[int] = set()
    for key in ("AI_KNOWLEDGE_CHANNEL_ID", "AI_SPY_CHANNEL_ID"):
        try:
            value = int(os.environ.get(key, "0") or 0)
        except ValueError:
            value = 0
        if value:
            ids.add(value)
    for part in (os.environ.get("AI_PROTECTED_CHANNEL_IDS") or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


class ConfirmView(discord.ui.View):
    """Confirm / Cancel buttons that only the person who asked can press."""

    def __init__(self, requester_id: int, on_confirm):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self._on_confirm = on_confirm
        self.message = None
        self._used = False

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who asked can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if self._used:
            return
        self._used = True
        self.stop()
        await self._on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self._used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)

    async def on_timeout(self):
        if self.message is not None and not self._used:
            try:
                await self.message.edit(content="Timed out. Nothing was changed.", view=None)
            except Exception:
                pass


class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending: dict[int, Reminder] = {}
        self._tz: dict[int, ResolvedTZ | None] = {}
        self._loaded = False
        self._warned_db = False
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    # ------------------------------------------------------------------
    # scheduler
    # ------------------------------------------------------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self):
        if not self._loaded:
            await self._load()
        now = datetime.now(timezone.utc)
        for r in [x for x in self._pending.values() if x.due_at <= now]:
            overdue = now - r.due_at
            if r.kind in SCHEDULED_CHANNEL_KINDS and overdue > DESTRUCTIVE_OVERDUE_LIMIT:
                await self._miss(r)
                continue
            await self._fire(r, late=overdue > LATE_NOTE_AFTER)

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    async def _load(self):
        try:
            rows = await reminders_db.list_pending()
        except Exception as exc:
            if not self._warned_db:
                log.warning("Reminders table unavailable (run database/migrations/ai_reminders.sql): %s", exc)
                self._warned_db = True
            return
        for row in rows:
            try:
                due = datetime.fromisoformat(str(row["due_at"]).replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                # setdefault: a reminder created in memory before the first load keeps its own entry
                self._pending.setdefault(int(row["id"]), Reminder(
                    id=int(row["id"]), guild_id=int(row["guild_id"]), channel_id=int(row["channel_id"]),
                    requester_id=int(row["requester_id"]), kind=row["kind"],
                    targets=[int(x) for x in (row.get("target_ids") or [])],
                    message=row.get("message") or "", due_at=due,
                ))
            except Exception:
                log.exception("Skipping unreadable reminder row: %r", row)
        self._loaded = True
        log.info("Loaded %d pending reminder(s)", len(self._pending))

    async def _finish(self, reminder_id: int, status: str):
        try:
            await reminders_db.set_status(reminder_id, status)
        except Exception as exc:
            log.warning("Could not mark reminder %s as %s: %s", reminder_id, status, exc)

    async def _channel_for(self, r: Reminder):
        guild = self.bot.get_guild(r.guild_id)
        channel = guild.get_channel(r.channel_id) if guild else None
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(r.channel_id)
            except Exception:
                channel = None
        return guild, channel

    async def _miss(self, r: Reminder):
        self._pending.pop(r.id, None)
        await self._finish(r.id, "missed")
        _, channel = await self._channel_for(r)
        if channel is not None:
            try:
                await channel.send(
                    f"Skipped the scheduled {r.kind} for <@{r.requester_id}>: I was offline when it was due.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass

    async def _fire(self, r: Reminder, late: bool = False):
        self._pending.pop(r.id, None)
        status = "failed"
        try:
            guild, channel = await self._channel_for(r)
            if r.kind in ("remind", "ping"):
                ok = await self._fire_message(r, channel, late)
            else:
                ok = await self._fire_channel_action(r, guild, channel)
            status = "done" if ok else "failed"
        except Exception:
            log.exception("Reminder %s failed", r.id)
        await self._finish(r.id, status)

    async def _fire_message(self, r: Reminder, channel, late: bool) -> bool:
        late_note = " (delayed, I was offline)" if late else ""
        text = r.message or "Reminder!"
        if r.kind == "remind":
            content = f"<@{r.requester_id}> ⏰ Reminder{late_note}: {text}"
            notify = [r.requester_id]
        else:
            tags = " ".join(f"<@{t}>" for t in r.targets)
            content = f"{tags} ⏰ Ping from <@{r.requester_id}>{late_note}: {text}"
            notify = list(r.targets)
        allowed = discord.AllowedMentions(
            users=[discord.Object(id=i) for i in notify], roles=False, everyone=False, replied_user=False
        )
        if channel is not None:
            try:
                await channel.send(content, allowed_mentions=allowed)
                return True
            except Exception as exc:
                log.warning("Could not post reminder %s in channel %s: %s", r.id, r.channel_id, exc)
        # channel gone or not writable: tell the requester privately
        try:
            user = self.bot.get_user(r.requester_id) or await self.bot.fetch_user(r.requester_id)
            await user.send(f"Your reminder could not be posted in its channel: {text}")
        except Exception:
            return False
        return False

    async def _fire_channel_action(self, r: Reminder, guild, channel) -> bool:
        if channel is None or guild is None:
            return False
        try:
            member = guild.get_member(r.requester_id) or await guild.fetch_member(r.requester_id)
        except Exception:
            member = None
        if member is None:
            await self._say(channel, f"Skipped the scheduled {r.kind}: <@{r.requester_id}> is no longer in the server.")
            return False
        missing = self._missing_perm(member, channel, r.kind, guild.me)
        if missing:
            await self._say(channel, f"Skipped the scheduled {r.kind} for <@{r.requester_id}>: {self._perm_text(*missing)}")
            return False
        reason = f"AI action (scheduled) requested by {member} ({member.id})"
        if r.kind == "delete":
            blocked = await self._protected_reason(channel)
            if blocked:
                await self._say(channel, f"Skipped the scheduled delete: {blocked}")
                return False
            log.warning("AI action: scheduled delete of #%s (%s) requested by %s", channel.name, channel.id, member.id)
            await channel.delete(reason=reason)
        else:
            await self._set_lock(channel, r.kind == "lock", reason)
            await self._say(channel, "🔒 Channel locked." if r.kind == "lock" else "🔓 Channel unlocked.")
        return True

    async def _say(self, channel, text: str):
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # timezones
    # ------------------------------------------------------------------

    async def get_timezone(self, user_id: int) -> ResolvedTZ | None:
        if user_id in self._tz:
            return self._tz[user_id]
        try:
            name = await reminders_db.get_timezone(user_id)
        except Exception as exc:
            log.warning("Could not read timezone for %s: %s", user_id, exc)
            return None  # not cached, so it is retried next time
        resolved = resolve_timezone(name) if name else None
        self._tz[user_id] = resolved
        return resolved

    async def planner_context(self, user_id: int) -> str:
        """Current time and the speaker's timezone, for the AI planner prompt."""
        now = datetime.now(timezone.utc)
        tz = await self.get_timezone(user_id)
        if tz:
            local = now.astimezone(tz.tz)
            return (f"Current time: {now:%Y-%m-%d %H:%M} UTC. The speaker's timezone is {tz.name}; "
                    f"their local time is {local:%Y-%m-%d %H:%M} ({local:%A}).\n")
        return (f"Current time: {now:%Y-%m-%d %H:%M} UTC. The speaker's timezone is unknown "
                "(clock times need it; relative times are fine).\n")

    async def _when(self, user_id: int, due: datetime) -> str:
        tz = await self.get_timezone(user_id)
        now = datetime.now(timezone.utc)
        return f"{fmt_local(due, tz.tz if tz else None, tz.name if tz else None)} ({fmt_delta(due - now)})"

    # ------------------------------------------------------------------
    # permissions
    # ------------------------------------------------------------------

    @staticmethod
    def _missing_perm(member, channel, action: str, me):
        """(who, permission) for the first missing permission, or None if all is fine."""
        needed = _PERMS_NEEDED.get(action, ())
        theirs = channel.permissions_for(member)
        for perm in needed:
            if not getattr(theirs, perm, False):
                return ("you", perm)
        bots = channel.permissions_for(me)
        for perm in needed + (("read_message_history",) if action == "purge" else ()):
            if not getattr(bots, perm, False):
                return ("bot", perm)
        return None

    @staticmethod
    def _perm_text(who: str, perm: str) -> str:
        label = _PERM_LABELS.get(perm, perm)
        if who == "you":
            return f"You need the {label} permission for that."
        return f"I'm missing the {label} permission in this channel."

    async def _protected_reason(self, channel) -> str | None:
        if channel.id in _protected_ids():
            return "This channel is protected from deletion."
        try:
            room = await database.get_war_room_by_channel(channel.id)
        except Exception:
            return "I couldn't verify this isn't a war room, so I won't delete it."
        if room:
            return "This is a war room. Close it with the war-room command so its records are cleaned up."
        return None

    @staticmethod
    async def _set_lock(channel, locked: bool, reason: str):
        await channel.set_permissions(
            channel.guild.default_role, send_messages=False if locked else None, reason=reason
        )

    # ------------------------------------------------------------------
    # entry point used by the AI cog
    # ------------------------------------------------------------------

    async def handle(self, message, intent: Intent):
        """Run one intent for the person who pinged the bot. Returns (reply_text, view_or_None)."""
        try:
            if intent.kind == "set_timezone":
                return await self._h_timezone(message, intent), None
            if intent.kind == "list_reminders":
                return await self._h_list(message), None
            if intent.kind == "cancel_reminder":
                return await self._h_cancel(message, intent), None
            if intent.kind == "remind":
                return await self._h_remind(message, intent), None
            if intent.kind == "channel_action":
                return await self._h_channel(message, intent)
        except Exception:
            log.exception("Action %s failed", intent.kind)
            return "Something went wrong running that. Nothing was changed.", None
        return "I don't know how to do that yet.", None

    async def _h_timezone(self, message, intent: Intent) -> str:
        resolved = resolve_timezone(intent.tz)
        if resolved is None:
            return "I couldn't understand that timezone. Try something like Africa/Lagos, WAT or UTC+1."
        try:
            await reminders_db.set_timezone(message.author.id, resolved.name)
        except Exception as exc:
            log.warning("Could not save timezone: %s", exc)
            return "I couldn't save your timezone (the reminders database table is missing). Ask an admin to run the reminders SQL migration."
        self._tz[message.author.id] = resolved
        local = datetime.now(timezone.utc).astimezone(resolved.tz)
        return f"Done. Your timezone is {resolved.name}; it's {local:%H:%M} for you now. Clock times in your reminders use it."

    async def _h_remind(self, message, intent: Intent) -> str:
        requester = message.author
        if getattr(message, "mention_everyone", False) or getattr(message, "role_mentions", None):
            return "I only ping people you @mention, not roles or everyone."
        mentioned = {m.id: m for m in getattr(message, "mentions", []) if not getattr(m, "bot", False)}
        wanted = [t for t in intent.targets if t != requester.id]
        if any(t not in mentioned for t in wanted):
            return "I can only ping people you @mention in the message."
        targets = wanted
        kind = "ping" if targets else "remind"

        tz = await self.get_timezone(requester.id)
        res = compute_due(datetime.now(timezone.utc), delay_minutes=intent.delay_minutes,
                          at_local=intent.at_local, tz=tz.tz if tz else None)
        if res.error:
            return ERROR_TEXT[res.error]
        if sum(1 for r in self._pending.values() if r.requester_id == requester.id) >= MAX_PENDING_PER_USER:
            return f"You already have {MAX_PENDING_PER_USER} pending reminders. Cancel one first."

        text = clean_text(intent.text)
        try:
            rid = await reminders_db.insert_reminder(
                guild_id=message.guild.id, channel_id=message.channel.id, requester_id=requester.id,
                kind=kind, target_ids=targets, message=text, due_at=res.due,
            )
        except Exception as exc:
            log.warning("Could not save reminder: %s", exc)
            return "I can't save reminders right now (the reminders database table is missing). Ask an admin to run the reminders SQL migration."
        self._pending[rid] = Reminder(rid, message.guild.id, message.channel.id, requester.id, kind, targets, text, res.due)

        when = await self._when(requester.id, res.due)
        body = f": {text}" if text else ""
        if kind == "ping":
            names = ", ".join(mentioned[t].display_name for t in targets)
            reply = f"⏰ Got it. I'll ping {names} {when}{body} (id {rid})."
        else:
            reply = f"⏰ Got it. I'll remind you {when}{body} (id {rid})."
        if tz is None:
            reply += "\nTip: tell me your timezone (e.g. 'my timezone is Africa/Lagos') so clock times like 'at 3pm' work."
        return reply

    async def _h_list(self, message) -> str:
        mine = sorted((r for r in self._pending.values() if r.requester_id == message.author.id), key=lambda r: r.due_at)
        if not mine:
            return "You have no pending reminders."
        lines = []
        for r in mine[:10]:
            what = f"{r.kind} <#{r.channel_id}>" if r.kind in SCHEDULED_CHANNEL_KINDS else (r.message or "reminder")
            lines.append(f"#{r.id} · {await self._when(message.author.id, r.due_at)} · {what}")
        return "Your pending reminders:\n" + "\n".join(lines)

    async def _h_cancel(self, message, intent: Intent) -> str:
        uid = message.author.id
        mine = sorted((r for r in self._pending.values() if r.requester_id == uid), key=lambda r: r.due_at)
        if intent.cancel_all:
            chosen = mine
        elif intent.reminder_id is not None:
            chosen = [r for r in mine if r.id == intent.reminder_id]
        else:
            chosen = mine[-1:]  # the most recent one
        if not chosen:
            return "I couldn't find a pending reminder of yours to cancel."
        for r in chosen:
            self._pending.pop(r.id, None)
            await self._finish(r.id, "cancelled")
        return "Cancelled " + (f"reminder #{chosen[0].id}." if len(chosen) == 1 else f"{len(chosen)} reminders.")

    async def _h_channel(self, message, intent: Intent):
        channel, member, guild = message.channel, message.author, message.guild
        action = intent.action
        if not isinstance(channel, discord.TextChannel):
            return "I can only do that in regular text channels for now.", None
        missing = self._missing_perm(member, channel, action, guild.me)
        if missing:
            return self._perm_text(*missing), None
        scheduled = bool(intent.delay_minutes) or bool(intent.at_local)
        reason = f"AI action requested by {member} ({member.id})"

        if scheduled:
            if action not in SCHEDULED_CHANNEL_KINDS:
                return "I can only schedule delete, lock and unlock.", None
            return await self._schedule_channel_action(message, intent)

        if action == "rename":
            name = intent.value.strip()
            if not name or len(name) > 100:
                return "Tell me the new name (up to 100 characters).", None
            await channel.edit(name=name, reason=reason)
            return f"Renamed this channel to {channel.mention}.", None
        if action == "set_topic":
            topic = intent.value.strip()
            if len(topic) > 1024:
                return "A topic can be at most 1024 characters.", None
            await channel.edit(topic=topic or None, reason=reason)
            return "Topic updated." if topic else "Topic cleared.", None
        if action == "slowmode":
            try:
                seconds = int(float(intent.value))
            except ValueError:
                return "Tell me the slowmode in seconds (0 turns it off, max 21600).", None
            if not 0 <= seconds <= 21600:
                return "Slowmode must be between 0 and 21600 seconds.", None
            await channel.edit(slowmode_delay=seconds, reason=reason)
            return "Slowmode off." if seconds == 0 else f"Slowmode set to {seconds}s.", None
        if action in ("lock", "unlock"):
            await self._set_lock(channel, action == "lock", reason)
            return ("🔒 Channel locked. Everyone can read but not send." if action == "lock"
                    else "🔓 Channel unlocked."), None
        if action == "purge":
            return await self._confirm_purge(message, intent)
        if action == "delete":
            return await self._confirm_delete(message)
        return "I can't do that yet.", None

    async def _confirm_delete(self, message):
        channel, guild = message.channel, message.guild
        blocked = await self._protected_reason(channel)
        if blocked:
            return blocked, None

        async def on_confirm(interaction):
            member = interaction.user
            missing = self._missing_perm(member, channel, "delete", guild.me)
            if missing:
                await interaction.response.edit_message(content=self._perm_text(*missing), view=None)
                return
            blocked_now = await self._protected_reason(channel)
            if blocked_now:
                await interaction.response.edit_message(content=blocked_now, view=None)
                return
            await interaction.response.edit_message(content="Deleting this channel…", view=None)
            log.warning("AI action: %s (%s) deleted #%s (%s)", member, member.id, channel.name, channel.id)
            try:
                await channel.delete(reason=f"AI action requested by {member} ({member.id})")
            except Exception as exc:
                log.warning("Channel delete failed: %s", exc)
                try:
                    await interaction.followup.send("I couldn't delete the channel. Nothing was changed.", ephemeral=True)
                except Exception:
                    pass

        view = ConfirmView(message.author.id, on_confirm)
        return f"⚠️ Delete {channel.mention} permanently? This can't be undone.", view

    async def _confirm_purge(self, message, intent: Intent):
        channel, guild = message.channel, message.guild
        try:
            count = int(float(intent.value))
        except ValueError:
            return f"Tell me how many messages to delete (1 to {MAX_PURGE}).", None
        if not 1 <= count <= MAX_PURGE:
            return f"I can delete between 1 and {MAX_PURGE} messages at a time.", None

        async def on_confirm(interaction):
            member = interaction.user
            missing = self._missing_perm(member, channel, "purge", guild.me)
            if missing:
                await interaction.response.edit_message(content=self._perm_text(*missing), view=None)
                return
            await interaction.response.edit_message(content=f"Deleting {count} messages…", view=None)
            log.warning("AI action: %s (%s) purged %d messages in #%s (%s)", member, member.id, count, channel.name, channel.id)
            try:
                deleted = await channel.purge(limit=count, before=interaction.message)
                await interaction.edit_original_response(content=f"Deleted {len(deleted)} messages.")
                await asyncio.sleep(5)
                await interaction.delete_original_response()
            except Exception as exc:
                log.warning("Purge failed: %s", exc)

        view = ConfirmView(message.author.id, on_confirm)
        return f"⚠️ Delete the last {count} messages in {channel.mention}? This can't be undone.", view

    async def _schedule_channel_action(self, message, intent: Intent):
        channel, guild, member = message.channel, message.guild, message.author
        action = intent.action
        tz = await self.get_timezone(member.id)
        res = compute_due(datetime.now(timezone.utc), delay_minutes=intent.delay_minutes,
                          at_local=intent.at_local, tz=tz.tz if tz else None)
        if res.error:
            return ERROR_TEXT[res.error], None
        if sum(1 for r in self._pending.values() if r.requester_id == member.id) >= MAX_PENDING_PER_USER:
            return f"You already have {MAX_PENDING_PER_USER} pending reminders. Cancel one first.", None
        if action == "delete":
            blocked = await self._protected_reason(channel)
            if blocked:
                return blocked, None

        async def schedule(due: datetime) -> str:
            try:
                rid = await reminders_db.insert_reminder(
                    guild_id=guild.id, channel_id=channel.id, requester_id=member.id,
                    kind=action, target_ids=[], message="", due_at=due,
                )
            except Exception as exc:
                log.warning("Could not save scheduled %s: %s", action, exc)
                return "I can't save scheduled actions right now (the reminders database table is missing). Ask an admin to run the reminders SQL migration."
            self._pending[rid] = Reminder(rid, guild.id, channel.id, member.id, action, [], "", due)
            when = await self._when(member.id, due)
            verb = {"delete": "delete", "lock": "lock", "unlock": "unlock"}[action]
            return (f"⏰ Got it. I'll {verb} {channel.mention} {when} (id {rid}). "
                    f"Say 'cancel reminder {rid}' to stop it.")

        if action != "delete":
            return await schedule(res.due), None

        async def on_confirm(interaction):
            if res.due <= datetime.now(timezone.utc):
                await interaction.response.edit_message(content="That time passed while waiting. Ask again.", view=None)
                return
            text = await schedule(res.due)
            await interaction.response.edit_message(content=text, view=None)

        when = await self._when(member.id, res.due)
        view = ConfirmView(member.id, on_confirm)
        return f"⚠️ Delete {channel.mention} permanently {when}? This can't be undone.", view


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
