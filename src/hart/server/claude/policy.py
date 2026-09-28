"""What each kind of Claude run may use.

Claude runs without shell or file access: the only built-in tools ever
enabled are WebSearch/WebFetch (chat only), everything else goes through
the hart MCP tools.  The web tools pass through a guard that blocks
requests which look like they carry the athlete's data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MCP_SERVER = "hart"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"

# MCP tools that change state or reach outside; everything else is read-only.
WRITE_TOOLS = frozenset(
    {
        "sync_garmin",
        "sync_all",
        "propose_athlete_note",
        "propose_season_change",
        "propose_health_check",
        "propose_plan_change",
        "acknowledge_anomaly",
        "send_discord_message",
    }
)
# Write tools chat may use: sync (idempotent, enqueues a job) and proposals
# for notes and season changes (nothing changes until the athlete approves).
CHAT_WRITE_TOOLS = frozenset(
    {
        "sync_garmin",
        "sync_all",
        "propose_athlete_note",
        "propose_season_change",
        "propose_health_check",
        "propose_plan_change",
    }
)

# Built-in Claude Code tools that must never be available.
DENIED_BUILTINS = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "Task",
    "Agent",
    "TodoWrite",
    "ExitPlanMode",
    "Skill",
)
WEB_TOOLS = ("WebSearch", "WebFetch")


def mcp_tool_names() -> list[str]:
    from hart import mcp_server

    return sorted(t.name for t in mcp_server.mcp._tool_manager.list_tools())


@dataclass(frozen=True)
class ToolPolicy:
    allowed_mcp: frozenset[str]
    web: bool

    @property
    def allowed_tools(self) -> list[str]:
        """Auto-approved without the permission callback."""
        return sorted(MCP_PREFIX + name for name in self.allowed_mcp)

    @property
    def builtin_tools(self) -> list[str]:
        """Built-in tools that exist at all in the session."""
        return list(WEB_TOOLS) if self.web else []

    @property
    def disallowed_tools(self) -> list[str]:
        hidden = [MCP_PREFIX + n for n in mcp_tool_names() if n not in self.allowed_mcp]
        return [*DENIED_BUILTINS, *hidden, *([] if self.web else WEB_TOOLS)]


def chat_policy(web: bool) -> ToolPolicy:
    read = {n for n in mcp_tool_names() if n not in WRITE_TOOLS}
    return ToolPolicy(allowed_mcp=frozenset(read | CHAT_WRITE_TOOLS), web=web)


def read_only_policy() -> ToolPolicy:
    return ToolPolicy(allowed_mcp=frozenset(n for n in mcp_tool_names() if n not in WRITE_TOOLS), web=False)


# ---------------------------------------------------------------------------
# Web guard
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?(?![\w])")
_ACTIVITY_ID = re.compile(r"\bfit_\d{8}_\d{6}_\w+|\b\d{9,}\b")
MAX_QUERY_PARAM = 200


def _significant_digits(token: str) -> int:
    digits = token.lstrip("-").replace(",", ".").replace(".", "").lstrip("0")
    return len(digits)


def _is_year(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2100


@dataclass
class WebGuard:
    """Blocks web requests that look like they contain athlete data.

    A tripwire, not a guarantee — the system prompt's non-disclosure rule is
    the main defence.  It learns the numbers and IDs returned by the
    hart tools during the run and refuses WebSearch queries / WebFetch
    URLs that contain any of them (years excepted), or very long query
    parameters.
    """

    seen_numbers: set[str] = field(default_factory=set)
    seen_ids: set[str] = field(default_factory=set)

    def learn(self, text: str) -> None:
        for token in _NUMBER.findall(text):
            if _significant_digits(token) >= 3 and not _is_year(token):
                self.seen_numbers.add(token.lstrip("-").replace(",", "."))
        self.seen_ids.update(_ACTIVITY_ID.findall(text))

    def check(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        """Reason to block, or None if the request may go out."""
        if tool == "WebSearch":
            text = str(tool_input.get("query", ""))
        else:
            text = str(tool_input.get("url", ""))
            for _key, value in parse_qsl(urlsplit(text).query, keep_blank_values=True):
                if len(value) > MAX_QUERY_PARAM:
                    return "blocked: a URL parameter is too long to be a normal lookup"
        for found in _ACTIVITY_ID.findall(text):
            if found in self.seen_ids:
                return "blocked: request contained an activity ID"
        for token in _NUMBER.findall(text):
            normalized = token.lstrip("-").replace(",", ".")
            if normalized in self.seen_numbers and not _is_year(token):
                return "blocked: request looked like it contained athlete data"
        return None
