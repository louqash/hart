"""DDL definitions and schema initialisation for the hart DuckDB database.

Every table uses ``CREATE TABLE IF NOT EXISTS`` so the function is safe to call
on every connection open.  A ``schema_version`` metadata table tracks applied
migrations so future releases can upgrade the schema incrementally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hart.storage.database import Database

# Bump this when adding migrations.
CURRENT_SCHEMA_VERSION: int = 15

# ---------------------------------------------------------------------------
# DDL statements
# ---------------------------------------------------------------------------

_RAW_TABLES: str = """
-- ===== Raw layer =====

CREATE TABLE IF NOT EXISTS activities (
    activity_id   VARCHAR PRIMARY KEY,
    source        VARCHAR NOT NULL,
    external_id   VARCHAR,
    sport_type    VARCHAR NOT NULL,
    sub_type      VARCHAR,
    name          VARCHAR,
    description   VARCHAR,
    start_time    TIMESTAMP NOT NULL,
    elapsed_seconds     INT NOT NULL,
    moving_seconds      INT,
    distance_meters     DOUBLE,
    total_elevation_m   DOUBLE,
    avg_hr              DOUBLE,
    max_hr              DOUBLE,
    avg_power           DOUBLE,
    max_power           DOUBLE,
    normalized_power    DOUBLE,
    avg_cadence         DOUBLE,
    avg_pace_sec_km     DOUBLE,
    avg_speed_kmh       DOUBLE,
    calories            INT,
    avg_temperature     DOUBLE,
    training_effect_aerobic   DOUBLE,
    training_effect_anaerobic DOUBLE,
    fit_file_path       VARCHAR,
    gear_id             VARCHAR,
    weather_summary     VARCHAR,
    rpe                 INT,
    feel                INT,
    garmin_training_load DOUBLE,
    training_effect_label VARCHAR,
    body_battery_delta  INT,
    begin_stamina       DOUBLE,
    end_stamina         DOUBLE,
    imported_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS activity_streams (
    activity_id           VARCHAR NOT NULL,
    timestamp_sec         INT NOT NULL,
    heart_rate            SMALLINT,
    power                 SMALLINT,
    cadence               SMALLINT,
    speed                 DOUBLE,
    altitude              DOUBLE,
    distance              DOUBLE,
    latitude              DOUBLE,
    longitude             DOUBLE,
    temperature           DOUBLE,
    grade_percent         DOUBLE,
    ground_contact_time_ms  SMALLINT,
    vertical_oscillation_mm SMALLINT,
    vertical_ratio_pct    DOUBLE,
    stride_length_m       DOUBLE,
    respiration_rate      DOUBLE,
    PRIMARY KEY (activity_id, timestamp_sec)
);

CREATE TABLE IF NOT EXISTS activity_laps (
    activity_id       VARCHAR NOT NULL,
    lap_index         INT NOT NULL,
    start_time        TIMESTAMP,
    elapsed_seconds   INT NOT NULL,
    moving_seconds    INT,
    distance_meters   DOUBLE,
    avg_hr            DOUBLE,
    max_hr            DOUBLE,
    avg_power         DOUBLE,
    avg_cadence       DOUBLE,
    avg_pace_sec_km   DOUBLE,
    total_elevation_m DOUBLE,
    avg_temperature   DOUBLE,
    PRIMARY KEY (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS activity_workout_steps (
    activity_id     VARCHAR NOT NULL,
    step_index      INT NOT NULL,
    name            VARCHAR,
    intensity       VARCHAR,
    duration_type   VARCHAR,
    duration_value  DOUBLE,
    target_type     VARCHAR,
    target_low      DOUBLE,
    target_high     DOUBLE,
    target_unit     VARCHAR,
    repeat_from     INT,
    repeat_count    INT,
    notes           VARCHAR,
    PRIMARY KEY (activity_id, step_index)
);

CREATE TABLE IF NOT EXISTS strength_sets (
    activity_id         VARCHAR NOT NULL,
    set_index           INT NOT NULL,
    set_type            VARCHAR NOT NULL,
    start_time          TIMESTAMP,
    duration_sec        DOUBLE,
    repetitions         INT,
    weight_kg           DOUBLE,
    exercise_category   VARCHAR,
    exercise_name       VARCHAR,
    exercise_confidence DOUBLE,
    PRIMARY KEY (activity_id, set_index)
);

CREATE TABLE IF NOT EXISTS hrv_samples (
    activity_id    VARCHAR NOT NULL,
    sample_index   INT NOT NULL,
    rr_interval_ms DOUBLE NOT NULL,
    PRIMARY KEY (activity_id, sample_index)
);

CREATE TABLE IF NOT EXISTS daily_health (
    date               DATE PRIMARY KEY,
    resting_hr         DOUBLE,
    min_hr             DOUBLE,
    max_hr_day         DOUBLE,
    hr_p25             DOUBLE,
    hr_p50             DOUBLE,
    hr_p75             DOUBLE,
    avg_stress         DOUBLE,
    max_stress         DOUBLE,
    stress_p50         DOUBLE,
    stress_p75         DOUBLE,
    stress_p90         DOUBLE,
    stress_p95         DOUBLE,
    high_stress_duration  INT,
    medium_stress_duration INT,
    low_stress_duration   INT,
    rest_stress_duration  INT,
    body_battery_high  INT,
    body_battery_low   INT,
    body_battery_start INT,
    training_readiness INT,
    vo2max_run         DOUBLE,
    vo2max_cycle       DOUBLE,
    respiration_avg    DOUBLE,
    respiration_min    DOUBLE,
    respiration_max    DOUBLE,
    steps              INT,
    active_calories    INT,
    total_calories     INT
);

CREATE TABLE IF NOT EXISTS sleep_records (
    date             DATE PRIMARY KEY,
    sleep_start      TIMESTAMP,
    sleep_end        TIMESTAMP,
    total_sleep_sec  INT,
    deep_sleep_sec   INT,
    light_sleep_sec  INT,
    rem_sleep_sec    INT,
    awake_sec        INT,
    sleep_score      INT,
    avg_spo2         DOUBLE,
    avg_respiration  DOUBLE,
    avg_hr_sleep     DOUBLE,
    hrv_status       VARCHAR,
    hrv_overnight_ms DOUBLE
);

CREATE TABLE IF NOT EXISTS hrv_daily (
    date                     DATE PRIMARY KEY,
    hrv_weekly_avg_ms        DOUBLE,
    hrv_last_night_ms        DOUBLE,
    hrv_last_night_5min_high DOUBLE,
    hrv_status               VARCHAR,
    baseline_low_ms          DOUBLE,
    baseline_high_ms         DOUBLE
);

"""

_COMPUTED_TABLES: str = """
-- ===== Computed layer =====

CREATE TABLE IF NOT EXISTS activity_metrics (
    activity_id            VARCHAR PRIMARY KEY,
    sport_type             VARCHAR NOT NULL,
    date                   DATE NOT NULL,
    tss                    DOUBLE,
    tss_method             VARCHAR,
    hr_zone_seconds        JSON,
    efficiency_factor      DOUBLE,
    aerobic_decoupling_pct DOUBLE,
    avg_ground_contact_ms  DOUBLE,
    avg_vertical_osc_mm    DOUBLE,
    avg_vertical_ratio_pct DOUBLE,
    avg_stride_length_m    DOUBLE,
    swolf                  DOUBLE,
    estimated_calories     INT,
    estimated_kj           DOUBLE,
    carb_calories          DOUBLE,
    fat_calories           DOUBLE
);

CREATE TABLE IF NOT EXISTS daily_training_load (
    date        DATE NOT NULL,
    sport_type  VARCHAR NOT NULL,
    daily_tss   DOUBLE,
    ctl         DOUBLE,
    atl         DOUBLE,
    tsb         DOUBLE,
    monotony    DOUBLE,
    strain      DOUBLE,
    PRIMARY KEY (date, sport_type)
);

CREATE TABLE IF NOT EXISTS weekly_summary (
    week_start              DATE NOT NULL,
    sport_type              VARCHAR NOT NULL,
    session_count           INT DEFAULT 0,
    total_duration_sec      INT DEFAULT 0,
    total_distance_m        DOUBLE DEFAULT 0,
    total_tss               DOUBLE DEFAULT 0,
    total_elevation_m       DOUBLE DEFAULT 0,
    avg_hr                  DOUBLE,
    avg_ef                  DOUBLE,
    avg_decoupling          DOUBLE,
    hr_zone_seconds         JSON,
    longest_session_sec     INT,
    PRIMARY KEY (week_start, sport_type)
);

CREATE TABLE IF NOT EXISTS daily_recovery (
    date                    DATE PRIMARY KEY,
    recovery_score          DOUBLE NOT NULL,
    hrv_component           DOUBLE,
    sleep_component         DOUBLE,
    body_battery_component  DOUBLE,
    readiness_component     DOUBLE,
    stress_component        DOUBLE,
    fatigue_component       DOUBLE,
    notes                   VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS anomaly_id_seq START 1;

CREATE TABLE IF NOT EXISTS anomaly_log (
    id                INTEGER PRIMARY KEY DEFAULT nextval('anomaly_id_seq'),
    detected_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    anomaly_type      VARCHAR NOT NULL,
    severity          VARCHAR NOT NULL,
    sport_type        VARCHAR,
    metric_name       VARCHAR NOT NULL,
    expected_value    DOUBLE,
    actual_value      DOUBLE,
    z_score           DOUBLE,
    description       VARCHAR,
    activity_id       VARCHAR,
    date_range_start  DATE,
    date_range_end    DATE,
    acknowledged      BOOLEAN DEFAULT FALSE
);
"""

_METADATA_TABLES: str = """
-- ===== Metadata =====

CREATE TABLE IF NOT EXISTS sync_state (
    source              VARCHAR PRIMARY KEY,
    last_sync_at        TIMESTAMP,
    last_activity_time  TIMESTAMP,
    metadata            JSON
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_APP_TABLES: str = """
-- ===== Web service (hart server) =====

CREATE SEQUENCE IF NOT EXISTS phase_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS race_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS planned_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS note_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS annotation_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS grade_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS suggestion_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS chat_msg_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS claude_run_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS job_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS lab_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS proposal_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS health_check_id_seq START 1;

CREATE TABLE IF NOT EXISTS training_phases (
    id          INTEGER PRIMARY KEY DEFAULT nextval('phase_id_seq'),
    phase_type  VARCHAR NOT NULL,
    name        VARCHAR NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    goal        VARCHAR,
    source      VARCHAR NOT NULL,
    confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT current_timestamp,
    updated_at  TIMESTAMPTZ DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS races (
    id         INTEGER PRIMARY KEY DEFAULT nextval('race_id_seq'),
    name       VARCHAR NOT NULL,
    race_date  DATE NOT NULL,
    distance   VARCHAR NOT NULL,
    priority   VARCHAR NOT NULL,
    notes      VARCHAR
);

CREATE TABLE IF NOT EXISTS planned_sessions (
    id             INTEGER PRIMARY KEY DEFAULT nextval('planned_id_seq'),
    date           DATE NOT NULL,
    sport_type     VARCHAR NOT NULL,
    title          VARCHAR NOT NULL,
    description    VARCHAR,
    duration_min   INTEGER,
    intensity      VARCHAR,
    source         VARCHAR NOT NULL,
    replaces_id    INTEGER,
    activity_id    VARCHAR,
    match_method   VARCHAR,
    suggestion_id  INTEGER,
    garmin_text    VARCHAR,
    garmin_steps       JSON,          -- structured steps sent to Garmin (schema v11)
    garmin_workout_id  VARCHAR,       -- Garmin Connect workout / calendar entry
    garmin_schedule_id VARCHAR,
    garmin_status      VARCHAR,       -- sent | failed | null
    garmin_error       VARCHAR,
    garmin_sent_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS athlete_notes (
    id          INTEGER PRIMARY KEY DEFAULT nextval('note_id_seq'),
    category    VARCHAR NOT NULL,
    title       VARCHAR NOT NULL,
    body        VARCHAR NOT NULL,
    valid_from  DATE,
    valid_to    DATE,
    rules       JSON,
    status      VARCHAR NOT NULL,
    source      VARCHAR NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT current_timestamp,
    updated_at  TIMESTAMPTZ DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY DEFAULT nextval('annotation_id_seq'),
    kind        VARCHAR NOT NULL,
    label       VARCHAR NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE,
    source      VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS session_feedback (
    activity_id  VARCHAR PRIMARY KEY,
    rpe          INTEGER,
    feel         INTEGER,
    comment      VARCHAR,
    updated_at   TIMESTAMPTZ DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS session_grades (
    id                 INTEGER PRIMARY KEY DEFAULT nextval('grade_id_seq'),
    activity_id        VARCHAR NOT NULL,
    version            INTEGER NOT NULL,
    status             VARCHAR NOT NULL,
    ungraded_reason    VARCHAR,
    planned_session_id INTEGER,
    intent_source      VARCHAR,
    session_type       VARCHAR,
    score_execution    INTEGER,
    score_response     INTEGER,
    score_context      INTEGER,
    overall_score      DOUBLE,
    letter             VARCHAR,
    confidence         VARCHAR,
    summary            VARCHAR,
    highlights         JSON,
    concerns           JSON,
    citations          JSON,
    features           JSON,
    claude_run_id      INTEGER,
    created_at         TIMESTAMPTZ DEFAULT current_timestamp,
    UNIQUE (activity_id, version)
);

CREATE TABLE IF NOT EXISTS daily_suggestions (
    id               INTEGER PRIMARY KEY DEFAULT nextval('suggestion_id_seq'),
    for_date         DATE NOT NULL,
    version          INTEGER NOT NULL,
    kind             VARCHAR NOT NULL,
    readiness        VARCHAR NOT NULL,
    readiness_detail JSON,
    recommendation   VARCHAR,
    sessions         JSON,
    cautions         JSON,
    summary          VARCHAR,
    citations        JSON,
    status           VARCHAR NOT NULL,
    error            VARCHAR,
    context          JSON,
    claude_run_id    INTEGER,
    created_at       TIMESTAMPTZ DEFAULT current_timestamp,
    UNIQUE (for_date, version)
);

CREATE TABLE IF NOT EXISTS chat_conversations (
    id                 VARCHAR PRIMARY KEY,
    title              VARCHAR,
    claude_session_id  VARCHAR,
    context_ref        JSON,
    model              VARCHAR NOT NULL,
    archived           BOOLEAN DEFAULT FALSE,
    created_at         TIMESTAMPTZ DEFAULT current_timestamp,
    updated_at         TIMESTAMPTZ DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id               INTEGER PRIMARY KEY DEFAULT nextval('chat_msg_id_seq'),
    conversation_id  VARCHAR NOT NULL,
    role             VARCHAR NOT NULL,
    content          VARCHAR NOT NULL,
    tool_calls       JSON,
    claude_run_id    INTEGER,
    created_at       TIMESTAMPTZ DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS claude_runs (
    id              INTEGER PRIMARY KEY DEFAULT nextval('claude_run_id_seq'),
    purpose         VARCHAR NOT NULL,
    model           VARCHAR NOT NULL,
    prompt_version  VARCHAR NOT NULL,
    session_id      VARCHAR,
    status          VARCHAR NOT NULL,
    error           VARCHAR,
    num_turns       INTEGER,
    duration_ms     INTEGER,
    transcript      JSON,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY DEFAULT nextval('job_id_seq'),
    type         VARCHAR NOT NULL,
    payload      JSON,
    status       VARCHAR NOT NULL,
    attempts     INTEGER DEFAULT 0,
    error        VARCHAR,
    result       JSON,
    trigger      VARCHAR NOT NULL,
    dedupe_key   VARCHAR,
    created_at   TIMESTAMPTZ DEFAULT current_timestamp,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS lab_results (
    id           INTEGER PRIMARY KEY DEFAULT nextval('lab_id_seq'),
    test_date    DATE NOT NULL,
    marker_name  VARCHAR NOT NULL,
    marker_key   VARCHAR,
    value_text   VARCHAR NOT NULL,
    value_num    DOUBLE,
    qualifier    VARCHAR,
    unit         VARCHAR,
    ref_low      DOUBLE,
    ref_high     DOUBLE,
    ref_text     VARCHAR,
    flag         VARCHAR,
    source       VARCHAR NOT NULL,
    UNIQUE (test_date, marker_name)
);

CREATE TABLE IF NOT EXISTS health_checks (
    id             INTEGER PRIMARY KEY DEFAULT nextval('health_check_id_seq'),
    kind           VARCHAR NOT NULL,      -- lab_panel | follow_up | medical_exam | physio | other
    title          VARCHAR NOT NULL,
    markers        JSON,                  -- catalogue keys
    due_date       DATE,
    interval_days  INTEGER,               -- recurring manual checks
    status         VARCHAR NOT NULL,      -- proposed | open | snoozed | done | dismissed
    snoozed_until  DATE,
    source         VARCHAR NOT NULL,      -- rule | manual | claude_proposed
    rule_key       VARCHAR,               -- one check per key, e.g. "followup:2026-05-07"
    rationale      VARCHAR,
    done_on        DATE,
    lab_date       DATE,                  -- results that completed a lab check
    created_at     TIMESTAMPTZ DEFAULT current_timestamp,
    updated_at     TIMESTAMPTZ DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS season_proposals (
    id          INTEGER PRIMARY KEY DEFAULT nextval('proposal_id_seq'),
    kind        VARCHAR NOT NULL,      -- phase | annotation
    action      VARCHAR NOT NULL,      -- create | update | delete
    target_id   INTEGER,               -- existing phase/annotation for update/delete
    payload     JSON,                  -- proposed field values
    reason      VARCHAR NOT NULL,
    summary     VARCHAR NOT NULL,      -- human-readable description of the change
    status      VARCHAR NOT NULL,      -- pending | applied | dismissed | failed
    error       VARCHAR,
    source      VARCHAR NOT NULL,      -- claude
    created_at  TIMESTAMPTZ DEFAULT current_timestamp,
    decided_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS app_settings (
    key         VARCHAR PRIMARY KEY,
    value       JSON NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT current_timestamp
);
"""

_INDEXES: str = """
CREATE INDEX IF NOT EXISTS idx_activities_start_time
    ON activities (start_time);

CREATE INDEX IF NOT EXISTS idx_activities_sport_type
    ON activities (sport_type);

CREATE INDEX IF NOT EXISTS idx_activity_streams_activity_id
    ON activity_streams (activity_id);

CREATE INDEX IF NOT EXISTS idx_activity_metrics_date
    ON activity_metrics (date);

CREATE INDEX IF NOT EXISTS idx_daily_training_load_date
    ON daily_training_load (date);

CREATE INDEX IF NOT EXISTS idx_jobs_status
    ON jobs (status);

CREATE INDEX IF NOT EXISTS idx_session_grades_activity
    ON session_grades (activity_id);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_schema(db: Database) -> None:
    """Create all tables, indexes, and record the schema version.

    Safe to call repeatedly -- every DDL statement uses ``IF NOT EXISTS``.
    """
    for ddl_block in (_RAW_TABLES, _COMPUTED_TABLES, _METADATA_TABLES, _APP_TABLES, _INDEXES):
        for statement in _split_statements(ddl_block):
            db.execute(statement)

    # Run incremental migrations.
    _run_migrations(db)

    # Record schema version if not already present.
    existing: list[tuple[int]] = db.fetchall(
        "SELECT version FROM schema_version WHERE version = ?",
        [CURRENT_SCHEMA_VERSION],
    )
    if not existing:
        db.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            [CURRENT_SCHEMA_VERSION],
        )


def _run_migrations(db: Database) -> None:
    """Apply incremental schema migrations."""
    applied = {row[0] for row in db.fetchall("SELECT version FROM schema_version")}

    if 2 not in applied:
        # v2: add Garmin summaryDTO fields to activities
        _add_column_if_missing(db, "activities", "rpe", "INT")
        _add_column_if_missing(db, "activities", "feel", "INT")
        _add_column_if_missing(db, "activities", "garmin_training_load", "DOUBLE")
        _add_column_if_missing(db, "activities", "training_effect_label", "VARCHAR")
        _add_column_if_missing(db, "activities", "body_battery_delta", "INT")
        _add_column_if_missing(db, "activities", "begin_stamina", "DOUBLE")
        _add_column_if_missing(db, "activities", "end_stamina", "DOUBLE")

    if 3 not in applied:
        # v3: drop FTP-dependent columns and never-populated zone columns
        _drop_column_if_exists(db, "activity_metrics", "intensity_factor")
        _drop_column_if_exists(db, "activity_metrics", "power_zone_seconds")
        _drop_column_if_exists(db, "activity_metrics", "pace_zone_seconds")
        _drop_column_if_exists(db, "weekly_summary", "intensity_distribution")

    if 4 not in applied:
        # v4: drop manual-input table (lactate tests)
        try:
            db.execute("DROP TABLE IF EXISTS lactate_tests")
        except Exception:
            pass

    if 5 not in applied:
        # v5: strength training was parsed as "other" (FIT sport=training,
        # sub_sport=strength_training).  Reclassify; strength_sets table is
        # created by the DDL above.  Activity IDs keep their "_other" suffix.
        db.execute(
            "UPDATE activity_metrics SET sport_type = 'strength' WHERE activity_id IN ("
            "  SELECT activity_id FROM activities WHERE sub_type = 'strength_training')"
        )
        db.execute("UPDATE activities SET sport_type = 'strength' WHERE sub_type = 'strength_training'")
        # Per-sport load for "other" included these sessions; drop it so the
        # next update_training_load() rebuilds "other" and "strength" cleanly.
        db.execute("DELETE FROM daily_training_load WHERE sport_type = 'other'")

    if 11 not in applied:
        # v11: Garmin Connect workouts for planned sessions.
        for column, col_type in (
            ("garmin_steps", "JSON"),
            ("garmin_workout_id", "VARCHAR"),
            ("garmin_schedule_id", "VARCHAR"),
            ("garmin_status", "VARCHAR"),
            ("garmin_error", "VARCHAR"),
            ("garmin_sent_at", "TIMESTAMPTZ"),
        ):
            _add_column_if_missing(db, "planned_sessions", column, col_type)

    if 12 not in applied:
        # v12: Ember can propose changing or archiving an existing note (a 'proposed' row that targets it).
        _add_column_if_missing(db, "athlete_notes", "target_id", "INTEGER")
        _add_column_if_missing(db, "athlete_notes", "proposed_action", "VARCHAR")  # NULL/create, update, archive
        _add_column_if_missing(db, "athlete_notes", "proposal_reason", "VARCHAR")

    if 13 not in applied:
        # v13: one session note. session_feedback.comment is the athlete's note in hart; NULL means "follow the
        # Garmin description". note_garmin_seen is the Garmin text when the note was written in hart, so a
        # later Garmin edit can be offered. Existing comments absorb the Garmin text so nothing is lost.
        _add_column_if_missing(db, "session_feedback", "note_garmin_seen", "VARCHAR")
        db.execute(
            "UPDATE session_feedback AS f SET "
            "comment = CASE WHEN a.description IS NOT NULL AND trim(a.description) <> '' "
            "AND trim(a.description) <> trim(f.comment) THEN a.description || chr(10) || chr(10) || f.comment "
            "ELSE f.comment END, note_garmin_seen = a.description "
            "FROM activities AS a WHERE a.activity_id = f.activity_id AND f.comment IS NOT NULL "
            "AND f.note_garmin_seen IS NULL"
        )

    if 14 not in applied:
        # v14: interval breakdowns — each lap's kind and workout step (the table itself is created above).
        _add_column_if_missing(db, "activity_laps", "intensity", "VARCHAR")
        _add_column_if_missing(db, "activity_laps", "wkt_step_index", "INT")

    if 15 not in applied:
        # v15: heart-rate targets stored before plain-bpm files were recognised: the parser subtracted the FIT
        # +100 offset from values that had none (120-140 bpm became 20-40). No workout targets under 80 bpm.
        db.execute(
            "UPDATE activity_workout_steps SET target_low = target_low + 100, target_high = target_high + 100 "
            "WHERE target_unit = 'bpm' AND coalesce(target_high, target_low) < 80"
        )

    if 9 not in applied:
        # v9: accepted-suggestion link and Garmin text on plan rows;
        # failure reason and frozen context on suggestions.
        _add_column_if_missing(db, "planned_sessions", "suggestion_id", "INTEGER")
        _add_column_if_missing(db, "planned_sessions", "garmin_text", "VARCHAR")
        _add_column_if_missing(db, "daily_suggestions", "error", "VARCHAR")
        _add_column_if_missing(db, "daily_suggestions", "context", "JSON")


def _add_column_if_missing(db: Database, table: str, column: str, col_type: str) -> None:
    """Add a column to a table if it doesn't already exist."""
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        pass  # Column already exists


def _drop_column_if_exists(db: Database, table: str, column: str) -> None:
    """Drop a column from a table if it exists."""
    try:
        db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    except Exception:
        pass  # Column doesn't exist or already dropped


def _split_statements(block: str) -> list[str]:
    """Split a multi-statement DDL block on semicolons, ignoring blanks/comments."""
    import re

    statements: list[str] = []
    for raw in block.split(";"):
        # Strip leading/trailing whitespace and remove SQL comment lines
        cleaned = re.sub(r"^\s*--[^\n]*\n?", "", raw, flags=re.MULTILINE).strip()
        if cleaned:
            statements.append(cleaned)
    return statements
