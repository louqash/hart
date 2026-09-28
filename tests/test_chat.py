"""Tests: Claude runner, tool policy, web guard, chat API with SSE."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from hart.config import ServerSettings, get_config
from hart.server.claude.policy import MCP_PREFIX, WebGuard, chat_policy

# ---------------------------------------------------------------------------
# Scripted stand-in for ClaudeSDKClient
# ---------------------------------------------------------------------------


def _delta(text: str) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s1", event={"type": "content_block_delta",
                                                          "delta": {"type": "text_delta", "text": text}})


def _result(**kw: Any) -> ResultMessage:
    base = dict(subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=2, session_id="s1")
    return ResultMessage(**{**base, **kw})


ANSWER = [
    SystemMessage(subtype="init", data={"session_id": "s1"}),
    AssistantMessage(content=[ToolUseBlock(id="t1", name=MCP_PREFIX + "get_training_load", input={"days": 7})],
                     model="claude-sonnet-5"),
    UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": '{"ctl": 20.1}'}])]),
    _delta("Your CTL "), _delta("is **20.1**."),
    AssistantMessage(content=[TextBlock(text="Your CTL is **20.1**.")], model="claude-sonnet-5"),
    _result(),
]


class FakeClient:
    instances: list["FakeClient"] = []
    scripts: list[list[Any]] = []

    def __init__(self, options: Any) -> None:
        self.options = options
        self.prompt: str | None = None
        self.interrupted = False
        self.script = FakeClient.scripts.pop(0) if FakeClient.scripts else list(ANSWER)
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_response(self):
        for item in self.script:
            if callable(item):
                await item(self)
                continue
            if self.interrupted:
                yield _result(terminal_reason="aborted_streaming")
                return
            yield item
            await asyncio.sleep(0)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances.clear()
    FakeClient.scripts.clear()


# ---------------------------------------------------------------------------
# Policy and web guard
# ---------------------------------------------------------------------------


def test_chat_policy() -> None:
    p = chat_policy(web=True)
    allowed = set(p.allowed_tools)
    assert MCP_PREFIX + "get_activities" in allowed and MCP_PREFIX + "propose_athlete_note" in allowed
    assert MCP_PREFIX + "send_discord_message" not in allowed
    assert MCP_PREFIX + "send_discord_message" in p.disallowed_tools
    assert "Bash" in p.disallowed_tools and "Read" in p.disallowed_tools
    assert p.builtin_tools == ["WebSearch", "WebFetch"]
    assert "WebFetch" in chat_policy(web=False).disallowed_tools


def test_web_guard() -> None:
    g = WebGuard()
    g.learn('{"ftp": 231, "ctl": 20.14, "activity_id": "fit_20260926_114110_bike", "year": 2027, "z": 5}')
    assert g.check("WebSearch", {"query": "Ironman 2027 bike course"}) is None
    assert g.check("WebSearch", {"query": "ftp 231 pacing for ironman"})
    assert g.check("WebSearch", {"query": "is CTL 20.14 low"})
    assert g.check("WebFetch", {"url": "https://x.example/?id=fit_20260926_114110_bike"})
    assert g.check("WebFetch", {"url": "https://x.example/?d=" + "a" * 300})
    assert g.check("WebSearch", {"query": "zone 5 intervals"}) is None  # 1-digit numbers are fine


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    config = dataclasses.replace(
        get_config(), db_path=tmp_path / "app.duckdb",
        server=ServerSettings(env="production", seed_dir=tmp_path / "seed"),
    )
    app = create_app(config, run_scheduler=False, claude_client_factory=FakeClient,
                     handlers={"sync": lambda d, p: {}, "sync_light": lambda d, p: {}})
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


H = {"Tailscale-User-Login": "me@example.com"}
W = {**H, "X-Requested-With": "hart"}


def _events(client, run_id: str, after: int = -1) -> list[dict[str, Any]]:
    events = []
    with client.stream("GET", f"/api/chat/runs/{run_id}/stream?after={after}", headers=H) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event: end"):
                break
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_chat_turn_streams_and_saves(client) -> None:
    conv = client.post("/api/chat", headers=W, json={"model": "claude-sonnet-5"}).json()["id"]
    run = client.post(f"/api/chat/{conv}/messages", headers=W, json={"text": "What's my CTL?"}).json()["run_id"]
    events = _events(client, run)
    types = [e["type"] for e in events]
    assert types[0] == "run" and "tool_start" in types and "tool_end" in types and types[-1] == "done"
    assert "".join(e["delta"] for e in events if e["type"] == "text") == "Your CTL is **20.1**."
    done = events[-1]
    assert done["status"] == "ok" and "<strong>20.1</strong>" in done["content_html"]

    # Options passed to the SDK: model, loopback MCP with the internal token, no built-in shell/files.
    opts = FakeClient.instances[0].options
    assert opts.model == "claude-sonnet-5"
    assert opts.mcp_servers["hart"]["headers"]["Authorization"].startswith("Bearer ")
    assert "Bash" in opts.disallowed_tools and opts.resume is None

    saved = client.get(f"/api/chat/{conv}", headers=H).json()
    assert saved["title"] == "What's my CTL?" and saved["claude_session_id"] == "s1"
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][1]["tool_calls"][0]["name"] == "get_training_load"

    # Replay: a reconnecting client gets only events after its last id.
    assert _events(client, run, after=len(events) - 2)[-1]["type"] == "done"

    # Follow-up resumes the Claude Code session.
    run2 = client.post(f"/api/chat/{conv}/messages", headers=W, json={"text": "And ATL?"}).json()["run_id"]
    _events(client, run2)
    assert FakeClient.instances[1].options.resume == "s1"
    assert FakeClient.instances[1].prompt == "And ATL?"


def test_usage_limit_is_reported(client) -> None:
    FakeClient.scripts.append([
        RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=1790500000), uuid="u", session_id="s"),
        _result(is_error=True, api_error_status=429, session_id="s"),
    ])
    conv = client.post("/api/chat", headers=W, json={}).json()["id"]
    run = client.post(f"/api/chat/{conv}/messages", headers=W, json={"text": "hi"}).json()["run_id"]
    done = _events(client, run)[-1]
    assert done["status"] == "usage_limited" and done["resets_at"] == 1790500000
    msg = client.get(f"/api/chat/{conv}", headers=H).json()["messages"][1]["content"]
    assert "usage limit" in msg.lower()
    system = client.get("/api/system", headers=H).json()
    assert system["claude"]["problem"]["status"] == "usage_limited"


def test_context_prefixes_first_prompt(client) -> None:
    conv = client.post("/api/chat", headers=W, json={"context": "readiness:2026-09-26"}).json()["id"]
    run = client.post(f"/api/chat/{conv}/messages", headers=W, json={"text": "Should I train?"}).json()["run_id"]
    _events(client, run)
    assert FakeClient.instances[0].prompt.startswith("(Context the athlete opened this chat from: today's readiness")


def test_permission_callback_and_busy_conversation(client) -> None:
    import threading

    gate = threading.Event()
    decisions: list[Any] = []

    async def probe(fake: FakeClient) -> None:
        cb = fake.options.can_use_tool
        decisions.append(await cb(MCP_PREFIX + "get_activities", {}, None))
        decisions.append(await cb(MCP_PREFIX + "send_discord_message", {}, None))
        decisions.append(await cb("Bash", {"command": "ls"}, None))
        decisions.append(await cb("WebSearch", {"query": "ironman swim course"}, None))

    async def wait(_fake: FakeClient) -> None:
        for _ in range(500):  # the app loop runs in another thread
            if gate.is_set():
                return
            await asyncio.sleep(0.01)

    FakeClient.scripts.append([probe, ANSWER[1], ANSWER[2], wait, *ANSWER[3:]])
    conv = client.post("/api/chat", headers=W, json={}).json()["id"]
    run = client.post(f"/api/chat/{conv}/messages", headers=W, json={"text": "hi"}).json()["run_id"]
    busy = client.post(f"/api/chat/{conv}/messages", headers=W, json={"text": "again"})
    assert busy.status_code == 409
    gate.set()
    _events(client, run)
    assert [type(d) for d in decisions] == [PermissionResultAllow, PermissionResultDeny, PermissionResultDeny,
                                            PermissionResultAllow]


def test_chat_page_renders(client) -> None:
    assert client.get("/chat", headers=H).status_code == 200
    conv = client.post("/api/chat", headers=W, json={}).json()["id"]
    assert client.get(f"/chat/{conv}", headers=H).status_code == 200
    assert client.get("/chat/nope", headers=H).status_code == 404


def test_lab_results_import_and_tool(tmp_path: Path) -> None:
    from hart.server.labs import import_lab_results, parse_range, parse_value
    from hart.storage.database import Database

    assert parse_value("<8.00") == (8.0, "<") and parse_value("15,5") == (15.5, None)
    assert parse_range("13.5 - 18.0") == (13.5, 18.0) and parse_range(None) == (None, None)
    src = tmp_path / "blood.json"
    src.write_text(json.dumps([{"date": "2026-05-07", "markers": [
        {"name": "Ferrytyna", "value": "120", "unit": "ng/ml", "reference_range": "30 - 400", "flag": None},
        {"name": "TSH", "value": "4.1", "unit": "uIU/ml", "reference_range": "0.27 - 4.2", "flag": "H"}]}]))
    db = Database(tmp_path / "t.duckdb").connect()
    assert import_lab_results(db, src) == 2
    assert import_lab_results(db, src) == 0  # idempotent
    assert db.fetchone("SELECT marker_key, value_num, ref_high FROM lab_results WHERE marker_name = 'TSH'") == ("tsh", 4.1, 4.2)
    db.close()


def test_usage_is_recorded_and_summarised(tmp_path) -> None:
    import datetime as _dt

    from hart.server.data import claude_usage
    from hart.storage.database import Database

    db = Database(tmp_path / "u.duckdb").connect()
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    for purpose, status, usage in (("chat", "ok", {"input_tokens": 100, "output_tokens": 50}),
                                   ("grade", "usage_limited", None), ("chat", "ok", {"output_tokens": 10})):
        db.execute("INSERT INTO claude_runs (purpose, model, prompt_version, status, duration_ms, transcript, started_at, "
                   "finished_at) VALUES (?, 'claude-opus-5-5', 'x', ?, 60000, ?, ?, ?)",
                   [purpose, status, json.dumps({"usage": usage, "resets_at": 123}), now, now])
    u = claude_usage(db, _dt.date.today())
    chat = next(p for p in u["purposes"] if p["purpose"] == "chat")
    assert (chat["runs"], chat["tokens"], chat["minutes"]) == (2, 160, 2.0)
    assert next(p for p in u["purposes"] if p["purpose"] == "grade")["failed"] == 1
    assert u["per_day"][-1]["total"] == 3 and u["last_limit"] is not None
    db.close()
