"""Espionage alert for the Evergreen alerts cog.

Detects a spy-count drop on an alliance member, then lists the nations that could have
done it (in the real spy range, not in the victim's alliance or treaty partners), ranked
by how recently they were active. Every search is stored as an incident snapshot.
"""

import logging
from datetime import datetime, timezone

import discord

from utils import database, embeds
from utils.spy_incidents_db import save_spy_incident
from utils.spy_history import (
    MAX_LINES,
    fetch_previous_incidents,
    find_repeat_suspects,
)
from utils.spy_friends import fetch_friendly_alliances
from utils.spy_suspects import (
    chunk_lines,
    find_spy_suspect_report,
    format_suspect_line,
)

log = logging.getLogger("evergreen.spy_alerts")

# One compact list: the most recently active nations that fit in a single embed.
# Everything else is counted in the summary and kept in the incident snapshot.
LIST_MAX_CHARS = 3300


def _summary_text(report) -> str:
    lines = [
        f"**Range:** {report.min_score:,.0f}–{report.max_score:,.0f} score · "
        f"**In range:** {report.in_range_total:,}",
        f"**Active ≤15 min:** {len(report.recent)} · "
        f"**15–60 min:** {len(report.earlier)} · "
        f"**Older:** {report.older_count:,}",
    ]

    excluded = []
    if report.excluded_same_alliance:
        excluded.append(f"{report.excluded_same_alliance} same alliance")
    if report.excluded_treaty:
        excluded.append(f"{report.excluded_treaty} treaty partners & allies")
    if report.no_activity_data:
        excluded.append(f"{report.no_activity_data} no activity data")
    if report.unreadable:
        excluded.append(f"{report.unreadable} unreadable")
    if excluded:
        lines.append("**Excluded:** " + " · ".join(excluded))

    if report.treaty_alliances:
        names = [
            f"{name or 'Unknown'} ({aid})"
            for aid, name in sorted(
                report.treaty_alliances.items(), key=lambda item: str(item[1])
            )
        ]
        shown = ", ".join(names[:10])
        if len(names) > 10:
            shown += f", +{len(names) - 10} more"
        lines.append(f"**Treaty partners & allies excluded:** {shown}")
    elif not report.treaty_lookup_failed:
        lines.append("**Treaty partners excluded:** none found")

    if report.treaty_lookup_failed:
        lines.append("⚠️ Treaty lookup failed, so no treaty partners were excluded.")

    return "\n".join(lines)


def _list_embed(report):
    """Top of the 'active within 15 minutes' list, cut to one embed."""
    if not report.recent:
        return embeds.info(
            "🟢 Active within 15 min before the alert",
            "Nobody in range was active in the 15 minutes before this alert.",
        )

    lines = [format_suspect_line(n) for n in report.recent]
    first_page = next(chunk_lines(lines, LIST_MAX_CHARS))
    shown = first_page.count("\n") + 1
    hidden = len(lines) - shown

    if hidden > 0:
        first_page += f"\n… and {hidden} more active in that window (saved in the incident snapshot)"

    title = f"🟢 Active within 15 min before the alert (top {shown} of {len(lines)})"
    return embeds.info(title, first_page)


def _repeat_lines(items) -> str:
    lines = [format_suspect_line(n) for n in items[:MAX_LINES]]
    if len(items) > MAX_LINES:
        lines.append(f"… and {len(items) - MAX_LINES} more")
    return "\n".join(lines)


def _repeat_embed(victim_name: str, repeat: dict):
    """Nations from this alert that also appeared in the member's earlier reports."""
    n = repeat["compared"]
    if n == 0:
        return embeds.info(
            "🔁 Repeat suspects",
            f"No earlier spy reports for **{victim_name}** to compare with yet.",
        )
    scope = "last report" if n == 1 else f"last {n} reports"
    both = "the earlier report" if n == 1 else "both earlier reports"
    parts = []
    if repeat["all"]:
        parts.append(
            f"**In this alert and {both}** ({len(repeat['all'])}):\n"
            + _repeat_lines(repeat["all"])
        )
    if repeat["some"]:
        some = [nation for nation, _ in repeat["some"]]
        parts.append(
            f"**In this alert and one earlier report** ({len(some)}):\n" + _repeat_lines(some)
        )
    if not parts:
        return embeds.info(
            "🔁 Repeat suspects",
            f"No nation active before this alert also appeared in **{victim_name}**'s {scope}.",
        )
    intro = (
        f"Nations active within 15 min before this alert that also appeared in "
        f"**{victim_name}**'s {scope}. A repeat is a lead, not proof."
    )
    return embeds.info("🔁 Repeat suspects", (intro + "\n\n" + "\n\n".join(parts))[:4000])


async def handle_espionage_check(cog, guild_id, config, member, our_alliance_id):
    """Check one alliance member for a spy loss and post the alert if there was one."""
    channel_id = config.get("espionage_channel_id")
    if not channel_id:
        return

    nation_id = int(member["id"])
    current_spies = int(member.get("spies") or 0)
    last_spies = await database.get_last_spies(guild_id, nation_id)

    if last_spies is None or current_spies >= last_spies:
        await database.set_last_spies(guild_id, nation_id, current_spies)
        return

    channel = await cog._get_channel(channel_id)
    if not channel:
        return

    detected_at = datetime.now(timezone.utc)
    name = member.get("nation_name", "Unknown")

    discord_id = await database.get_discord_id_for_nation(nation_id)
    ping = f"<@{discord_id}> " if discord_id else ""

    report = None
    search_error = None

    try:
        # The server's own ALLIES / EXTENSION lists can name friendly alliances
        # that the in-game treaty lookup does not return.
        try:
            friends = await fetch_friendly_alliances(guild_id)
        except Exception as exc:
            friends = {}
            log.warning("Could not load allies coalition for spy report: %s", exc)
        report = await find_spy_suspect_report(
            cog.bot.pw_client,
            member,
            detected_at,
            extra_excluded=friends,
        )
    except Exception as exc:
        search_error = str(exc) or exc.__class__.__name__
        log.exception("Spy suspect search failed for nation %s", nation_id)

    header = embeds.warning(
        "🕵️ Possible Espionage Loss",
        f"**{name}** lost spies: {last_spies} → {current_spies}.\n"
        f"**Change:** -{last_spies - current_spies} spies.\n"
        "Candidates are nations in spy range, most recently active first. "
        "This does not prove who did it.",
    )

    if report is None:
        header.add_field(
            name="Possible Suspects",
            value=f"Could not complete suspect search: `{search_error[:900]}`",
            inline=False,
        )
    else:
        header.add_field(
            name="Search summary",
            value=_summary_text(report)[:1024],
            inline=False,
        )

    list_embed = _list_embed(report) if report is not None else None

    try:
        await channel.send(
            content=ping or None,
            embeds=[header] + ([list_embed] if list_embed else []),
        )
    except discord.HTTPException:
        log.exception("Combined espionage alert failed for nation %s; trying header only", nation_id)
        try:
            await channel.send(content=ping or None, embeds=[header])
        except discord.HTTPException:
            # Keep the old baseline so the loss is retried next cycle.
            log.exception(
                "Espionage alert for nation %s could not be delivered; retrying next cycle",
                nation_id,
            )
            return

    # The alert is out: move the baseline now so a later failure never re-alerts.
    await database.set_last_spies(guild_id, nation_id, current_spies)

    if report is None:
        return

    # Repeat suspects: separate message, best effort, never blocks the alert.
    # Runs before the snapshot below, so "earlier reports" never include this one.
    try:
        previous = await fetch_previous_incidents(guild_id, nation_id)
        await channel.send(embed=_repeat_embed(name, find_repeat_suspects(report, previous)))
    except Exception as exc:
        log.warning("Repeat-suspect message failed for nation %s: %s", nation_id, exc)

    # Incident snapshot: best effort, never blocks the alert.
    try:
        await save_spy_incident(
            guild_id,
            member,
            report,
            spies_before=last_spies,
            spies_after=current_spies,
        )
    except Exception as exc:
        log.warning("Could not save spy incident snapshot: %s", exc)
