"""Talk to a running hart server.

When ``HART_SERVER_URL`` is set, commands that have a server equivalent
(``hart sync``, ``hart status``, ``hart auth`` token upload) go through the
server instead of opening the local database file.
"""

from __future__ import annotations

import time
from typing import Any

import duckdb
import httpx
import typer
from rich.console import Console

from hart.config import HartSettings
from hart.storage.database import Database, get_database

HEADERS = {"X-Requested-With": "hart"}
TERMINAL_STATUSES = {"ok", "error", "cancelled"}


def server_url(config: HartSettings) -> str:
    return config.server.server_url


def request(config: HartSettings, method: str, path: str, json: Any = None) -> Any:
    url = f"{server_url(config)}{path}"
    try:
        resp = httpx.request(method, url, json=json, headers=HEADERS, timeout=30)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Can't reach hart server at {server_url(config)}: {exc}") from exc
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            message = resp.text
        raise RuntimeError(f"hart server returned {resp.status_code}: {message}")
    return resp.json()


def wait_for_job(config: HartSettings, job_id: int, timeout_s: float = 900) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        job = request(config, "GET", f"/api/jobs/{job_id}")
        if job["status"] in TERMINAL_STATUSES or time.monotonic() > deadline:
            return job
        time.sleep(2)


def open_local_db(config: HartSettings, console: Console) -> Database:
    """Open the local database, explaining the lock error when hart server has it."""
    try:
        return get_database(config.db_path)
    except duckdb.IOException as exc:
        if "lock" not in str(exc).lower():
            raise
        console.print(
            "[red]The database is locked by another process[/red] (hart server or a "
            "Claude session's MCP server).\n"
            "Set [cyan]HART_SERVER_URL[/cyan] to use the server, use the web UI, "
            "or stop the other process."
        )
        raise typer.Exit(2) from exc
