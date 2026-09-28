"""Chat with Ember from Discord.

A Discord bot (``DISCORD_BOT_TOKEN`` + ``DISCORD_CHANNEL_ID``) keeps a gateway
connection open and listens in that channel. A message there that tags
@Ember (a reply to the evening message included) opens a thread and a new
Ember conversation; every message in the thread continues it, no tag needed.
Untagged messages in the channel are left alone. The conversation is an ordinary chat
(same tools, same limits) and also appears on the Ember page.

Only allowed Discord users are answered: ``HART_DISCORD_ALLOWED_USERS``, or by
default the owner of the bot application. Everyone else, other bots and
webhooks are ignored — the channel may have other members, and the answers
contain health data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

from hart.config import HartSettings
from hart.server import state
from hart.server.chat import ChatBusy, ChatService
from hart.storage.database import Database

logger = logging.getLogger(__name__)

THREADS_KEY = "discord_threads"  # thread id → conversation id
MESSAGE_LIMIT = 2000
THREAD_NAME_LEN = 60


def configured(config: HartSettings) -> bool:
    return bool(config.discord.bot_token and config.discord.channel_id)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def to_discord(markdown: str) -> str:
    """Discord renders most markdown but not tables: show those as code blocks."""
    out: list[str] = []
    table: list[str] = []
    in_code = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        is_row = not in_code and line.strip().startswith("|") and line.strip().endswith("|")
        if is_row:
            if not re.fullmatch(r"\|[\s:\-|]+\|", line.strip()):  # drop the |---|---| separator
                table.append(line.strip())
            continue
        if table:
            out += ["```", *table, "```"]
            table = []
        out.append(line)
    if table:
        out += ["```", *table, "```"]
    return "\n".join(out).strip()


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split into Discord-sized messages at line breaks; a code block cut in two is closed and reopened."""
    width = limit - 8  # room for a closing/opening fence
    lines = [
        piece for line in text.splitlines() for piece in (line[i : i + width] for i in range(0, len(line) or 1, width))
    ]
    chunks: list[str] = []
    current = ""
    in_code = False
    for line in lines:
        now_in_code = in_code != line.lstrip().startswith("```")
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) + (4 if now_in_code else 0) > limit:
            chunks.append(current + ("\n```" if in_code else ""))
            current = ("```\n" if in_code else "") + line
        else:
            current = candidate
        in_code = now_in_code
    if current.strip():
        chunks.append(current)
    return chunks


MENTION = re.compile(r"<(?:@[!&]?|#)\d+>")


def clean_text(text: str) -> str:
    """The message without Discord's mention codes (``<@123…>`` for @Ember and other tags)."""
    return re.sub(r"[ \t]{2,}", " ", MENTION.sub("", text or "")).strip()


def _thread_name(text: str) -> str:
    """The first line of the question, capitalised and shortened: what the thread list shows."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    name = " ".join(first.split()) or "Ember"
    name = name[0].upper() + name[1:]
    return name if len(name) <= THREAD_NAME_LEN else name[: THREAD_NAME_LEN - 1].rstrip() + "…"


def _embed_text(message: Any) -> str:
    """What a referenced message said: its text, or the evening message's embed."""
    parts = [getattr(message, "content", "") or ""]
    for embed in getattr(message, "embeds", None) or []:
        parts += [embed.title or "", embed.description or ""]
        parts += [f"{f.name}: {f.value}" for f in getattr(embed, "fields", None) or []]
    return " · ".join(p.strip() for p in parts if p and p.strip())


# ---------------------------------------------------------------------------
# Message handling (no discord.py types here, so tests can use plain fakes)
# ---------------------------------------------------------------------------


class DiscordChat:
    def __init__(self, db: Database, chat: ChatService, config: HartSettings) -> None:
        self._db = db
        self._chat = chat
        self._channel_id = config.discord.channel_id
        self.allowed_users: set[int] = set(config.discord.allowed_user_ids)
        self.bot_user_id: int | None = None  # set once connected

    def _asked(self, message: Any) -> bool:
        """Tagged: @Ember in the text, or a reply to Ember with the mention left on (Discord's default)."""
        if self.bot_user_id is None:
            return False
        mentioned = {getattr(u, "id", None) for u in getattr(message, "mentions", None) or []}
        content = message.content or ""
        return self.bot_user_id in mentioned or any(f"<@{p}{self.bot_user_id}>" in content for p in ("", "!"))

    def _threads(self) -> dict[str, str]:
        with contextlib.closing(self._db.cursor()) as cur:
            return state.get_setting(cur, THREADS_KEY, {}) or {}

    def _remember(self, thread_id: int, conv_id: str) -> None:
        threads = self._threads()
        threads[str(thread_id)] = conv_id
        with contextlib.closing(self._db.cursor()) as cur:
            state.set_setting(cur, THREADS_KEY, threads)

    async def handle(self, message: Any) -> None:
        author = message.author
        if getattr(author, "bot", False) or getattr(message, "webhook_id", None):
            return
        channel = message.channel
        in_thread = getattr(channel, "parent_id", None) == self._channel_id
        if not in_thread and getattr(channel, "id", None) != self._channel_id:
            return
        if not in_thread and not self._asked(message):
            return  # only answer in the channel when tagged; threads Ember started need no tag
        if author.id not in self.allowed_users:
            logger.info("Discord: ignoring a message from user %s (not allowed)", author.id)
            return
        text = clean_text(message.content)
        if not text:
            return

        if in_thread:
            conv_id = self._threads().get(str(channel.id))
            if conv_id is None or self._chat.get(conv_id) is None:
                return  # a thread hart didn't start
            target = channel
        else:
            label = "Discord"
            reference = getattr(message, "reference", None)
            if reference is not None and getattr(reference, "message_id", None):
                referenced = getattr(reference, "resolved", None)
                if referenced is None:
                    with contextlib.suppress(Exception):
                        referenced = await channel.fetch_message(reference.message_id)
                said = _embed_text(referenced) if referenced is not None else ""
                if said:
                    label = f"Discord — a reply to this message: {said[:1500]}"
            conv_id = self._chat.create(context_ref={"source": "discord", "label": label})
            target = await message.create_thread(name=_thread_name(text), auto_archive_duration=1440)
            self._remember(target.id, conv_id)

        try:
            key = await self._chat.send(conv_id, text)
        except ChatBusy:
            await target.send("Still answering your previous message — send this again when I'm done.")
            return
        async with target.typing():
            async for _index, event in self._chat.events(key):
                if event.get("type") == "done":
                    break
        conversation = self._chat.get(conv_id) or {"messages": []}
        replies = [m for m in conversation["messages"] if m["role"] == "assistant"]
        answer = replies[-1]["content"] if replies else "Something went wrong — no answer was saved."
        for part in split_message(to_discord(answer)):
            await target.send(part)


# ---------------------------------------------------------------------------
# The gateway connection
# ---------------------------------------------------------------------------


class DiscordBot:
    """Runs the discord.py client as a task in the server's event loop."""

    def __init__(self, db: Database, chat: ChatService, config: HartSettings) -> None:
        self.handler = DiscordChat(db, chat, config)
        self._token = config.discord.bot_token
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None
        self.status: dict[str, Any] = {"state": "starting", "detail": None}

    async def start(self) -> None:
        import discord

        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it on the bot's page in the developer portal
        client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self._client = client

        @client.event
        async def on_ready() -> None:
            if not self.handler.allowed_users:
                info = await client.application_info()
                owners = [m.id for m in info.team.members] if info.team else [info.owner.id]
                self.handler.allowed_users = set(owners)
            self.handler.bot_user_id = client.user.id
            self.status = {"state": "connected", "detail": str(client.user)}
            logger.info("Discord: connected as %s", client.user)

        @client.event
        async def on_message(message: Any) -> None:
            try:
                await self.handler.handle(message)
            except Exception:  # noqa: BLE001 — one bad message must not stop the bot
                logger.exception("Discord: handling a message failed")
                with contextlib.suppress(Exception):
                    await message.channel.send("Something went wrong on my side — see the server log.")

        async def run() -> None:
            try:
                await client.start(self._token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                hint = (
                    " — enable the Message Content intent on the bot's page in the Discord developer portal"
                    if "intent" in str(exc).lower()
                    else ""
                )
                self.status = {"state": "error", "detail": f"{exc}{hint}"}
                logger.error("Discord bot stopped: %s%s", exc, hint)

        self._task = asyncio.create_task(run(), name="discord-bot")

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
