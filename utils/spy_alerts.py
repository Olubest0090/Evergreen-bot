"""Espionage alert for the Evergreen alerts cog.

Detects a spy-count drop on an alliance member, then lists the nations that could have
done it (in the real spy range, not in the victim's alliance or treaty partners), ranked
by how recently they were active. Every search is stored as an incident snapshot.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord

from utils import database, embeds
from utils.spy_incidents_db import save_spy_incident
from utils.spy_suspects import (
    chunk_lines,
    find_spy_suspect_report,
    format_suspect_line,
)

log = logging.getLogger("evergreen.spy_alerts")

# Safety caps. Anything beyond them is counted in the alert and kept in the snapshot.
MAX_RECENT_LISTED = 200
MAX_EARLIER_LISTED = 80
PAGE_PAUSE_SECONDS = 0.6


def _summary_text(report) -> str:
    lines = [
        f"**Attacker score range:** {report.min_score:,.0f} – {report.max_score:,.0f} "
        f"(victim score {report.victim_score:,.0f}; spy ops reach 0.4x–2.5x)",
        f"**Nations in range:** {report.in_range_total:,}",
        f"**Active within 15 min before alert:** {len(report.recent)}",
        f"**Active 15–60 min before alert:** {len(report.earlier)}",
        f"**Active over 60 min before alert (not listed):** {report.older_count:,}",
    ]

    excluded = []
    if report.excluded_same_alliance:
        excluded.append(f"{report.excluded_same_alliance} in the victim's alliance")
    if report.excluded_treaty:
        excluded.append(
            f"{report.excluded_treaty} in treaty partner alliances "
            "(the game blocks ops between treaty partners)"
        )
    if report.no_activity_data:
        excluded.append(f"{report.no_activity_data} with no activity data")
    if report.unreadable:
        excluded.append(f"{report.unreadable} with unreadable data")
    if excluded:
        lines.append("**Excluded:** " + "; ".join(excluded))

    if report.treaty_lookup_failed:
        lines.append(
            "⚠️ The treaty lookup failed, so no treaty partners were excluded."
        )

    if not report.recent:
        lines.append(
            "\nNo nations in range were active in the 15 minutes before this alert. "
            "Check the earlier list below."
        )

    return "\n".join(lines)


async def _send_pages(channel, title: str, suspects: list, cap: int) -> None:
    if not suspects:
        return

    shown = suspects[:cap]
    hidden = len(suspects) - len(shown)
    pages = list(chunk_lines([format_suspect_line(n) for n in shown]))

    for index, text in enumerate(pages, start=1):
        if hidden and index == len(pages):
            text += f"\n… and {hidden} more (kept in the incident snapshot)"

        page_title = title if len(pages) == 1 else f"{title} ({index}/{len(pages)})"

        try:
            await channel.send(embed=embeds.info(page_title, text))
        except discord.HTTPException:
            log.exception("Could not send suspect page %s/%s", index, len(pages))
            return

        await asyncio.sleep(PAGE_PAUSE_SECONDS)


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
        report = await find_spy_suspect_report(
            cog.bot.pw_client,
            member,
            detected_at,
        )
    except Exception as exc:
        search_error = str(exc) or exc.__class__.__name__
        log.exception("Spy suspect search failed for nation %s", nation_id)

    header = embeds.warning(
        "🕵️ Possible Espionage Loss",
        f"**{name}** lost spies: {last_spies} → {current_spies}.\n"
        f"**Change:** -{last_spies - current_spies} spies.\n"
        "Candidates are nations in the spy range of the victim, ranked by how "
        "recently they were active. This does not prove who performed the operation.",
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

    try:
        await channel.send(content=ping or None, embed=header)
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

    await _send_pages(
        channel,
        "🟢 Active within 15 min before the alert",
        report.recent,
        MAX_RECENT_LISTED,
    )
    await _send_pages(
        channel,
        "🟡 Active 15–60 min before the alert",
        report.earlier,
        MAX_EARLIER_LISTED,
    )
