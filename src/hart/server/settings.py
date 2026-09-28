"""Runtime settings: one registry for everything that's reasonable to change.

Each setting has a default, may come from an environment variable, and —
when ``ui`` is true — can be overridden on the Settings page.  Resolution
order: Settings-page override (stored in ``app_settings``) → environment
variable → default.  Infrastructure (paths, ports, credentials, the identity
header) stays environment-only; see docs/configuration.md.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from typing import Any

from hart.server import state
from hart.storage.database import Database

OVERRIDE_PREFIX = "setting:"

MODELS = {
    "claude-opus-5-5": "Opus 5.5 — best answers, uses the most of your plan",
    "claude-sonnet-5": "Sonnet 5 — strong and lighter on usage",
    "claude-haiku-4-5": "Haiku 4.5 — fast and cheapest, for simple parsing",
}


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    label: str
    default: Any
    kind: str = "str"  # str | int | bool | time | choice
    env: str | None = None  # HART_… variable that sets the default
    help: str = ""
    choices: dict[str, str] | None = None
    ui: bool = True
    min: float | None = None
    max: float | None = None


GROUPS = {
    "athlete": "Athlete",
    "ember": "Ember (Claude)",
    "schedule": "Schedule",
    "notifications": "Notifications & integrations",
}

REGISTRY: tuple[Setting, ...] = (
    # --- athlete -----------------------------------------------------------
    Setting("athlete_name", "athlete", "Your name", "Athlete", env="HART_ATHLETE_NAME",
            help="How Ember addresses you. Everything else about you lives in your notes."),
    Setting("athlete_max_hr", "athlete", "Max heart rate", 190, "int", env="HART_ATHLETE_MAX_HR", min=120, max=230,
            help="Part of the profile Ember reads; heart-rate zones come from Garmin."),
    Setting("power_single_sided", "athlete", "Single-sided power meter", False, "bool", env="HART_POWER_SINGLE_SIDED",
            help="Left-only pedals or cranks double one leg: Ember compares power trends, not absolute watts."),
    Setting("coach_platform", "athlete", "Coach's platform", "", env="HART_COACH_PLATFORM",
            help="Where your coach plans, e.g. TrainingPeaks. Leave empty if you don't have a coach — "
                 "suggestions then plan every day freely."),
    # --- ember -------------------------------------------------------------
    Setting("model_chat", "ember", "Model for chat (default)", "claude-opus-5-5", "choice", env="HART_MODEL_CHAT",
            choices=MODELS, help="Can be switched per conversation."),
    Setting("model_suggest", "ember", "Model for daily suggestions", "claude-opus-5-5", "choice",
            env="HART_MODEL_SUGGEST", choices=MODELS),
    Setting("model_grade", "ember", "Model for session grading", "claude-sonnet-5", "choice", env="HART_MODEL_GRADE",
            choices=MODELS, help="Also converts sessions to Garmin workouts."),
    Setting("model_parse", "ember", "Model for reading pasted plans and lab reports", "claude-haiku-4-5", "choice",
            env="HART_MODEL_PARSE", choices=MODELS),
    # --- schedule ----------------------------------------------------------
    Setting("morning_watch_start", "schedule", "Morning sleep check from", "05:30", "time", env="HART_MORNING_FROM",
            help="Light syncs every 15 min until last night's sleep arrives."),
    Setting("morning_watch_end", "schedule", "…until", "10:30", "time", env="HART_MORNING_UNTIL",
            help="Also when today's suggestion is made at the latest."),
    Setting("hourly_first", "schedule", "Hourly sync from (hour)", 6, "int", env="HART_HOURLY_FROM", min=0, max=23),
    Setting("hourly_last", "schedule", "Hourly sync until (hour)", 23, "int", env="HART_HOURLY_UNTIL", min=0, max=23),
    Setting("preliminary_time", "schedule", "Tomorrow's suggestion at", "20:00", "time", env="HART_PRELIMINARY_AT",
            help="From this time on, the dashboard shows tomorrow instead of today."),
    Setting("backup_time", "schedule", "Nightly backup at", "03:00", "time", env="HART_BACKUP_AT"),
    # --- notifications -----------------------------------------------------
    Setting("evening_message_enabled", "notifications", "Evening Discord message", True, "bool",
            env="HART_EVENING_MESSAGE", help="Needs HART_DISCORD_WEBHOOK_URL."),
    Setting("evening_message_time", "notifications", "Evening message at", "22:00", "time",
            env="HART_EVENING_MESSAGE_AT"),
    Setting("garmin_auto_send", "notifications", "Send accepted suggestions to Garmin", True, "bool",
            env="HART_GARMIN_AUTO_SEND", help="Runs and rides are scheduled on your Garmin calendar."),
)
BY_KEY = {s.key: s for s in REGISTRY}

# Keys used before the registry existed (kept so existing choices survive).
LEGACY_KEYS = {"evening_message_enabled": "evening_message_enabled", "garmin_auto_send": "garmin_auto_send"}


class SettingError(ValueError):
    pass


def parse(setting: Setting, raw: Any) -> Any:
    """Validate and convert a value (from the env, JSON or a form field)."""
    if setting.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise SettingError(f"{setting.label}: expected yes/no")
    if setting.kind == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise SettingError(f"{setting.label}: expected a whole number") from exc
        if (setting.min is not None and value < setting.min) or (setting.max is not None and value > setting.max):
            raise SettingError(f"{setting.label}: must be between {setting.min:g} and {setting.max:g}")
        return value
    if setting.kind == "time":
        try:
            hour, minute = (int(part) for part in str(raw).strip().split(":")[:2])
            return datetime.time(hour, minute).strftime("%H:%M")
        except ValueError as exc:
            raise SettingError(f"{setting.label}: expected a time like 20:00") from exc
    if setting.kind == "choice":
        if raw not in (setting.choices or {}):
            raise SettingError(f"{setting.label}: pick one of {', '.join(setting.choices or {})}")
        return raw
    return str(raw).strip()


def _env_value(setting: Setting) -> Any:
    if not setting.env:
        return None
    raw = os.environ.get(setting.env)
    if raw in (None, "") and setting.env.startswith("HART_"):
        raw = os.environ.get("TRI_" + setting.env[5:])  # pre-rename name
    if raw in (None, ""):
        return None
    try:
        return parse(setting, raw)
    except SettingError:
        return None


def get(db: Database, key: str) -> Any:
    setting = BY_KEY[key]
    if setting.ui:
        override = state.get_setting(db, OVERRIDE_PREFIX + key)
        if override is None and key in LEGACY_KEYS:
            override = state.get_setting(db, LEGACY_KEYS[key])
        if override is not None:
            return override
    env = _env_value(setting)
    return setting.default if env is None else env


def get_time(db: Database, key: str) -> datetime.time:
    return datetime.time.fromisoformat(get(db, key))


def set_value(db: Database, key: str, raw: Any) -> Any:
    setting = BY_KEY.get(key)
    if setting is None or not setting.ui:
        raise SettingError(f"{key} can't be changed here")
    value = parse(setting, raw)
    state.set_setting(db, OVERRIDE_PREFIX + key, value)
    if key in LEGACY_KEYS:
        state.delete_setting(db, LEGACY_KEYS[key])
    return value


def reset(db: Database, key: str) -> None:
    if key not in BY_KEY:
        raise SettingError(f"unknown setting {key}")
    state.delete_setting(db, OVERRIDE_PREFIX + key)
    if key in LEGACY_KEYS:
        state.delete_setting(db, LEGACY_KEYS[key])


def describe(db: Database) -> list[dict[str, Any]]:
    """Every UI setting with its value and where the value comes from."""
    out = []
    for s in REGISTRY:
        if not s.ui:
            continue
        override = state.get_setting(db, OVERRIDE_PREFIX + s.key)
        if override is None and s.key in LEGACY_KEYS:
            override = state.get_setting(db, LEGACY_KEYS[s.key])
        env = _env_value(s)
        source = "settings" if override is not None else "env" if env is not None else "default"
        out.append({
            "key": s.key, "group": s.group, "group_label": GROUPS[s.group], "label": s.label, "help": s.help,
            "kind": s.kind, "choices": s.choices, "min": s.min, "max": s.max, "env": s.env,
            "value": get(db, s.key), "default": s.default if env is None else env, "source": source,
        })
    return out
