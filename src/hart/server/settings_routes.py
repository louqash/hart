"""Settings page and API: the runtime settings registry plus advanced thresholds."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from hart.analytics.health_checks import DEFAULTS as HEALTH_DEFAULTS
from hart.server import health, settings, state
from hart.server.routes import get_config, get_db, page
from hart.storage.database import Database

router = APIRouter()

# Advanced thresholds: stored as one JSON object per family, like before.
THRESHOLD_FAMILIES = {
    "training": (state.THRESHOLDS_KEY, state.DEFAULT_THRESHOLDS, "Training state, readiness and phase template"),
    "health": (health.HEALTH_SETTINGS_KEY, HEALTH_DEFAULTS, "Health-check reminders"),
}
THRESHOLD_LABELS = {
    "state_": "Training state",
    "flag_": "Phase flags",
    "ready_": "Readiness",
    "phase_": "Phase template",
    "health_": "Health",
}


class SettingIn(BaseModel):
    value: Any


class ThresholdIn(BaseModel):
    value: float | None = None  # null resets to the default


def _label(key: str) -> str:
    for prefix, group in THRESHOLD_LABELS.items():
        if key.startswith(prefix):
            return f"{group}: {key[len(prefix) :].replace('_', ' ')}"
    return key.replace("_", " ")


def thresholds(db: Database) -> list[dict[str, Any]]:
    out = []
    for family, (store, defaults, title) in THRESHOLD_FAMILIES.items():
        overrides = state.get_setting(db, store, {}) or {}
        out.append(
            {
                "family": family,
                "title": title,
                "items": [
                    {
                        "key": k,
                        "label": _label(k),
                        "default": v,
                        "value": overrides.get(k, v),
                        "overridden": k in overrides,
                    }
                    for k, v in defaults.items()
                ],
            }
        )
    return out


def server_facts(config: Any) -> list[tuple[str, str, str]]:
    """Environment-only settings, shown read-only (never secrets)."""
    s = config.server
    return [
        ("HART_DATA_DIR / database", str(config.db_path), "Where the database lives"),
        ("HART_TZ", s.tz, "Time zone for days, schedules and messages"),
        ("HART_PUBLIC_HOST", s.public_host or "—", "Public host name (for links and the same-origin check)"),
        ("HART_AUTH_HEADER", s.auth_header, "Identity header set by your reverse proxy"),
        ("HART_ALLOWED_USERS", ", ".join(s.allowed_users) or "anyone the proxy lets in", "Who may sign in"),
        ("HART_BACKUP_DIR", str(s.backup_dir) if s.backup_dir else "not set — no nightly backups", "Nightly backups"),
        ("GARMIN_EMAIL", "set" if config.garmin.email else "not set", "Garmin Connect login"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "set" if _has_claude_token() else "not set", "Claude subscription token"),
        (
            "HART_DISCORD_WEBHOOK_URL",
            "set" if config.discord.webhook_url or config.discord.bot_token else "not set",
            "Evening message",
        ),
    ]


def _has_claude_token() -> bool:
    import os

    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(request: Request, db: Database = Depends(get_db), config=Depends(get_config)) -> HTMLResponse:
    from hart.server import evening

    discord_ready = evening.configured(config)
    items = settings.describe(db)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["key"].startswith("evening_message_") and not discord_ready:
            continue  # can't be sent without Discord; the note below says how to set it up
        groups.setdefault(item["group_label"], []).append(item)
    return page(
        request,
        "settings.html",
        {
            "nav": "settings",
            "groups": groups,
            "thresholds": thresholds(db),
            "server": server_facts(config),
            "discord_ready": discord_ready,
            "notifications_group": settings.GROUPS["notifications"],
        },
    )


@router.get("/api/settings")
def api_settings(db: Database = Depends(get_db)) -> dict[str, Any]:
    return {"settings": settings.describe(db), "thresholds": thresholds(db)}


@router.put("/api/settings/{key}")
def api_set_setting(key: str, body: SettingIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        value = settings.set_value(db, key, body.value)
    except settings.SettingError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"key": key, "value": value}


@router.delete("/api/settings/{key}")
def api_reset_setting(key: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        settings.reset(db, key)
    except settings.SettingError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return {"key": key, "value": settings.get(db, key)}


@router.put("/api/thresholds/{family}/{key}")
def api_set_threshold(
    family: str, key: str, body: ThresholdIn, db: Database = Depends(get_db), config=Depends(get_config)
) -> dict[str, Any]:
    if family not in THRESHOLD_FAMILIES:
        raise HTTPException(404, detail="unknown threshold family")
    store, defaults, _ = THRESHOLD_FAMILIES[family]
    if key not in defaults:
        raise HTTPException(404, detail="unknown threshold")
    overrides = state.get_setting(db, store, {}) or {}
    if body.value is None:
        overrides.pop(key, None)
    else:
        overrides[key] = body.value
    state.set_setting(db, store, overrides)
    if family == "health":
        from hart.server.data import local_today

        health.sync_checks(db, local_today(config))  # reminders follow the new thresholds
    return {"key": key, "value": overrides.get(key, defaults[key])}
