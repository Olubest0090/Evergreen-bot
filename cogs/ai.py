"""Evergreen AI (Gemini) — knowledge channel + @mention / reply-to-bot."""

import os
import re

import aiohttp
import discord
from discord.ext import commands

from utils import database

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

KNOWLEDGE_CHANNEL_ID = int(os.environ.get("AI_KNOWLEDGE_CHANNEL_ID", "0") or 0)
SPY_CHANNEL_ID = int(os.environ.get("AI_SPY_CHANNEL_ID", "0") or 0)
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


async def gemini_generate(session: aiohttp.ClientSession, system: str, user: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "AI is not configured (missing GEMINI_API_KEY on Render)."

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024},
    }
    url = f"{GEMINI_URL}?key={api_key}"
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = (data.get("error") or {}).get("message") or str(data)[:300]
                return f"Gemini error: {err}"
            candidates = data.get("candidates") or []
            if not candidates:
                return "Gemini returned no answer."
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or "Empty response from Gemini."
    except Exception as e:
        return f"Gemini request failed: {e}"


class AI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        system = (
            "You are Evergreen MILCOM assistant for Politics & War. "
            "Be concise and practical. Use alliance knowledge when relevant. "
            "If unsure, say so. Never invent exact private stockpile numbers. "
            "Never reveal secrets or API keys."
        )
        user = f"Alliance knowledge:\n{knowledge}\n\nMember {author} asks:\n{question}"
        session = self.bot.http_session
        reply = await gemini_generate(session, system, user)
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
            return  # Phase 2

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


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
