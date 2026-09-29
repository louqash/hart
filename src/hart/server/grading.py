"""Session grading.

Code computes the facts (``analytics.grading_features``) and the overall
score; Claude scores three dimensions with justifications and cites the
numbers it used; code then checks every citation against the facts and the
tool outputs of that run.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from hart.analytics.grading_features import build_features
from hart.config import HartSettings
from hart.server import settings, state
from hart.server.claude.policy import read_only_policy
from hart.server.claude.prompts import athlete_profile
from hart.server.claude.runner import ClaudeRunner, RunOutcome, RunSpec
from hart.server.data import rows
from hart.storage.database import Database

logger = logging.getLogger(__name__)

GRADE_PROMPT_VERSION = "grade@4"
WEIGHTS = {"execution": 0.45, "response": 0.30, "context_fit": 0.25}
LETTERS = ((4.5, "A"), (3.75, "B"), (3.0, "C"), (2.25, "D"))
UNVERIFIED_LIMIT = 0.30
CLAUDE_PAUSED_UNTIL = "claude_paused_until"

SessionType = Literal[
    "recovery",
    "endurance",
    "long",
    "tempo",
    "threshold",
    "vo2_intervals",
    "race",
    "brick",
    "strength",
    "technique",
    "event_trip",
    "other",
]


class Scores(BaseModel):
    execution: int | None = Field(default=None, ge=1, le=5)
    response: int | None = Field(default=None, ge=1, le=5)
    context_fit: int = Field(ge=1, le=5)


class Justifications(BaseModel):
    execution: str | None = None
    response: str | None = None
    context_fit: str


class Citation(BaseModel):
    label: str = Field(max_length=80)
    value: float | str
    unit: str | None = None
    source: str = Field(description="Where the value comes from: 'features.<path>' or a tool name")


class GradeOutput(BaseModel):
    session_type: SessionType
    scores: Scores
    justifications: Justifications
    confidence: Literal["high", "medium", "low"]
    summary: str = Field(max_length=700)
    highlights: list[str] = Field(default_factory=list, max_length=3)
    concerns: list[str] = Field(default_factory=list, max_length=3)
    citations: list[Citation] = Field(default_factory=list, max_length=15)


_SYSTEM = """\
You grade one training session for {name}, an endurance athlete. Their goals, injuries and \
constraints are in the bundle's context and in `get_athlete_context`.

You get a JSON bundle of facts computed from their data. You may call the read-only hart \
tools for more (e.g. `get_activity_streams`, `get_strength_history`, `get_athlete_context`), \
but the bundle is usually enough — keep it to a few calls.

# Rubric — score each dimension 1–5 (5 = excellent) with one sentence of justification
- execution: did the session hit its intent? No coach plan is linked yet, so infer the intent \
(session_type) from the facts and judge zones, duration, pacing/power steadiness, structure.
- response: how did the body respond vs their own comparable sessions (efficiency, HR at output, \
decoupling, HR drift)? If `comparison.pre_injury_baseline` is true, the comparison sessions are \
from before the injury: judge the trajectory back toward that level, not the gap. Use null when \
there's nothing to compare or no heart rate.
- context_fit: was it the right session given readiness that morning, form (TSB), the phase \
(e.g. comeback: consistency and tissue tolerance over intensity), active constraints and their \
feedback?
- `feedback` holds what the athlete said about the session: RPE, feel and their `note` (written in \
hart or in Garmin Connect — often the coach's workout or how it went). Read the note: it states the \
intent, pain or niggles, and circumstances the numbers can't show, and it weighs in all three scores.
For `event` sessions (trips, camps and similar) set session_type "event_trip" and execution and \
response to null — score context fit only.

# Rules
- Every number in summary, highlights, concerns and justifications must come from the bundle or a \
tool result; list each one you rely on in `citations` with its exact value and source \
(e.g. "features.durability.stream_decoupling.decoupling_pct"). Never estimate or invent numbers.
- Missing data lowers confidence; it is not a fault of the athlete.
- {data_notes}
- Be direct and specific; plain English; no filler. summary ≤ 3 sentences.
"""


def grading_system_prompt(profile: dict[str, Any]) -> str:
    from hart.server.claude.prompts import data_notes

    return _SYSTEM.format(name=profile["name"], data_notes=data_notes(profile))


def grade_output_schema() -> dict[str, Any]:
    return GradeOutput.model_json_schema()


# ---------------------------------------------------------------------------
# Scoring and verification (code, not Claude)
# ---------------------------------------------------------------------------


def overall(scores: Scores) -> tuple[float, str]:
    parts = {k: getattr(scores, k) for k in WEIGHTS if getattr(scores, k) is not None}
    weight = sum(WEIGHTS[k] for k in parts)
    value = sum(WEIGHTS[k] * v for k, v in parts.items()) / weight
    letter = next((grade for threshold, grade in LETTERS if value >= threshold), "E")
    return round(value, 2), letter


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(n) for n in _NUM.findall(text)]


def verify_citations(citations: list[Citation], sources: list[str]) -> list[dict[str, Any]]:
    """Mark each cited number verified if it appears in the facts or tool outputs.

    A match is the same value at the cited precision (rounding), or within 1%
    for values above 10.  Non-numeric citations are checked as text.
    """
    haystack = "\n".join(sources)
    numbers = _numbers(haystack)
    out = []
    for c in citations:
        verified = False
        value = c.value
        if isinstance(value, str):
            try:
                value = float(value.replace(",", "."))
            except ValueError:
                verified = value.strip().lower() in haystack.lower()
        if isinstance(value, float | int):
            text = repr(float(value))
            decimals = len(text.split(".")[1].rstrip("0")) if "." in text else 0
            for n in numbers:
                if round(n, decimals) == round(float(value), decimals) or (
                    abs(float(value)) > 10 and abs(n - float(value)) <= abs(float(value)) * 0.01
                ):
                    verified = True
                    break
        out.append({**c.model_dump(), "verified": verified})
    return out


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _next_version(db: Database, activity_id: str) -> int:
    return db.fetchone("SELECT coalesce(max(version), 0) + 1 FROM session_grades WHERE activity_id = ?", [activity_id])[
        0
    ]


def store_grade(db: Database, activity_id: str, fields: dict[str, Any]) -> int:
    version = _next_version(db, activity_id)
    cols = ["activity_id", "version", *fields]
    values = [
        activity_id,
        version,
        *[json.dumps(v, default=str) if isinstance(v, dict | list) else v for v in fields.values()],
    ]
    db.execute(f"INSERT INTO session_grades ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", values)
    return version


def claude_paused(db: Database) -> str | None:
    until = state.get_setting(db, CLAUDE_PAUSED_UNTIL)
    if until and datetime.datetime.fromisoformat(until) > datetime.datetime.now(tz=datetime.UTC):
        return until
    return None


def pause_claude(db: Database, outcome: RunOutcome) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    if outcome.status == "usage_limited":
        until = (
            datetime.datetime.fromtimestamp(outcome.resets_at, tz=datetime.UTC)
            if outcome.resets_at
            else now + datetime.timedelta(minutes=60)
        )
    else:  # auth_failed: back off until someone fixes the token
        until = now + datetime.timedelta(hours=6)
    state.set_setting(db, CLAUDE_PAUSED_UNTIL, until.isoformat())


class GradingPaused(Exception):
    pass


def grade_activity(
    db: Database,
    activity_id: str,
    claude: ClaudeRunner,
    loop: asyncio.AbstractEventLoop,
    config: HartSettings,
    *,
    trigger: str = "sync",
    force: bool = False,
) -> dict[str, Any]:
    """Grade one activity (runs in a job thread; Claude runs on the event loop)."""
    features = build_features(db, activity_id, state.get_thresholds(db))
    if features is None:
        raise ValueError(f"Activity {activity_id} not found")
    features["trigger"] = trigger

    if not features["gradable"] and not force:
        version = store_grade(
            db,
            activity_id,
            {
                "status": "ungraded",
                "ungraded_reason": features["ungraded_reason"],
                "features": features,
            },
        )
        return {"status": "ungraded", "version": version, "reason": features["ungraded_reason"]}

    paused = claude_paused(db)
    if paused:
        raise GradingPaused(f"Claude paused until {paused}")

    bundle = json.dumps(features, default=str, indent=1)
    prompt = f"Grade this session. Facts:\n```json\n{bundle}\n```"
    spec = RunSpec(
        purpose="grade",
        prompt=prompt,
        model=settings.get(db, "model_grade"),
        system_prompt=grading_system_prompt(athlete_profile(db)),
        prompt_version=GRADE_PROMPT_VERSION,
        policy=read_only_policy(),
        max_turns=12,
        timeout_s=300,
        background=True,
        output_schema=grade_output_schema(),
    )

    def run(s: RunSpec) -> RunOutcome:
        return asyncio.run_coroutine_threadsafe(claude.run(s), loop).result(timeout=s.timeout_s + 60)

    outcome = run(spec)
    if outcome.status in ("usage_limited", "auth_failed"):
        pause_claude(db, outcome)
        raise GradingPaused(outcome.error or outcome.status)

    output, error = _parse(outcome)
    if output is None and outcome.status == "ok":
        # One repair attempt with the validation error.
        repair = RunSpec(
            **{
                **spec.__dict__,
                "prompt": f"{prompt}\n\nYour previous answer failed validation: {error}. Return a corrected grade.",
                "resume": outcome.session_id,
            }
        )
        outcome = run(repair)
        output, error = _parse(outcome)

    if output is None:
        version = store_grade(
            db,
            activity_id,
            {
                "status": "failed",
                "ungraded_reason": (error or outcome.error or "no output")[:500],
                "features": features,
                "claude_run_id": outcome.run_id,
            },
        )
        return {"status": "failed", "version": version, "error": error or outcome.error}

    if features.get("event"):
        output.session_type = "event_trip"
        output.scores.execution = output.scores.response = None
    score, letter = overall(output.scores)
    citations = verify_citations(output.citations, [bundle, *outcome.tool_outputs])
    confidence = output.confidence
    if citations and sum(not c["verified"] for c in citations) / len(citations) > UNVERIFIED_LIMIT:
        confidence = "low"
    version = store_grade(
        db,
        activity_id,
        {
            "status": "graded",
            "intent_source": "inferred",
            "session_type": output.session_type,
            "score_execution": output.scores.execution,
            "score_response": output.scores.response,
            "score_context": output.scores.context_fit,
            "overall_score": score,
            "letter": letter,
            "confidence": confidence,
            "summary": output.summary,
            "highlights": output.highlights,
            "concerns": output.concerns,
            "citations": citations,
            "features": {**features, "justifications": output.justifications.model_dump()},
            "claude_run_id": outcome.run_id,
        },
    )
    return {"status": "graded", "version": version, "letter": letter, "score": score}


def _structured_raw(outcome: RunOutcome) -> Any:
    """The structured output, or a JSON object found in the text as a fallback."""
    raw = outcome.structured
    if raw is None and outcome.text:
        match = re.search(r"\{.*\}", outcome.text, re.S)
        if match:
            with contextlib.suppress(json.JSONDecodeError):
                raw = json.loads(match.group())
    return raw


def _parse(outcome: RunOutcome) -> tuple[GradeOutput | None, str | None]:
    if outcome.status != "ok":
        return None, outcome.error
    raw = _structured_raw(outcome)
    if raw is None:
        return None, "no structured output"
    try:
        return GradeOutput.model_validate(raw), None
    except ValidationError as exc:
        return None, str(exc)[:800]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

GRADE_COLUMNS = (
    "id, activity_id, version, status, ungraded_reason, intent_source, session_type, score_execution, "
    "score_response, score_context, overall_score, letter, confidence, summary, highlights, concerns, "
    "citations, features, claude_run_id, created_at"
)


def _decode(g: dict[str, Any]) -> dict[str, Any]:
    for key in ("highlights", "concerns", "citations", "features"):
        if isinstance(g.get(key), str):
            g[key] = json.loads(g[key])
    return g


def latest_grade(db: Database, activity_id: str, version: int | None = None) -> dict[str, Any] | None:
    where = "AND version = ?" if version else ""
    params: list[Any] = [activity_id, version] if version else [activity_id]
    found = rows(
        db,
        f"SELECT {GRADE_COLUMNS} FROM session_grades WHERE activity_id = ? {where} ORDER BY version DESC LIMIT 1",
        params,
    )
    return _decode(found[0]) if found else None


def grade_chips(db: Database, activity_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Latest grade status/letter per activity, plus queued/running grading jobs."""
    if not activity_ids:
        return {}
    placeholders = ", ".join("?" for _ in activity_ids)
    chips = {
        r["activity_id"]: r
        for r in rows(
            db,
            "SELECT activity_id, status, letter, overall_score, ungraded_reason, version FROM ("
            "SELECT *, row_number() OVER (PARTITION BY activity_id ORDER BY version DESC) AS rn "
            f"FROM session_grades WHERE activity_id IN ({placeholders})) WHERE rn = 1",
            activity_ids,
        )
    }
    for r in rows(
        db,
        f"SELECT dedupe_key FROM jobs WHERE type = 'grade' AND status IN ('queued', 'running') "
        f"AND dedupe_key IN ({placeholders})",
        [f"grade:{a}" for a in activity_ids],
    ):
        chips.setdefault(r["dedupe_key"][6:], {})["pending"] = True
    return chips
