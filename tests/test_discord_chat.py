"""Chat with Ember from Discord: routing, permissions, formatting."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from hart.config import get_config
from hart.server import discord_chat
from hart.server.chat import ChatService
from hart.server.claude.runner import ClaudeRunner
from hart.storage.database import Database
from tests.test_chat import FakeClient, _reset_fake  # noqa: F401 — autouse fixture

CHANNEL = 111
ME = 42


# ---------------------------------------------------------------------------
# Plain stand-ins for discord.py objects
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Author:
    id: int
    bot: bool = False


class Channel:
    def __init__(self, id: int, parent_id: int | None = None) -> None:
        self.id = id
        self.parent_id = parent_id
        self.sent: list[str] = []
        self.history: dict[int, Any] = {}

    async def send(self, text: str) -> None:
        self.sent.append(text)

    @contextlib.asynccontextmanager
    async def typing(self):
        yield

    async def fetch_message(self, message_id: int) -> Any:
        return self.history[message_id]


@dataclasses.dataclass
class Embed:
    title: str | None
    description: str | None
    fields: list[Any] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Reference:
    message_id: int
    resolved: Any = None


class Message:
    def __init__(self, channel: Channel, text: str, author: int = ME, *, bot=False, webhook_id=None, reference=None):
        self.channel = channel
        self.content = text
        self.author = Author(author, bot)
        self.webhook_id = webhook_id
        self.reference = reference
        self.embeds: list[Embed] = []
        self.thread: Channel | None = None

    async def create_thread(self, name: str, auto_archive_duration: int) -> Channel:
        self.thread = Channel(900 + len(name), parent_id=self.channel.id)
        self.thread.name = name
        return self.thread


# ---------------------------------------------------------------------------


@pytest.fixture
def setup(tmp_path: Path):
    db = Database(tmp_path / "d.duckdb").connect()
    base = get_config()
    config = dataclasses.replace(
        base,
        db_path=tmp_path / "d.duckdb",
        discord=dataclasses.replace(base.discord, bot_token="t", channel_id=CHANNEL, allowed_user_ids=(ME,)),
    )
    runner = ClaudeRunner(
        db, mcp_url="http://127.0.0.1:1/mcp", internal_token="x", workspace=tmp_path, client_factory=FakeClient
    )
    chat = ChatService(db, runner, config)
    yield discord_chat.DiscordChat(db, chat, config), chat
    db.close()


def test_channel_message_opens_a_thread_and_answers(setup) -> None:
    bot, chat = setup
    channel = Channel(CHANNEL)
    message = Message(channel, "How is my fitness?")
    asyncio.run(bot.handle(message))
    thread = message.thread
    assert thread is not None and thread.name == "How is my fitness?"
    assert thread.sent == ["Your CTL is **20.1**."]
    conv = chat.list()[0]
    assert conv["title"] == "How is my fitness?"
    assert "Written in Discord" in FakeClient.instances[0].prompt

    # A follow-up in the thread continues the same conversation.
    asyncio.run(bot.handle(Message(thread, "And last month?")))
    assert len(chat.list()) == 1 and len(chat.get(conv["id"])["messages"]) == 4
    assert thread.sent[-1] == "Your CTL is **20.1**."


def test_reply_to_evening_message_carries_its_content(setup) -> None:
    bot, _chat = setup
    channel = Channel(CHANNEL)
    evening = Message(channel, "", author=7, webhook_id=5)
    evening.embeds = [Embed("🌲 Tomorrow · Tuesday 29 Sep", "🧭 **Free choice** — easy run")]
    channel.history[1] = evening
    asyncio.run(bot.handle(Message(channel, "Can I swim instead?", reference=Reference(1))))
    prompt = FakeClient.instances[0].prompt
    assert "a reply to this message: 🌲 Tomorrow · Tuesday 29 Sep" in prompt and "easy run" in prompt


def test_ignores_strangers_bots_webhooks_and_other_channels(setup) -> None:
    bot, chat = setup
    channel = Channel(CHANNEL)
    for message in (
        Message(channel, "hi", author=99),  # not allowed
        Message(channel, "hi", bot=True),
        Message(channel, "hi", webhook_id=5),
        Message(Channel(222), "hi"),  # another channel
        Message(Channel(333, parent_id=CHANNEL), "hi"),  # a thread hart didn't start
    ):
        asyncio.run(bot.handle(message))
        assert message.thread is None and not message.channel.sent
    assert chat.list() == [] and not FakeClient.instances


def test_formatting_tables_and_splitting() -> None:
    table = "Week:\n| Sport | Hours |\n|---|---|\n| Run | 3 |\nDone."
    assert discord_chat.to_discord(table) == "Week:\n```\n| Sport | Hours |\n| Run | 3 |\n```\nDone."

    text = "\n".join(f"line {i} " + "x" * 80 for i in range(60))
    parts = discord_chat.split_message(text)
    assert len(parts) > 1 and all(len(p) <= 2000 for p in parts)
    assert "\n".join(parts) == text

    code = "Intro\n```\n" + "\n".join("y" * 90 for _ in range(40)) + "\n```\nEnd"
    parts = discord_chat.split_message(code)
    assert all(len(p) <= 2000 and p.count("```") % 2 == 0 for p in parts)  # every part's code block is closed
    assert discord_chat.split_message("x" * 4500)[0] == "x" * 1992
