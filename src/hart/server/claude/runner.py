"""Run Claude Code through the Agent SDK on the Claude subscription.

Every run — chat turn, grade, suggestion — goes through :class:`ClaudeRunner`:
it builds the SDK options (MCP over loopback, tool policy, permission
callback with the web guard), streams events to the caller, records the run
in ``claude_runs`` with a tool-call transcript, and classifies failures
(usage limit, expired token, timeout, cancellation).

Authentication comes from ``CLAUDE_CODE_OAUTH_TOKEN`` in the environment
(``claude setup-token``); the CLI binary is bundled with the SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hart.server.claude.policy import MCP_PREFIX, MCP_SERVER, ToolPolicy, WebGuard
from hart.storage.database import Database

logger = logging.getLogger(__name__)

# Auto-approved (allowed_tools) MCP tools skip can_use_tool by design: the
# callback only needs to see the web tools and anything not on the list.
warnings.filterwarnings("ignore", message=r"can_use_tool will not be invoked")

Emit = Callable[[dict[str, Any]], None]
OUTPUT_EXCERPT = 2000


@dataclass
class RunSpec:
    purpose: str  # chat | grade | suggest | parse_plan
    prompt: str
    model: str
    system_prompt: str
    prompt_version: str
    policy: ToolPolicy
    max_turns: int = 30
    timeout_s: float = 600
    resume: str | None = None
    background: bool = False
    output_schema: dict[str, Any] | None = None  # JSON schema → structured output


@dataclass
class RunOutcome:
    run_id: int
    status: str = "running"  # ok | error | cancelled | usage_limited | auth_failed
    text: str = ""
    session_id: str | None = None
    error: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    num_turns: int | None = None
    duration_ms: int | None = None
    resets_at: int | None = None  # usage limit reset (unix time), if reported
    structured: Any = None  # parsed structured output when output_schema was set
    tool_outputs: list[str] = field(default_factory=list)  # full texts, for citation checks
    usage: dict[str, Any] | None = None  # token counts reported by the CLI
    cost_usd: float | None = None  # what the run would cost on the API: a proxy for plan usage


def _tool_output_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(json.dumps(item, default=str))
    return "\n".join(parts)


def _short_name(name: str) -> str:
    return name[len(MCP_PREFIX) :] if name.startswith(MCP_PREFIX) else name


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _default_client_factory(options: Any) -> Any:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


class ClaudeRunner:
    def __init__(
        self,
        db: Database,
        *,
        mcp_url: str,
        internal_token: str,
        workspace: Path,
        max_concurrency: int = 2,
        client_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._db = db
        self._mcp_url = mcp_url
        self._internal_token = internal_token
        self._workspace = workspace
        self._slots = asyncio.Semaphore(max_concurrency)
        # Background jobs may hold at most one slot, so chat is never starved.
        self._background_slots = asyncio.Semaphore(max(1, max_concurrency - 1))
        self._client_factory = client_factory or _default_client_factory
        self._active: dict[int, tuple[Any, RunOutcome]] = {}
        self._cancel_requested: set[int] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, spec: RunSpec, emit: Emit | None = None) -> RunOutcome:
        emit = emit or (lambda _event: None)
        background = self._background_slots if spec.background else contextlib.nullcontext()
        async with background, self._slots:
            outcome = RunOutcome(run_id=self._start_row(spec))
            emit({"type": "run", "run_id": outcome.run_id})
            started = time.monotonic()
            guard = WebGuard()
            client = self._client_factory(self._options(spec, guard))
            self._active[outcome.run_id] = (client, outcome)
            try:
                await asyncio.wait_for(self._drive(client, spec, emit, outcome, guard), spec.timeout_s)
            except TimeoutError:
                outcome.status, outcome.error = "error", f"timed out after {int(spec.timeout_s)} s"
                with contextlib.suppress(Exception):
                    await client.interrupt()
            except Exception as exc:  # noqa: BLE001 — every failure is recorded, not raised
                logger.exception("Claude run %s failed", outcome.run_id)
                outcome.status, outcome.error = self._classify_exception(exc)
            finally:
                with contextlib.suppress(Exception):
                    await client.disconnect()
                self._active.pop(outcome.run_id, None)
                if outcome.run_id in self._cancel_requested:
                    self._cancel_requested.discard(outcome.run_id)
                    if outcome.status in ("running", "ok", "error"):
                        outcome.status = "cancelled"
                if outcome.status == "running":
                    outcome.status = "ok"
                outcome.duration_ms = outcome.duration_ms or int((time.monotonic() - started) * 1000)
                self._finish_row(spec, outcome)
            return outcome

    async def interrupt(self, run_id: int) -> bool:
        active = self._active.get(run_id)
        if active is None:
            return False
        self._cancel_requested.add(run_id)
        with contextlib.suppress(Exception):
            await active[0].interrupt()
        return True

    # ------------------------------------------------------------------
    # SDK plumbing
    # ------------------------------------------------------------------

    def _options(self, spec: RunSpec, guard: WebGuard) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

        policy = spec.policy
        allowed = set(policy.allowed_tools)

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any) -> Any:
            if tool_name in allowed:
                return PermissionResultAllow()
            if tool_name in policy.builtin_tools:
                reason = guard.check(tool_name, tool_input)
                if reason:
                    logger.warning("Web guard %s: %s", reason, tool_name)
                    return PermissionResultDeny(message=f"{reason}. Rephrase generically, without athlete data.")
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"{tool_name} is not available in hart.")

        return ClaudeAgentOptions(
            model=spec.model,
            system_prompt=spec.system_prompt,
            tools=policy.builtin_tools,
            allowed_tools=policy.allowed_tools,
            disallowed_tools=policy.disallowed_tools,
            can_use_tool=can_use_tool,
            permission_mode="default",
            mcp_servers={
                MCP_SERVER: {
                    "type": "http",
                    "url": self._mcp_url,
                    "headers": {"Authorization": f"Bearer {self._internal_token}"},
                }
            },
            strict_mcp_config=True,
            setting_sources=["project"],
            cwd=str(self._workspace),
            max_turns=spec.max_turns,
            resume=spec.resume,
            include_partial_messages=True,
            env={"DISABLE_AUTOUPDATER": "1"},
            output_format={"type": "json_schema", "schema": spec.output_schema} if spec.output_schema else None,
        )

    async def _drive(self, client: Any, spec: RunSpec, emit: Emit, outcome: RunOutcome, guard: WebGuard) -> None:
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        calls: dict[str, dict[str, Any]] = {}
        text_parts: list[str] = []
        error_kind: str | None = None
        rate_limited = False

        await client.connect()
        await client.query(spec.prompt)
        async for msg in client.receive_response():
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                delta = ev.get("delta") or {}
                if (
                    msg.parent_tool_use_id is None
                    and ev.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"
                ):
                    emit({"type": "text", "delta": delta.get("text", "")})
                elif (
                    msg.parent_tool_use_id is None
                    and ev.get("type") == "content_block_start"
                    and (ev.get("content_block") or {}).get("type") == "text"
                    and text_parts
                ):
                    emit({"type": "text", "delta": "\n\n"})
            elif isinstance(msg, AssistantMessage):
                if msg.error:
                    error_kind = msg.error
                if msg.parent_tool_use_id is not None:
                    continue
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        call = {
                            "id": block.id,
                            "name": _short_name(block.name),
                            "input": block.input,
                            "is_error": None,
                            "output_excerpt": None,
                            "started": time.monotonic(),
                        }
                        calls[block.id] = call
                        outcome.tool_calls.append(call)
                        emit({"type": "tool_start", "id": block.id, "name": call["name"], "input": block.input})
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        text = _tool_output_text(block.content)
                        guard.learn(text)
                        outcome.tool_outputs.append(text)
                        call = calls.get(block.tool_use_id)
                        if call is not None:
                            call["is_error"] = bool(block.is_error)
                            call["output_excerpt"] = text[:OUTPUT_EXCERPT]
                            call["ms"] = int((time.monotonic() - call.pop("started")) * 1000)
                        emit(
                            {
                                "type": "tool_end",
                                "id": block.tool_use_id,
                                "is_error": bool(block.is_error),
                                "output_excerpt": text[:OUTPUT_EXCERPT],
                            }
                        )
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "init" and msg.data.get("session_id"):
                    outcome.session_id = msg.data["session_id"]
            elif isinstance(msg, RateLimitEvent):
                if msg.rate_limit_info.status == "rejected":
                    rate_limited = True
                    outcome.resets_at = msg.rate_limit_info.resets_at
            elif isinstance(msg, ResultMessage):
                outcome.session_id = msg.session_id or outcome.session_id
                outcome.num_turns = msg.num_turns
                outcome.duration_ms = msg.duration_ms
                outcome.structured = msg.structured_output
                outcome.usage = msg.usage
                outcome.cost_usd = msg.total_cost_usd
                if (msg.terminal_reason or "").startswith("aborted"):
                    outcome.status = "cancelled"
                elif error_kind == "authentication_failed":
                    outcome.status, outcome.error = (
                        "auth_failed",
                        "Claude authentication failed — the token may have expired",
                    )
                elif rate_limited or error_kind == "rate_limit" or msg.api_error_status == 429:
                    outcome.status, outcome.error = "usage_limited", "Claude usage limit reached"
                elif msg.is_error:
                    detail = "; ".join(msg.errors or []) or (msg.result or msg.subtype)
                    outcome.status, outcome.error = "error", detail
                else:
                    outcome.status = "ok"
                if not text_parts and msg.result and not msg.is_error:
                    text_parts.append(msg.result)
        outcome.text = "\n\n".join(p.strip() for p in text_parts if p.strip())
        for call in outcome.tool_calls:
            call.pop("started", None)

    @staticmethod
    def _classify_exception(exc: Exception) -> tuple[str, str]:
        text = str(exc)
        lowered = text.lower()
        if "401" in lowered or "authentication" in lowered or "oauth" in lowered or "login" in lowered:
            return "auth_failed", f"Claude authentication failed: {text[:300]}"
        if "rate limit" in lowered or "usage limit" in lowered or "429" in lowered:
            return "usage_limited", "Claude usage limit reached"
        return "error", f"{type(exc).__name__}: {text[:500]}"

    # ------------------------------------------------------------------
    # claude_runs rows
    # ------------------------------------------------------------------

    def _start_row(self, spec: RunSpec) -> int:
        with contextlib.closing(self._db.cursor()) as cur:
            return cur.fetchone(
                "INSERT INTO claude_runs (purpose, model, prompt_version, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?) RETURNING id",
                [spec.purpose, spec.model, spec.prompt_version, _now()],
            )[0]

    def _finish_row(self, spec: RunSpec, outcome: RunOutcome) -> None:
        transcript = {
            "tool_calls": outcome.tool_calls,
            "result_excerpt": outcome.text[:4000],
            "resumed": spec.resume,
            "resets_at": outcome.resets_at,
            "usage": outcome.usage,
            "cost_usd": outcome.cost_usd,
        }
        with contextlib.closing(self._db.cursor()) as cur:
            cur.execute(
                "UPDATE claude_runs SET status = ?, error = ?, session_id = ?, num_turns = ?, duration_ms = ?, "
                "transcript = ?, finished_at = ? WHERE id = ?",
                [
                    outcome.status,
                    outcome.error,
                    outcome.session_id,
                    outcome.num_turns,
                    outcome.duration_ms,
                    json.dumps(transcript, default=str),
                    _now(),
                    outcome.run_id,
                ],
            )
