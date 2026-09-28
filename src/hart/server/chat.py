"""Chat with Claude.

A chat turn runs as a server-side task that outlives the HTTP connection:
events are numbered and buffered, the SSE stream only observes them, and a
client that reconnects (phone locked, network blip) resumes from the last
event id.  Follow-up turns resume the same Claude Code session; if that
session is gone, the turn restarts with a summary of recent messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from hart.config import HartSettings
from hart.server import settings, state
from hart.server.claude.policy import chat_policy
from hart.server.claude.prompts import CHAT_PROMPT_VERSION, athlete_profile, chat_system_prompt
from hart.server.claude.runner import ClaudeRunner, RunOutcome, RunSpec
from hart.server.data import rows
from hart.storage.database import Database

logger = logging.getLogger(__name__)

WEB_ACCESS_KEY = "chat_web_access"
KEEP_FINISHED_RUN_S = 600
HISTORY_FOR_SUMMARY = 10
TITLE_LEN = 60
CHAT_MODELS = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5": "Sonnet 5", "claude-haiku-4-5": "Haiku 4.5"}


class ChatBusy(Exception):
    """A run is already active in this conversation."""


@dataclass
class LiveRun:
    key: str
    conversation_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: bool = False
    finished_at: float | None = None
    claude_run_id: int | None = None
    task: asyncio.Task[None] | None = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class ChatService:
    def __init__(self, db: Database, runner: ClaudeRunner, config: HartSettings) -> None:
        self._db = db
        self._runner = runner
        self._config = config
        self._runs: dict[str, LiveRun] = {}
        self._by_conversation: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def create(self, model: str | None = None, context_ref: dict[str, Any] | None = None) -> str:
        conv_id = str(uuid.uuid4())
        with contextlib.closing(self._db.cursor()) as cur:
            model = model if model in CHAT_MODELS else settings.get(cur, "model_chat")
            cur.execute(
                "INSERT INTO chat_conversations (id, model, context_ref) VALUES (?, ?, ?)",
                [conv_id, model, json.dumps(context_ref) if context_ref else None],
            )
        return conv_id

    def list(self, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE NOT c.archived"
        with contextlib.closing(self._db.cursor()) as cur:
            items = rows(
                cur,
                "SELECT c.id, c.title, c.model, c.archived, c.created_at, c.updated_at, "
                "(SELECT count(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS messages "
                f"FROM chat_conversations c {where} ORDER BY c.updated_at DESC",
            )
        for item in items:
            item["active_run"] = self._by_conversation.get(item["id"])
        return items

    def get(self, conv_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._db.cursor()) as cur:
            conv = rows(
                cur,
                "SELECT id, title, model, claude_session_id, context_ref, archived, created_at, updated_at "
                "FROM chat_conversations WHERE id = ?",
                [conv_id],
            )
            if not conv:
                return None
            messages = rows(
                cur,
                "SELECT id, role, content, tool_calls, claude_run_id, created_at FROM chat_messages "
                "WHERE conversation_id = ? ORDER BY id",
                [conv_id],
            )
        conversation = conv[0]
        if isinstance(conversation.get("context_ref"), str):
            conversation["context_ref"] = json.loads(conversation["context_ref"])
        for m in messages:
            if isinstance(m.get("tool_calls"), str):
                m["tool_calls"] = json.loads(m["tool_calls"])
        conversation["messages"] = messages
        conversation["active_run"] = self._by_conversation.get(conv_id)
        return conversation

    def update(
        self, conv_id: str, *, title: str | None = None, archived: bool | None = None, model: str | None = None
    ) -> None:
        with contextlib.closing(self._db.cursor()) as cur:
            if title is not None:
                cur.execute("UPDATE chat_conversations SET title = ? WHERE id = ?", [title.strip()[:120], conv_id])
            if archived is not None:
                cur.execute("UPDATE chat_conversations SET archived = ? WHERE id = ?", [archived, conv_id])
            if model is not None and model in CHAT_MODELS:
                cur.execute("UPDATE chat_conversations SET model = ? WHERE id = ?", [model, conv_id])

    def web_access(self) -> bool:
        with contextlib.closing(self._db.cursor()) as cur:
            return bool(state.get_setting(cur, WEB_ACCESS_KEY, True))

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    async def send(self, conv_id: str, text: str) -> str:
        conversation = self.get(conv_id)
        if conversation is None:
            raise KeyError(conv_id)
        if conv_id in self._by_conversation:
            raise ChatBusy(conv_id)
        self._prune()

        prior = conversation["messages"]
        with contextlib.closing(self._db.cursor()) as cur:
            cur.execute(
                "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?, 'user', ?)", [conv_id, text]
            )
            if not conversation["title"]:
                title = " ".join(text.split())[:TITLE_LEN]
                cur.execute("UPDATE chat_conversations SET title = ? WHERE id = ?", [title, conv_id])
            cur.execute("UPDATE chat_conversations SET updated_at = ? WHERE id = ?", [_now(), conv_id])

        live = LiveRun(key=uuid.uuid4().hex, conversation_id=conv_id)
        self._runs[live.key] = live
        self._by_conversation[conv_id] = live.key
        live.task = asyncio.create_task(self._turn(live, conversation, prior, text), name=f"chat-{live.key}")
        return live.key

    async def stop(self, key: str) -> bool:
        live = self._runs.get(key)
        if live is None or live.done or live.claude_run_id is None:
            return False
        return await self._runner.interrupt(live.claude_run_id)

    async def events(self, key: str, after: int = -1) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Yield (event id, event) from *after*+1 until the run finishes."""
        live = self._runs.get(key)
        if live is None:
            yield 0, {"type": "gone"}
            return
        index = after + 1
        while True:
            async with live.changed:
                while index >= len(live.events) and not live.done:
                    await live.changed.wait()
            while index < len(live.events):
                yield index, live.events[index]
                index += 1
            if live.done and index >= len(live.events):
                return

    def _emit(self, live: LiveRun, event: dict[str, Any]) -> None:
        if event.get("type") == "run":
            live.claude_run_id = event["run_id"]
        live.events.append(event)

        async def notify() -> None:
            async with live.changed:
                live.changed.notify_all()

        asyncio.get_running_loop().create_task(notify())

    def _prompt(self, conversation: dict[str, Any], prior: list[dict[str, Any]], text: str, resume: bool) -> str:
        parts = []
        ctx = conversation.get("context_ref")
        if ctx and not prior:
            parts.append(f"(Context the athlete opened this chat from: {ctx.get('label') or json.dumps(ctx)})")
        if ctx and ctx.get("source") == "discord":
            parts.append(
                "(Written in Discord: answer briefly — a few short paragraphs or a short list. Discord shows "
                "tables only as plain text, so avoid them.)"
            )
        if prior and not resume:
            recent = prior[-HISTORY_FOR_SUMMARY:]
            history = "\n".join(f"{m['role']}: {m['content'][:800]}" for m in recent)
            parts.append(f"(Earlier in this conversation — the previous session could not be restored:\n{history}\n)")
        parts.append(text)
        return "\n\n".join(parts)

    async def _turn(self, live: LiveRun, conversation: dict[str, Any], prior: list[dict[str, Any]], text: str) -> None:
        emit = lambda event: self._emit(live, event)  # noqa: E731
        tz = self._config.server.tz
        today = datetime.datetime.now(ZoneInfo(tz)).date()
        resume = conversation.get("claude_session_id")
        with contextlib.closing(self._db.cursor()) as cur:
            profile = athlete_profile(cur)

        def spec(resume_id: str | None) -> RunSpec:
            return RunSpec(
                purpose="chat",
                prompt=self._prompt(conversation, prior, text, resume=bool(resume_id)),
                model=conversation["model"],
                system_prompt=chat_system_prompt(today, tz, profile),
                prompt_version=CHAT_PROMPT_VERSION,
                policy=chat_policy(web=self.web_access()),
                max_turns=30,
                timeout_s=600,
                resume=resume_id,
            )

        outcome: RunOutcome | None = None
        try:
            outcome = await self._runner.run(spec(resume), emit)
            if resume and outcome.status == "error" and not outcome.text and _session_missing(outcome.error):
                logger.info("Chat session %s not found; restarting with a summary", resume)
                emit({"type": "notice", "message": "Previous session expired — continuing from a summary."})
                outcome = await self._runner.run(spec(None), emit)
            self._save_reply(live, outcome)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chat turn failed")
            emit({"type": "error", "code": "internal", "message": str(exc)})
        finally:
            live.done = True
            live.finished_at = time.monotonic()
            self._by_conversation.pop(live.conversation_id, None)
            async with live.changed:
                live.changed.notify_all()

    def _save_reply(self, live: LiveRun, outcome: RunOutcome) -> None:
        text = outcome.text
        if outcome.status == "cancelled":
            text = (text + "\n\n" if text else "") + "*(stopped)*"
        elif outcome.status != "ok" and not text:
            text = {
                "usage_limited": "Claude usage limit reached — try again when the limit resets.",
                "auth_failed": "Claude couldn't authenticate — the token may have expired (see System).",
            }.get(outcome.status, f"Something went wrong: {outcome.error}")
        with contextlib.closing(self._db.cursor()) as cur:
            message_id = cur.fetchone(
                "INSERT INTO chat_messages (conversation_id, role, content, tool_calls, claude_run_id) "
                "VALUES (?, 'assistant', ?, ?, ?) RETURNING id",
                [live.conversation_id, text, json.dumps(outcome.tool_calls, default=str), outcome.run_id],
            )[0]
            if outcome.session_id:
                cur.execute(
                    "UPDATE chat_conversations SET claude_session_id = ?, updated_at = ? WHERE id = ?",
                    [outcome.session_id, _now(), live.conversation_id],
                )
        from hart.server.routes import markdown

        self._emit(
            live,
            {
                "type": "done",
                "status": outcome.status,
                "message_id": message_id,
                "content_html": str(markdown(text)),
                "error": outcome.error,
                "resets_at": outcome.resets_at,
            },
        )

    def _prune(self) -> None:
        cutoff = time.monotonic() - KEEP_FINISHED_RUN_S
        for key in [k for k, r in self._runs.items() if r.done and (r.finished_at or 0) < cutoff]:
            del self._runs[key]


def _session_missing(error: str | None) -> bool:
    text = (error or "").lower()
    return "no conversation found" in text or ("session" in text and "not found" in text)
