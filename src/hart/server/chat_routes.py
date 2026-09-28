"""Chat pages and API."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from hart.server import data
from hart.server.chat import CHAT_MODELS, ChatBusy, ChatService
from hart.server.routes import markdown, page

router = APIRouter()
HEARTBEAT_S = 15


def _chat(request: Request) -> ChatService:
    return request.app.state.chat


class NewChat(BaseModel):
    model: str | None = None
    context: str | None = Field(default=None, description="e.g. 'activity:<id>' or 'date:2026-09-26'")


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ChatPatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    archived: bool | None = None
    model: str | None = None


def _default_model(request: Request) -> str:
    from hart.server import settings

    with request.app.state.db.cursor() as cur:
        return settings.get(cur, "model_chat")


def resolve_context(request: Request, context: str | None) -> dict[str, Any] | None:
    """Turn 'activity:<id>' / 'date:<iso>' into a labelled context reference."""
    if not context or ":" not in context:
        return None
    kind, _, value = context.partition(":")
    if kind == "activity":
        with request.app.state.db.cursor() as cur:
            act = data.one(cur, "SELECT name, sport_type, start_time FROM activities WHERE activity_id = ?", [value])
        if act is None:
            return None
        when = act["start_time"].strftime("%Y-%m-%d %H:%M") if act.get("start_time") else ""
        return {"type": "activity", "id": value,
                "label": f"session {value} — {act['name'] or act['sport_type']} on {when}"}
    if kind in ("date", "readiness", "season", "suggestion"):
        labels = {"date": f"the day {value}", "readiness": f"today's readiness ({value})", "season": "the season plan",
                  "suggestion": f"the training suggestion for {value} (see get_daily_suggestion and get_planned_sessions)"}
        return {"type": kind, "id": value, "label": labels[kind]}
    return None


def _render_messages(conversation: dict[str, Any]) -> None:
    for m in conversation["messages"]:
        m["content_html"] = str(markdown(m["content"])) if m["role"] == "assistant" else None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/chat", response_class=HTMLResponse, include_in_schema=False)
def chat_page(request: Request, context: str | None = None) -> HTMLResponse:
    chat = _chat(request)
    return page(request, "chat.html", {
        "conversations": chat.list(), "conversation": None, "models": CHAT_MODELS,
        "default_model": _default_model(request),
        "context": resolve_context(request, context), "context_param": context or "",
        "web_access": chat.web_access(), "nav": "chat",
    })


@router.get("/chat/{conv_id}", response_class=HTMLResponse, include_in_schema=False)
def conversation_page(request: Request, conv_id: str) -> HTMLResponse:
    chat = _chat(request)
    conversation = chat.get(conv_id)
    if conversation is None:
        raise HTTPException(404, detail="conversation not found")
    _render_messages(conversation)
    return page(request, "chat.html", {
        "conversations": chat.list(), "conversation": conversation, "models": CHAT_MODELS,
        "default_model": conversation["model"], "context": conversation.get("context_ref"), "context_param": "",
        "web_access": chat.web_access(), "nav": "chat",
    })


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/chat")
def api_list(request: Request, archived: bool = False) -> list[dict[str, Any]]:
    return _chat(request).list(include_archived=archived)


@router.post("/api/chat")
def api_create(body: NewChat, request: Request) -> dict[str, Any]:
    return {"id": _chat(request).create(body.model, resolve_context(request, body.context))}


@router.get("/api/chat/{conv_id}")
def api_get(conv_id: str, request: Request) -> dict[str, Any]:
    conversation = _chat(request).get(conv_id)
    if conversation is None:
        raise HTTPException(404, detail="conversation not found")
    _render_messages(conversation)
    return conversation


@router.patch("/api/chat/{conv_id}")
def api_patch(conv_id: str, body: ChatPatch, request: Request) -> dict[str, Any]:
    chat = _chat(request)
    if chat.get(conv_id) is None:
        raise HTTPException(404, detail="conversation not found")
    chat.update(conv_id, title=body.title, archived=body.archived, model=body.model)
    return {"id": conv_id}


@router.post("/api/chat/{conv_id}/messages")
async def api_send(conv_id: str, body: NewMessage, request: Request) -> dict[str, Any]:
    try:
        key = await _chat(request).send(conv_id, body.text.strip())
    except KeyError as exc:
        raise HTTPException(404, detail="conversation not found") from exc
    except ChatBusy as exc:
        raise HTTPException(409, detail="Claude is still answering in this conversation") from exc
    return {"run_id": key}


@router.post("/api/chat/runs/{run_id}/stop")
async def api_stop(run_id: str, request: Request) -> dict[str, Any]:
    return {"stopped": await _chat(request).stop(run_id)}


@router.get("/api/chat/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request, after: int = -1) -> StreamingResponse:
    last_id = request.headers.get("last-event-id")
    if last_id and last_id.isdigit():
        after = max(after, int(last_id))
    chat = _chat(request)

    async def stream() -> AsyncIterator[bytes]:
        queue: asyncio.Queue[tuple[int, dict[str, Any]] | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for item in chat.events(run_id, after):
                    await queue.put(item)
            finally:
                await queue.put(None)

        task = asyncio.create_task(pump())
        try:
            yield b"retry: 2000\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"  # keeps proxies from closing an idle stream
                    continue
                if item is None:
                    yield b"event: end\ndata: {}\n\n"
                    return
                idx, event = item
                yield f"id: {idx}\ndata: {json.dumps(event, default=str)}\n\n".encode()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
