"""Key/value runtime state and tunables in the ``app_settings`` table."""

from __future__ import annotations

import datetime
import json
from typing import Any

from hart.storage.database import Database

# Runtime-state keys
GARMIN_AUTH_PAUSED = "garmin_auth_paused"
GARMIN_BACKOFF_UNTIL = "garmin_backoff_until"
GARMIN_RATE_LIMIT_STREAK = "garmin_rate_limit_streak"
DEMO_MODE = "demo_mode"  # set by `hart demo`: no syncs, no Claude, no Garmin uploads


def is_demo(db: "Database") -> bool:
    return bool(get_setting(db, DEMO_MODE, False))


def get_setting(db: Database, key: str, default: Any = None) -> Any:
    row = db.fetchone("SELECT value FROM app_settings WHERE key = ?", [key])
    if row is None:
        return default
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def set_setting(db: Database, key: str, value: Any) -> None:
    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        [key, json.dumps(value, default=str), datetime.datetime.now(tz=datetime.timezone.utc)],
    )


def delete_setting(db: Database, key: str) -> None:
    db.execute("DELETE FROM app_settings WHERE key = ?", [key])


# ---------------------------------------------------------------------------
# Tunable thresholds — defaults, overridable per key
# via the "thresholds" setting.  Load-based values are provisional: the
# load is Garmin's EPOC-based score, not power/pace TSS (spec P5).
# ---------------------------------------------------------------------------

THRESHOLDS_KEY = "thresholds"

DEFAULT_THRESHOLDS: dict[str, float] = {
    # Observed training state
    "state_tsb_overreach": -30.0,
    "state_ramp_overreach": 8.0,
    "state_detrain_ctl_change": -0.20,
    "state_detrain_consistency": 4,
    "state_building_ramp": 2.0,
    "state_absorbing_ramp": -2.0,
    "state_absorbing_tsb": 0.0,
    "state_min_history_days": 28,
    # Phase fit flags
    "flag_comeback_ramp": 4.0,
    # Readiness
    "ready_hrv_red_pct": -15.0,
    "ready_hrv_amber_pct": -7.0,
    "ready_rhr_red_bpm": 7.0,
    "ready_rhr_amber_bpm": 4.0,
    "ready_sleep_red_h": 5.0,
    "ready_sleep_amber_h": 6.5,
    "ready_recovery_red": 35.0,
    "ready_recovery_amber": 55.0,
    "ready_tsb_red": -35.0,
    "ready_tsb_amber": -20.0,
    "ready_training_readiness_amber": 40.0,
    "ready_baseline_days": 28,
    "ready_baseline_min_days": 14,
    # Phase template
    "phase_taper_weeks": 2,
    "phase_peak_weeks": 3,
    "phase_build_weeks": 12,
    "phase_base_block_weeks": 4,
    "phase_transition_days": 14,
}


def get_thresholds(db: Database) -> dict[str, float]:
    overrides = get_setting(db, THRESHOLDS_KEY, {}) or {}
    return {**DEFAULT_THRESHOLDS, **{k: v for k, v in overrides.items() if k in DEFAULT_THRESHOLDS}}
