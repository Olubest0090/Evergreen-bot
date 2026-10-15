"""Evergreen AI (Gemini) — knowledge channel + @mention / reply-to-bot."""

import os
import logging
import re

import aiohttp
import discord
from discord.ext import commands

from utils import database
from utils.battle_sim import (
    Force, ControlState, advice_for_war, infer_controls_from_attacks,
    simulate_air, simulate_ground, simulate_naval,
)
from utils.spy_parse import parse_spy_message, format_money, value_resources, unit_production_value, SPY_RECRUIT_COST

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
MAX_REPLY_CHARS = 600


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


async def _war_room_tactical_context(bot, channel_id: int, author_id: int) -> str:
    """Live war-room brief: enemy, linked nation, MAP, controls, specific attack tips."""
    try:
        room = await database.get_war_room_by_channel(channel_id)
    except Exception:
        room = None
    if not room or not room.get("enemy_nation_id"):
        return ""

    client = getattr(bot, "pw_client", None)
    if not client:
        return ""

    lines = []
    try:
        enemy = await client.get_nation(int(room["enemy_nation_id"]))
        if not enemy:
            return "War room enemy nation not found."
        lines.append(
            f"WAR ROOM TARGET: {enemy.get('nation_name')} id={enemy.get('id')} "
            f"AA={((enemy.get('alliance') or {}).get('name'))} "
            f"S={enemy.get('soldiers')} T={enemy.get('tanks')} "
            f"planes={enemy.get('aircraft')} ships={enemy.get('ships')} "
            f"missiles={enemy.get('missiles')} nukes={enemy.get('nukes')}"
        )

        our_id = await database.get_nation_for_user(author_id)
        our = await client.get_nation(int(our_id)) if our_id else None
        if our:
            lines.append(
                f"SPEAKER NATION (linked): {our.get('nation_name')} id={our.get('id')} "
                f"S={our.get('soldiers')} T={our.get('tanks')} "
                f"planes={our.get('aircraft')} ships={our.get('ships')} MAP context below"
            )
        else:
            lines.append("SPEAKER has no linked nation — give general stack advice only.")

        war = None
        if our:
            att_id, def_id = int(our["id"]), int(enemy["id"])
            for w in (our.get("offensive_wars") or []) + (our.get("defensive_wars") or []):
                if (w.get("turns_left") or 0) <= 0:
                    continue
                if {int(w.get("att_id") or 0), int(w.get("def_id") or 0)} == {att_id, def_id}:
                    war = await client.get_war(int(w["id"]))
                    break
        if war:
            att_id = int(our["id"])
            if int(war.get("att_id") or 0) == att_id:
                our_map = int(war.get("att_points") or 0)
                enemy_map = int(war.get("def_points") or 0)
                our_res = int(war.get("att_resistance") or 100)
                enemy_res = int(war.get("def_resistance") or 100)
            else:
                our_map = int(war.get("def_points") or 0)
                enemy_map = int(war.get("att_points") or 0)
                our_res = int(war.get("def_resistance") or 100)
                enemy_res = int(war.get("att_resistance") or 100)
            lines.append(
                f"THIS WAR id={war.get('id')} type={war.get('war_type')} "
                f"MAP you={our_map}/12 enemy={enemy_map}/12 "
                f"Res you={our_res} enemy={enemy_res}"
            )
            try:
                attacks = await client.get_war_attacks(int(war["id"]))
                controls = infer_controls_from_attacks(attacks or [], att_id)
            except Exception:
                controls = ControlState()
            ctrl = []
            if controls.air_sup_att:
                ctrl.append("YOU air superiority")
            if controls.air_sup_def:
                ctrl.append("ENEMY air superiority")
            if controls.ground_ctrl_att:
                ctrl.append("YOU ground control")
            if controls.ground_ctrl_def:
                ctrl.append("ENEMY ground control")
            if controls.blockade_att:
                ctrl.append("YOU blockade")
            if controls.blockade_def:
                ctrl.append("ENEMY blockades you")
            lines.append("Controls: " + (", ".join(ctrl) if ctrl else "none"))

            our_f = Force(
                soldiers=int(our.get("soldiers") or 0),
                tanks=int(our.get("tanks") or 0),
                aircraft=int(our.get("aircraft") or 0),
                ships=int(our.get("ships") or 0),
            )
            en_f = Force(
                soldiers=int(enemy.get("soldiers") or 0),
                tanks=int(enemy.get("tanks") or 0),
                aircraft=int(enemy.get("aircraft") or 0),
                ships=int(enemy.get("ships") or 0),
            )
            air = simulate_air(our_f, en_f)
            gnd = simulate_ground(
                our_f, en_f,
                att_has_air_sup=controls.air_sup_att,
                def_has_air_sup=controls.air_sup_def,
            )
            nav = simulate_naval(our_f, en_f)
            lines.append(
                f"Odds: Air \~{air['win_pct']:.0f}% win/\~{air['immense_pct']:.0f}% IT | "
                f"Ground \~{gnd['win_pct']:.0f}%/\~{gnd['immense_pct']:.0f}% IT | "
                f"Naval \~{nav['win_pct']:.0f}%/\~{nav['immense_pct']:.0f}% IT"
            )
            tips = advice_for_war(
                our=our_f, enemy=en_f,
                our_map=our_map, enemy_map=enemy_map,
                our_res=our_res, enemy_res=enemy_res,
                controls=controls,
            )
            lines.append("Tactical brief:\n" + "\n".join(f"- {t}" for t in tips))
        else:
            lines.append(
                "No active war found between speaker and this room's enemy "
                "(speaker may be a third slot partner — advise the stack)."
            )
            lines.append(
                f"Enemy still: planes={enemy.get('aircraft')} ships={enemy.get('ships')} "
                f"S={enemy.get('soldiers')} T={enemy.get('tanks')}"
            )
    except Exception as e:
        lines.append(f"(war room context error: {e})")

    lines.append(
        "ATTACK TYPES (be specific when advising):\n"
        "- Airstrike targets: aircraft (dogfight / take or break AS), soldiers, tanks, "
        "ships, infrastructure, money. Always costs 4 MAP. Immense on air = air superiority "
        "(enemy tanks at 50%). Any air win breaks enemy AS.\n"
        "- Ground: 3 MAP. Immense = ground control (later ground wins chip enemy planes). "
        "Only ground loots cash.\n"
        "- Naval targets: ships (standard), also naval can pressure infra/ground/air variants "
        "in-game where available. 4 MAP. Immense = blockade. Any naval win breaks their blockade "
        "on you; ally naval IT on the blockader frees partners.\n"
        "- Missile 8 MAP, Nuke 12 MAP.\n"
        "When someone says 'airstrike' pick the TARGET (usually aircraft if AS not held, "
        "else tanks/ships/soldiers to strip mil). Say yes/no with MAP check and odds."
    )
    return "\n".join(lines)




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

    async def _answer(
        self,
        guild_id: int,
        question: str,
        author: str,
        *,
        channel_id: int | None = None,
        author_id: int | None = None,
    ) -> str:
        knowledge = await self._knowledge_context(guild_id)
        live = await _live_pw_context(self.bot, guild_id, question)
        war_ctx = ""
        if channel_id and author_id:
            war_ctx = await _war_room_tactical_context(self.bot, channel_id, author_id)

        system = (
            "You are Evergreen MILCOM gov. Speak like a human milcom lead in Discord — not a guide, not a calculator dump. "
            "RULES FOR EVERY REPLY:\n"
            "1) Max 2–4 short lines. No walls of text.\n"
            "2) Tactical questions (airstrike/ground/naval/what next): start with YES, NO, or WAIT.\n"
            "3) Then name the exact action: airstrike aircraft | airstrike tanks | airstrike ships | "
            "airstrike soldiers | airstrike infra | airstrike money | ground | naval ships.\n"
            "4) One reason only (MAP / AS / GC / blockade / odds). Do NOT paste full unit tables or formula essays.\n"
            "5) Use WAR ROOM context as truth. MAP: ground 3, air/naval 4. If MAP too low → WAIT.\n"
            "6) If they already hold air superiority → prefer ground, not another dogfight.\n"
            "7) Never explain how to click buttons in the game UI. Never mention AI/models.\n"
            "Example good reply: 'NO — only 2 MAP. Wait a turn, then ground (you already have AS).'\n"
            "Example bad reply: long tutorial about the airstrike button or listing every calculation."
        )
        user = (
            f"Alliance knowledge:\n{knowledge}\n\n"
            f"Live game data:\n{live or '(none)'}\n\n"
            f"War room tactical context:\n{war_ctx or '(not a war room / no data)'}\n\n"
            f"Member {author} says:\n{question}"
        )
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

        # Spy channel = desk ONLY
        if SPY_CHANNEL_ID and message.channel.id == SPY_CHANNEL_ID:
            try:
                await self._handle_spy_desk(message)
            except Exception:
                log.exception("spy desk failed")
            return

        content_raw = message.content or ""
        is_mention = False
        if self.bot.user:
            uid = self.bot.user.id
            is_mention = (
                self.bot.user in message.mentions
                or f"<@{uid}>" in content_raw
                or f"<@!{uid}>" in content_raw
            )
        is_reply_to_bot = False
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message) and self.bot.user and ref.author.id == self.bot.user.id:
                is_reply_to_bot = True

        if not (is_mention or is_reply_to_bot):
            return

        log.info("AI ping from %s in #%s: %s", message.author.id, getattr(message.channel, "name", "?"), content_raw[:80])

        content = content_raw
        if self.bot.user:
            content = content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
        if not content:
            content = "Help me with this."

        import asyncio
        answer = "…"
        async with message.channel.typing():
            try:
                answer = await asyncio.wait_for(
                    self._answer(
                        message.guild.id, content, str(message.author),
                        channel_id=message.channel.id, author_id=message.author.id,
                    ),
                    timeout=45,
                )
            except asyncio.TimeoutError:
                answer = "API is slow — try again in a minute."
            except Exception as e:
                log.exception("AI reply failed")
                err = str(e)
                if "rate limit" in err.lower():
                    answer = "PnW rate limit — wait about 30s."
                elif "GROQ" in err or "not configured" in err.lower():
                    answer = "AI key missing on Render (GROQ_API_KEY)."
                else:
                    answer = "Couldn't answer — try again."
        try:
            await message.reply(answer, mention_author=False)
        except Exception as e:
            log.warning("reply failed: %s", e)
            try:
                await message.channel.send(f"{message.author.mention} {answer}"[:1900])
            except Exception as e2:
                log.warning("channel.send failed: %s", e2)

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
                lines.append(f"\n**Worth:** {format_money(total)}")
                lines.append(f"**You would loot:** {format_money(loot_est)}")
            else:
                lines.append("Could not parse resources from that report.")

            if report.op_cost_money is not None:
                lines.append(f"• Op cost: {format_money(report.op_cost_money)}")
                if report.resources and report.op_cost_money > 0:
                    total = value_resources(report.resources, prices)
                    lines.append(f"• Value / cost: **{total / report.op_cost_money:.1f}x**")

        else:
            # sabotage / spykill / other ops
            kind_label = {
                "sabotage": "Sabotage",
                "spykill": "Spy kill",
                "other": "Spy op",
            }.get(report.kind, "Spy op")
            lines.append(f"**{kind_label} — {title_target}**")

            damage_value = 0.0
            if report.destroyed:
                for unit, count in report.destroyed.items():
                    val = unit_production_value(unit, count, prices)
                    damage_value += val
                    lines.append(f"• Destroyed {count}× {unit}: \~{format_money(val)} build value")
            if report.spies_killed and "spy" not in report.destroyed:
                val = unit_production_value("spy", report.spies_killed, prices)
                damage_value += val
                lines.append(f"• Enemy spies killed: {report.spies_killed} (\~{format_money(val)})")

            op_cost = float(report.op_cost_money or 0)
            if report.op_cost_money is not None:
                lines.append(f"• Op cost: {format_money(op_cost)}")

            spy_loss_cost = 0.0
            if report.spies_lost:
                spy_loss_cost = report.spies_lost * SPY_RECRUIT_COST
                lines.append(
                    f"• Your spies lost: {report.spies_lost} "
                    f"(\~{format_money(spy_loss_cost)} recruit cost)"
                )

            net = damage_value - op_cost - spy_loss_cost
            lines.append(f"\n**Net value:** {format_money(net)}")

        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "\n..."
        try:
            await message.reply(body, mention_author=False)
        except discord.HTTPException as e:
            log.warning("[spy] reply failed: %s", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
