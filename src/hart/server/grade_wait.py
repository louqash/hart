"""Waiting for the athlete's note before grading (Settings → Notifications).

A session a sync brings in without a note or an effort rating (RPE or feel — from Garmin Connect or hart)
isn't graded right away: it waits up to ``grade_wait_minutes`` so the athlete can add them, since the
grade reads both. The Discord bot asks for them: "Add note" opens a form (RPE, feel, note), "Skip — grade
now" grades without. The session is graded as soon as both are in (a sync brings them from Garmin, or
they're saved on the session page or in the form), when the athlete skips, or when the wait is over (the
scheduler's tick). Any grade of the session ends the wait, and the message loses its buttons. With only a
webhook the message has no buttons; the wait is the same.

Sessions that aren't graded anyway (walks, under 10 minutes…) never wait.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from hart.analytics.grading_features import eligibility
from hart.config import HartSettings
from hart.server import evening, settings
from hart.server.data import one
from hart.server.habits import BUTTON, BUTTON_ROW, PREFIX, SECONDARY, ephemeral
from hart.storage.database import Database

logger = logging.getLogger(__name__)

Enqueue = Callable[..., dict[str, Any]]

ADD_NOTE, GRADE_NOW = "gradenote", "gradenow"
ACTIONS = (ADD_NOTE, GRADE_NOW)
PRIMARY, LINK = 1, 5
TEXT_INPUT, SHORT, PARAGRAPH = 4, 1, 2
CLICK, MODAL_SUBMIT = 3, 5
UPDATE_MESSAGE, MODAL = 7, 9
GAPS = {"note": "note", "effort": "RPE/feel"}

# What the Discord message says once the wait is over.
NOTES_IN = "✅ Note and effort are in — grading now."
SAVED = "✅ Saved — grading now."
SKIPPED = "⏭️ Grading now, without a note."
WAIT_OVER = "⏱️ The wait is over — grading without a note."
GRADING = "Grading now."


# ---------------------------------------------------------------------------
# What's missing
# ---------------------------------------------------------------------------


def missing(db: Database, activity_id: str) -> list[str]:
    """What the grade would go without: ``note`` and/or ``effort`` (RPE or feel), from hart or Garmin."""
    row = db.fetchone(
        "SELECT trim(coalesce(f.comment, a.description, '')) <> '', "
        "coalesce(f.rpe, f.feel, nullif(a.rpe, 0), a.feel) IS NOT NULL "
        "FROM activities a LEFT JOIN session_feedback f USING (activity_id) WHERE a.activity_id = ?",
        [activity_id],
    )
    if row is None:
        return []
    return [gap for gap, present in zip(GAPS, row) if not present]


def _gradable(db: Database, activity_id: str) -> bool:
    activity = one(
        db,
        "SELECT sport_type, sub_type, moving_seconds, elapsed_seconds, avg_hr, avg_power, avg_pace_sec_km "
        "FROM activities WHERE activity_id = ?",
        [activity_id],
    )
    return activity is not None and eligibility(activity, False)[0]


# ---------------------------------------------------------------------------
# The wait
# ---------------------------------------------------------------------------


def hold(
    db: Database, config: HartSettings, activity_ids: list[str], *, now: datetime.datetime | None = None
) -> list[str]:
    """Hold back new sessions that lack a note or effort and ask for them; returns the ones held — the
    caller grades the rest right away."""
    minutes = int(settings.get(db, "grade_wait_minutes") or 0)
    if minutes <= 0:
        return []
    grade_at = (now or datetime.datetime.now(tz=datetime.UTC)) + datetime.timedelta(minutes=minutes)
    held = []
    for activity_id in activity_ids:
        gaps = missing(db, activity_id)
        if not gaps or not _gradable(db, activity_id):
            continue
        db.execute(
            "INSERT INTO grade_waits (activity_id, grade_at, missing) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [activity_id, grade_at, ",".join(gaps)],
        )
        held.append(activity_id)
        _ask(db, config, activity_id, grade_at, gaps)
    return held


def release(db: Database, config: HartSettings | None, activity_id: str, status: str, *, edit: bool = True) -> bool:
    """End the wait — the caller grades. The Discord message loses its buttons and says *status* (unless
    *edit* is off: a button click answers with the new message itself). False if it wasn't waiting."""
    row = db.fetchone("DELETE FROM grade_waits WHERE activity_id = ? RETURNING message_id, channel_id", [activity_id])
    if row is None:
        return False
    message_id, channel_id = row
    if edit and message_id and config is not None and config.discord.bot_token:
        try:
            evening.edit(config, channel_id, message_id, resolved(db, activity_id, status))
        except Exception as exc:  # noqa: BLE001 — the grade matters, not the old message
            logger.warning("Discord: updating the note request for %s failed: %s", activity_id, exc)
    return True


def is_waiting(db: Database, activity_id: str) -> bool:
    return db.fetchone("SELECT 1 FROM grade_waits WHERE activity_id = ?", [activity_id]) is not None


def due(db: Database, now: datetime.datetime) -> list[str]:
    """Waiting sessions whose time is up."""
    return [r[0] for r in db.fetchall("SELECT activity_id FROM grade_waits WHERE grade_at <= ? ORDER BY 1", [now])]


def completed(db: Database) -> list[str]:
    """Waiting sessions that have their note and effort now (a sync brought them from Garmin)."""
    return [a for (a,) in db.fetchall("SELECT activity_id FROM grade_waits ORDER BY 1") if not missing(db, a)]


def waiting(db: Database, activity_id: str, tz: str) -> dict[str, Any] | None:
    """For the session page: when it's graded at the latest and what's still missing."""
    row = db.fetchone("SELECT grade_at FROM grade_waits WHERE activity_id = ?", [activity_id])
    if row is None:
        return None
    return {
        "grade_at": row[0].astimezone(ZoneInfo(tz)).strftime("%H:%M"),
        "missing": [GAPS[g] for g in missing(db, activity_id)],
    }


def grade(enqueue: Enqueue, activity_id: str, trigger: str, *, job_trigger: str = "manual") -> dict[str, Any]:
    return enqueue(
        "grade",
        {"activity_id": activity_id, "trigger": trigger},
        trigger=job_trigger,
        dedupe_key=f"grade:{activity_id}",
    )


# ---------------------------------------------------------------------------
# Feedback (the session page and the Discord form)
# ---------------------------------------------------------------------------


def save_feedback(db: Database, activity_id: str, rpe: int | None, feel: int | None, note: str | None) -> None:
    """Store the athlete's RPE, feel and note. A note that is still Garmin's description keeps following it."""
    garmin = db.fetchone("SELECT description FROM activities WHERE activity_id = ?", [activity_id])[0] or ""
    garmin = garmin.replace("\r\n", "\n").strip()
    current = db.fetchone("SELECT comment, note_garmin_seen FROM session_feedback WHERE activity_id = ?", [activity_id])
    note = (note or "").replace("\r\n", "\n").strip()
    if current is None or current[0] is None:
        # Following Garmin: an unchanged (or empty, with nothing in Garmin) note keeps following it.
        comment, seen = (None, None) if note == garmin else (note, garmin or None)
    else:
        comment, seen = note, current[1]
    db.execute(
        "INSERT OR REPLACE INTO session_feedback (activity_id, rpe, feel, comment, note_garmin_seen, updated_at) "
        "VALUES (?, ?, ?, ?, ?, current_timestamp)",
        [activity_id, rpe, feel, comment, seen],
    )


def after_feedback(
    db: Database, config: HartSettings | None, activity_id: str, enqueue: Enqueue, *, edit: bool = True
) -> dict[str, Any] | None:
    """The grade saved feedback starts: a waiting session is graded now; a graded one is re-graded with it
    once, never more."""
    if release(db, config, activity_id, SAVED, edit=edit):
        return grade(enqueue, activity_id, "notes_added")
    graded = db.fetchone(
        "SELECT count(*) FROM session_grades WHERE activity_id = ? AND status = 'graded'", [activity_id]
    )[0]
    already = db.fetchone(
        "SELECT count(*) FROM session_grades WHERE activity_id = ? "
        "AND json_extract_string(features, '$.trigger') = 'feedback'",
        [activity_id],
    )[0]
    if graded and not already:
        return grade(enqueue, activity_id, "feedback")
    return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _headline(db: Database, activity_id: str) -> str:
    a = (
        one(
            db,
            "SELECT name, sport_type, start_time, coalesce(moving_seconds, elapsed_seconds) AS seconds "
            "FROM activities WHERE activity_id = ?",
            [activity_id],
        )
        or {}
    )
    icon = evening.SPORT_ICONS.get(a.get("sport_type") or "", "•")
    name = a.get("name") or (a.get("sport_type") or "session").title()
    parts = [f"**{icon} {name}**"]
    if a.get("start_time"):
        parts.append(f"{a['start_time']:%a %d %b}")
    if a.get("seconds"):
        parts.append(evening._minutes(a["seconds"]))
    return " · ".join(parts)


def _url(config: HartSettings, activity_id: str) -> str | None:
    return f"https://{config.server.public_host}/sessions/{activity_id}" if config.server.public_host else None


def prompt(
    db: Database,
    config: HartSettings,
    activity_id: str,
    grade_at: datetime.datetime,
    gaps: list[str],
    *,
    buttons: bool,
) -> dict[str, Any]:
    """The message asking for the note: buttons with the bot, a link with a webhook."""
    when = grade_at.astimezone(ZoneInfo(config.server.tz)).strftime("%H:%M")
    them = "it" if len(gaps) == 1 else "them"
    where = "here, in Garmin Connect or in hart" if buttons else "in Garmin Connect or in hart"
    text = (
        f"{_headline(db, activity_id)}\n📝 No {' or '.join(GAPS[g] for g in gaps)} yet — add {them} {where} "
        f"and Ember grades with {them}; otherwise at {when}, without."
    )
    url = _url(config, activity_id)
    if not buttons:
        return {"content": text + (f"\n{url}" if url else "")}
    row: list[dict[str, Any]] = [
        {"type": BUTTON, "style": PRIMARY, "label": "Add note", "custom_id": f"{PREFIX}:{ADD_NOTE}:{activity_id}"},
        {
            "type": BUTTON,
            "style": SECONDARY,
            "label": "Skip — grade now",
            "custom_id": f"{PREFIX}:{GRADE_NOW}:{activity_id}",
        },
    ]
    if url:
        row.append({"type": BUTTON, "style": LINK, "label": "Open in hart", "url": url})
    return {"content": text, "components": [{"type": BUTTON_ROW, "components": row}]}


def resolved(db: Database, activity_id: str, status: str) -> dict[str, Any]:
    """The message once the wait is over: no buttons, and why."""
    return {"content": f"{_headline(db, activity_id)}\n{status}", "components": []}


def _ask(db: Database, config: HartSettings, activity_id: str, grade_at: datetime.datetime, gaps: list[str]) -> None:
    """Post the request; a failure never stops the wait (the session page shows it too)."""
    if not evening.configured(config):
        return
    bot = bool(config.discord.bot_token and config.discord.channel_id)
    try:
        created = evening.send(config, prompt(db, config, activity_id, grade_at, gaps, buttons=bot)) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Discord: asking for the note on %s failed: %s", activity_id, exc)
        return
    if bot and created.get("id"):
        db.execute(
            "UPDATE grade_waits SET message_id = ?, channel_id = ? WHERE activity_id = ?",
            [str(created["id"]), str(created.get("channel_id") or config.discord.channel_id), activity_id],
        )


def note_form(db: Database, activity_id: str) -> dict[str, Any]:
    """The form "Add note" opens, filled with what hart or Garmin already has."""
    a = one(
        db,
        "SELECT a.name, a.sport_type, a.description, nullif(a.rpe, 0) AS garmin_rpe, a.feel AS garmin_feel, "
        "f.rpe, f.feel, f.comment FROM activities a LEFT JOIN session_feedback f USING (activity_id) "
        "WHERE a.activity_id = ?",
        [activity_id],
    ) or {"sport_type": "session"}
    rpe = a.get("rpe") if a.get("rpe") is not None else (round(a["garmin_rpe"] / 10) if a.get("garmin_rpe") else None)
    feel = (
        a.get("feel")
        if a.get("feel") is not None
        else (round(a["garmin_feel"] / 25) + 1 if a.get("garmin_feel") is not None else None)
    )
    note = a.get("comment") if a.get("comment") is not None else a.get("description")

    def field(custom_id: str, label: str, style: int, max_length: int, value: Any, placeholder: str) -> dict[str, Any]:
        text_input: dict[str, Any] = {
            "type": TEXT_INPUT,
            "custom_id": custom_id,
            "label": label,
            "style": style,
            "required": False,
            "max_length": max_length,
            "placeholder": placeholder,
        }
        if value not in (None, ""):
            text_input["value"] = str(value)[:max_length]
        return {"type": BUTTON_ROW, "components": [text_input]}

    name = a.get("name") or str(a["sport_type"]).title()
    return {
        "type": MODAL,
        "data": {
            "custom_id": f"{PREFIX}:{ADD_NOTE}:{activity_id}",
            "title": f"How it went · {name}"[:45],
            "components": [
                field("rpe", "RPE — 1 (very easy) to 10 (maximal)", SHORT, 2, rpe, "e.g. 6"),
                field("feel", "Feel — 1 (very weak) to 5 (very strong)", SHORT, 1, feel, "e.g. 4"),
                field(
                    "note",
                    "Note — the workout, how it went",
                    PARAGRAPH,
                    4000,
                    note,
                    "e.g. 3×10′ sweet spot, legs heavy",
                ),
            ],
        },
    }


def _form_values(data: dict[str, Any]) -> dict[str, str]:
    """custom_id → value of every text input in a submitted form (inputs sit in rows, or in labels)."""
    out: dict[str, str] = {}
    stack: list[Any] = list(data.get("components") or [])
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        if "custom_id" in item and "value" in item:
            out[item["custom_id"]] = item["value"] or ""
        stack += item.get("components") or []
        if isinstance(item.get("component"), dict):
            stack.append(item["component"])
    return out


def _scale(raw: str | None, name: str, top: int) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= top:
        raise ValueError(f"{name} is a whole number from 1 to {top}")
    return value


def handle_interaction(
    db: Database, config: HartSettings, kind: int, data: dict[str, Any], enqueue: Enqueue
) -> dict[str, Any] | None:
    """The response to "Add note", "Skip — grade now" or the submitted form; None if it isn't one of these."""
    parts = str(data.get("custom_id") or "").split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX or parts[1] not in ACTIONS:
        return None
    action, activity_id = parts[1], parts[2]
    if db.fetchone("SELECT 1 FROM activities WHERE activity_id = ?", [activity_id]) is None:
        return ephemeral("That session isn't in hart any more.")

    def update(status: str) -> dict[str, Any]:
        return {"type": UPDATE_MESSAGE, "data": resolved(db, activity_id, status)}

    if kind == CLICK and action == GRADE_NOW:
        if release(db, config, activity_id, SKIPPED, edit=False):
            grade(enqueue, activity_id, "sync")
            return update(SKIPPED)
        graded = db.fetchone("SELECT 1 FROM session_grades WHERE activity_id = ? AND status = 'graded'", [activity_id])
        return update("Already graded." if graded else GRADING)
    if kind == CLICK and action == ADD_NOTE:
        return note_form(db, activity_id)
    if kind == MODAL_SUBMIT and action == ADD_NOTE:
        values = _form_values(data)
        try:
            rpe, feel = _scale(values.get("rpe"), "RPE", 10), _scale(values.get("feel"), "Feel", 5)
        except ValueError as exc:
            return ephemeral(f"{exc} — nothing was saved. Tap “Add note” to try again.")
        was_waiting = is_waiting(db, activity_id)
        save_feedback(db, activity_id, rpe, feel, values.get("note"))
        job = after_feedback(db, config, activity_id, enqueue, edit=False)
        if job is None:
            return update("✅ Saved.")
        return update(SAVED if was_waiting else "✅ Saved — re-grading with it.")
    return None
