"""Evergreen AI (Gemini) — knowledge channel + @mention / reply-to-bot."""

import os
import logging
import re

import aiohttp
import discord
from discord.ext import commands

from utils import database
from utils.spy_parse import parse_spy_message, format_money, value_resources

log = logging.getLogger("evergreen-ai")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

KNOWLEDGE_CHANNEL_ID = int(os.environ.get("AI_KNOWLEDGE_CHANNEL_ID", "0") or 0)
SPY_CHANNEL_ID = int(os.environ.get("AI_SPY_CHANNEL_ID", "0") or 0)

AI_PROVIDER = os.environ.get("AI_PROVIDER", "groq").lower()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
MAX_KNOWLEDGE_CHARS = 12_000
MAX_REPLY_CHARS = 1900


def _extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>\]]+", text or "")


async def _fetch_url_text(session: aiohttp.ClientSession, url: str) -> str:
    try:
        headers = {"User-Agent": "EvergreenBot/1.0 (knowledge indexer)"}
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12), headers=headers) as resp:
            if resp.status != 200:
                return f"[Could not fetch {url}: HTTP {resp.status}]"
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = await resp.text(errors="ignore")
            if "html" in ctype or raw.lstrip().startswith("<"):
                text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
                text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
                text = re.sub(r"(?s)<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
            else:
                text = raw.strip()
            return text[:8000] if text else f"[Empty page: {url}]"
    except Exception as e:
        return f"[Fetch failed for {url}: {e}]"


async def llm_generate(session, system: str, user: str) -> str:
    """Call Groq (default) or Gemini based on AI_PROVIDER."""
    import asyncio
    import json

    provider = os.environ.get("AI_PROVIDER", "groq").lower()

    if provider == "groq":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            return "AI is not configured (missing GROQ_API_KEY on Render)."
        model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.55,
            "max_tokens": 1024,
        }
        for attempt in range(2):
            try:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as resp:
                    raw = await resp.text()
                    try:
                        data = json.loads(raw)
                    except Exception:
                        log.warning("[ai] groq bad JSON %s: %s", resp.status, raw[:300])
                        if attempt == 0:
                            await asyncio.sleep(1.0)
                            continue
                        return "Brain lag — try again in a second."
                    if resp.status == 429:
                        log.warning("[ai] groq 429: %s", raw[:300])
                        return (
                            "Free AI limit hit for now. "
                            "Try again in a minute (or later if daily quota is used)."
                        )
                    if resp.status != 200:
                        err = (data.get("error") or {}).get("message") or raw[:300]
                        log.warning("[ai] groq error %s: %s", resp.status, err)
                        if attempt == 0:
                            await asyncio.sleep(1.0)
                            continue
                        return "Couldn't reach the brain right now. Try again shortly."
                    choices = data.get("choices") or []
                    if not choices:
                        return "Got nothing useful back. Rephrase and try again."
                    out = ((choices[0].get("message") or {}).get("content") or "").strip()
                    return out or "Empty answer — try asking another way."
            except Exception as e:
                log.warning("[ai] groq exception: %s: %s", type(e).__name__, e)
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                return "Brain lag — try again in a second."
        return "Couldn't reach the brain right now. Try again shortly."

    # Gemini fallback
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "AI is not configured right now. Ask gov to check the bot settings."
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.55, "maxOutputTokens": 1024},
    }
    url = f"{GEMINI_URL}?key={api_key}"
    for attempt in range(2):
        try:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=45)
            ) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except Exception:
                    log.warning("[ai] gemini bad JSON %s: %s", resp.status, raw[:300])
                    if attempt == 0:
                        await asyncio.sleep(1.2)
                        continue
                    return "Brain lag — try again in a second."
                if resp.status == 429:
                    log.warning("[ai] gemini 429: %s", raw[:300])
                    return (
                        "Daily/minute free AI limit hit. "
                        "Try again later (quota resets around midnight Pacific Time)."
                    )
                if resp.status != 200:
                    err = (data.get("error") or {}).get("message") or raw[:300]
                    log.warning("[ai] gemini error %s: %s", resp.status, err)
                    if attempt == 0:
                        await asyncio.sleep(1.2)
                        continue
                    return "Couldn't reach the brain right now. Try again shortly."
                candidates = data.get("candidates") or []
                if not candidates:
                    if attempt == 0:
                        await asyncio.sleep(0.8)
                        continue
                    return "Got nothing useful back. Rephrase and try again."
                parts = (candidates[0].get("content") or {}).get("parts") or []
                out = "".join(p.get("text", "") for p in parts).strip()
                return out or "Empty answer — try asking another way."
        except Exception as e:
            log.warning("[ai] gemini exception: %s: %s", type(e).__name__, e)
            if attempt == 0:
                await asyncio.sleep(1.2)
                continue
            return "Brain lag — try again in a second."
    return "Couldn't reach the brain right now. Try again shortly."


async def _live_pw_context(bot, guild_id: int, question: str) -> str:
    """Pull anything useful the P&W API can see for this question."""
    import re

    q = (question or "").strip()
    ql = q.lower()
    bits = []
    client = getattr(bot, "pw_client", None)
    if not client:
        return ""

    # --- detect IDs in text / links ---
    nation_ids = set()
    for m in re.finditer(r"nation/id[=/](\d+)", ql):
        nation_ids.add(int(m.group(1)))
    for m in re.finditer(r"\bnation\s*(?:id\s*)?#?(\d{2,7})\b", ql):
        nation_ids.add(int(m.group(1)))
    for m in re.finditer(r"\bid[=:\s]+(\d{2,7})\b", ql):
        nation_ids.add(int(m.group(1)))

    alliance_ids = set()
    for m in re.finditer(r"alliance/id[=/](\d+)", ql):
        alliance_ids.add(int(m.group(1)))
    for m in re.finditer(r"\balliance\s*(?:id\s*)?#?(\d{1,6})\b", ql):
        alliance_ids.add(int(m.group(1)))

    wants_market = any(w in ql for w in (
        "market", "price", "prices", "trade price", "avg price", "how much is",
        "steel", "gasoline", "munitions", "aluminum", "uranium", "food price", "credits",
    ))
    wants_members = any(w in ql for w in (
        "how many member", "member count", "members in", "roster", "headcount",
        "alliance size", "members are in",
    ))
    wants_treaty = any(w in ql for w in (
        "treaty", "treaties", "allied with", "allies", "mdp", "protectorate", "nap", "odp",
    ))
    wants_wars = any(w in ql for w in (
        "active war", "at war", "wars against", "who is fighting", "war count",
    ))
    wants_top = any(w in ql for w in ("top alliance", "top aa", "rank alliance", "score ranking"))

    # quoted name: "Nation Name" or 'Nation Name'
    name_candidates = re.findall(r"[\"']([^\"']{2,40})[\"']", q)

    # bare "who is X" / "look up X" / "info on X"
    for m in re.finditer(
        r"(?:who is|who's|lookup|look up|info on|about|nation|alliance)\s+([A-Za-z0-9][A-Za-z0-9 _'\-]{1,40})",
        q,
        flags=re.I,
    ):
        name_candidates.append(m.group(1).strip())

    try:
        config = await database.get_alerts_config(guild_id)
        our_aa = config.get("alliance_id") if config else None

        # --- market ---
        if wants_market:
            try:
                prices = await client.get_trade_prices()
                if prices:
                    bits.append(
                        "Live market averages: "
                        + ", ".join(
                            f"{k}={prices.get(k)}"
                            for k in (
                                "food", "coal", "oil", "uranium", "iron", "bauxite", "lead",
                                "gasoline", "munitions", "steel", "aluminum", "credits",
                            )
                            if prices.get(k) is not None
                        )
                    )
            except Exception as e:
                bits.append(f"(market failed: {e})")

        # --- resolve names to nation or alliance ---
        for name in name_candidates[:5]:
            if len(name) < 2:
                continue
            try:
                nation = await client.get_nation_by_name(name)
                if nation and nation.get("id"):
                    nation_ids.add(int(nation["id"]))
            except Exception:
                pass
            try:
                aa = await client.get_alliance_by_id_or_name(name)
                if aa and aa.get("id"):
                    alliance_ids.add(int(aa["id"]))
            except Exception:
                pass

        # default: "our / evergreen members" → our alliance
        if wants_members and our_aa and not alliance_ids:
            alliance_ids.add(int(our_aa))
        if wants_treaty and our_aa and not alliance_ids:
            alliance_ids.add(int(our_aa))
        if wants_wars and our_aa:
            alliance_ids.add(int(our_aa))

        # --- nations ---
        for nid in list(nation_ids)[:5]:
            try:
                n = await client.get_nation(nid)
                if not n:
                    bits.append(f"Nation id={nid}: not found")
                    continue
                aa = n.get("alliance") or {}
                bits.append(
                    "Live nation: "
                    f"id={n.get('id')} name={n.get('nation_name')} leader={n.get('leader_name')} "
                    f"score={n.get('score')} cities={n.get('num_cities') or n.get('cities')} "
                    f"alliance={aa.get('name')} (id={n.get('alliance_id')}) "
                    f"color={n.get('color')} soldiers={n.get('soldiers')} tanks={n.get('tanks')} "
                    f"aircraft={n.get('aircraft')} ships={n.get('ships')} "
                    f"missiles={n.get('missiles')} nukes={n.get('nukes')} "
                    f"beige_turns={n.get('beige_turns')} vm={n.get('vacation_mode_turns')} "
                    f"last_active={n.get('last_active')}"
                )
            except Exception as e:
                bits.append(f"(nation {nid} failed: {e})")

        # --- alliances ---
        for aid in list(alliance_ids)[:5]:
            try:
                aa = await client.get_alliance_by_id_or_name(str(aid))
                if aa:
                    bits.append(
                        "Live alliance: "
                        f"id={aa.get('id')} name={aa.get('name')} score={aa.get('score')} "
                        f"members={aa.get('num_nations') or aa.get('nation_count') or '?'} "
                        f"rank={aa.get('rank')} color={aa.get('color')}"
                    )
                if wants_members or (aa and not aa.get("num_nations")):
                    members = await client.get_alliance_members(aid)
                    if members is not None:
                        bits.append(f"Live alliance_id={aid} member_count={len(members)}")
                if wants_treaty:
                    treaties = await client.get_alliance_treaties(aid)
                    if treaties:
                        lines_t = [
                            f"- {t.get('treaty_type')}: {t.get('other_alliance_name')} (id {t.get('other_alliance_id')})"
                            for t in treaties
                        ]
                        bits.append(f"Live treaties for aa {aid}:\n" + "\n".join(lines_t))
                    else:
                        bits.append(f"Live treaties for aa {aid}: none")
                if wants_wars and our_aa and int(aid) == int(our_aa):
                    wars = await client.get_active_wars(int(our_aa))
                    bits.append(f"Live active wars involving our AA: {len(wars or [])}")
            except Exception as e:
                bits.append(f"(alliance {aid} failed: {e})")

        # --- top alliances ---
        if wants_top:
            try:
                top = await client.get_top_alliances(10)
                if top:
                    lines_top = [
                        f"{i+1}. {a.get('name')} (id={a.get('id')}) score={a.get('score')}"
                        for i, a in enumerate(top)
                    ]
                    bits.append("Live top alliances:\n" + "\n".join(lines_top))
            except Exception as e:
                bits.append(f"(top alliances failed: {e})")

        # --- our configured blocs (always small, useful) ---
        if any(w in ql for w in ("dnr", "enemies", "allies", "bloc", "coalition")):
            for bloc in ("ALLIES", "ENEMIES", "DNR", "EXTENSION", "DNR_MEMBERS"):
                try:
                    rows = await database.list_coalitions(guild_id, bloc)
                    if rows:
                        ids = [str(r.get("alliance_id")) for r in rows[:40] if r.get("alliance_id") is not None]
                        bits.append(f"Configured {bloc}: {', '.join(ids)}")
                except Exception:
                    pass

    except Exception as e:
        bits.append(f"(Live P&W lookup failed: {e})")

    return "\n\n".join(bits)


class AI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.warning(
            "[ai] AI cog loaded provider=%s groq_model=%s",
            os.environ.get("AI_PROVIDER", "groq"),
            os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"),
        )

    async def _knowledge_context(self, guild_id: int) -> str:
        try:
            rows = await database.list_ai_knowledge(guild_id, limit=40)
        except Exception:
            return "(Knowledge store unavailable.)"
        if not rows:
            return "(No alliance knowledge stored yet.)"
        chunks, total = [], 0
        for r in rows:
            piece = (r.get("content") or "").strip()
            if not piece:
                continue
            src = r.get("source") or "note"
            block = f"[{src}] {piece}\n"
            if total + len(block) > MAX_KNOWLEDGE_CHARS:
                break
            chunks.append(block)
            total += len(block)
        return "\n".join(chunks) if chunks else "(No alliance knowledge stored yet.)"

    async def _answer(self, guild_id: int, question: str, author: str) -> str:
        knowledge = await self._knowledge_context(guild_id)
        live = await _live_pw_context(self.bot, guild_id, question)
        system = (
            "You are Evergreen MILCOM in Discord — talk like a real alliance member, not a helpdesk bot. "
            "Short, casual, direct. Light banter OK. "
            "When Live game data is provided, treat it as ground truth and answer with those numbers. "
            "You can discuss any nation or alliance in Politics & War using live data — not only Evergreen. "
            "If live data is missing, say so briefly; do not invent stats. "
            "Never mention AI, models, or providers. Never reveal secrets or API keys."
        )
        user = f"Alliance knowledge:\n{knowledge}\n\nLive game data:\n{live or '(none requested)'}\n\nMember {author} asks:\n{question}"
        session = self.bot.http_session
        reply = await llm_generate(session, system, user)
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[: MAX_REPLY_CHARS - 20] + "\n…(truncated)"
        return reply

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if KNOWLEDGE_CHANNEL_ID and message.channel.id == KNOWLEDGE_CHANNEL_ID:
            await self._ingest_knowledge(message)
            return

        if SPY_CHANNEL_ID and message.channel.id == SPY_CHANNEL_ID:
            await self._handle_spy_desk(message)
            return

        is_mention = self.bot.user and self.bot.user in message.mentions
        is_reply_to_bot = False
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message) and ref.author.id == self.bot.user.id:
                is_reply_to_bot = True

        if not (is_mention or is_reply_to_bot):
            return

        content = message.content or ""
        if self.bot.user:
            content = (
                content.replace(f"<@{self.bot.user.id}>", "")
                .replace(f"<@!{self.bot.user.id}>", "")
                .strip()
            )
        if not content:
            content = "Help me with this."

        async with message.channel.typing():
            answer = await self._answer(message.guild.id, content, str(message.author))
        await message.reply(answer, mention_author=False)

    async def _ingest_knowledge(self, message: discord.Message):
        session = self.bot.http_session
        parts = []
        if message.content:
            parts.append(message.content.strip())
        urls = _extract_urls(message.content or "")
        for url in urls[:5]:
            parts.append(f"URL {url}:\n{await _fetch_url_text(session, url)}")
        for att in message.attachments[:3]:
            if att.size and att.size < 200_000 and att.filename.endswith((".txt", ".md", ".csv")):
                try:
                    data = await att.read()
                    parts.append(
                        f"File {att.filename}:\n{data.decode('utf-8', errors='ignore')[:6000]}"
                    )
                except Exception:
                    pass
        body = "\n\n".join(p for p in parts if p).strip()
        if not body:
            return
        try:
            await database.add_ai_knowledge(
                guild_id=message.guild.id,
                source=f"discord:{message.author.id}",
                content=body[:15000],
                url=urls[0] if urls else None,
            )
            await message.add_reaction("✅")
        except Exception as e:
            print(f"[ai] knowledge ingest failed: {e}")
            try:
                await message.add_reaction("⚠️")
            except discord.HTTPException:
                pass



    async def _handle_spy_desk(self, message: discord.Message):
        report = parse_spy_message(message.content or "")
        if not report:
            return

        prices = None
        try:
            prices = await self.bot.pw_client.get_trade_prices()
        except Exception as e:
            log.warning("[spy] trade prices failed: %s", e)

        lines = []
        title_target = report.target or "Target"

        if report.kind == "intel":
            lines.append(f"**Spy intel — {title_target}**")
            if report.resources:
                for k in (
                    "money", "food", "coal", "oil", "uranium", "lead", "iron", "bauxite",
                    "gasoline", "munitions", "steel", "aluminum", "credits",
                ):
                    if k in report.resources and report.resources[k]:
                        amt = report.resources[k]
                        if k == "money":
                            lines.append(f"• Money: {format_money(amt)}")
                        else:
                            lines.append(f"• {k.title()}: {amt:,.2f}")
                total = value_resources(report.resources, prices)
                loot_est = total * 0.14
                lines.append(f"
**Worth:** {format_money(total)}")
                lines.append(f"**You would loot:** {format_money(loot_est)}")
                lines.append("_\~14% of stockpile value. Live market averages when available._")
            else:
                lines.append("Could not parse resources from that report.")
        else:
            lines.append(f"**Spy op — {title_target}** ({report.kind})")
            if report.spies_killed is not None:
                lines.append(f"• Enemy spies killed: **{report.spies_killed}**")
            if report.spies_lost is not None:
                lines.append(f"• Your spies lost: **{report.spies_lost}**")

        if report.op_cost_money is not None:
            lines.append(f"• Op cost: {format_money(report.op_cost_money)}")
            if report.kind == "intel" and report.resources:
                total = value_resources(report.resources, prices)
                if report.op_cost_money > 0:
                    lines.append(f"• Value / cost ratio: **{total / report.op_cost_money:.1f}x**")

        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "\n…"
        try:
            await message.reply(body, mention_author=False)
        except discord.HTTPException as e:
            log.warning("[spy] reply failed: %s", e)



async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
