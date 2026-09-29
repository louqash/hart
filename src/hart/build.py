"""Which code is running: the package version plus the git commit it was built from.

A Docker image has no git history, so the deploy passes the commit in as a build
argument (``HART_COMMIT``, with ``HART_BUILT_AT``); a git checkout (``uv run hart
serve``) asks git directly. Shown on the System page and in ``/healthz``.
"""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path
from typing import Any

from hart import __version__

_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_ROOT), *args], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


@functools.cache
def build_info() -> dict[str, Any]:
    commit = os.environ.get("HART_COMMIT") or None
    built_at = os.environ.get("HART_BUILT_AT") or None
    if commit in (None, "unknown") and (_ROOT / ".git").exists():
        commit = _git("describe", "--always", "--dirty", "--abbrev=7")
        built_at = built_at or _git("log", "-1", "--format=%cI")
    return {"version": __version__, "commit": commit if commit != "unknown" else None, "built_at": built_at}


def describe() -> str:
    info = build_info()
    return f"{info['version']} · {info['commit']}" if info["commit"] else f"{info['version']} · commit unknown"
