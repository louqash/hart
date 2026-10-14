"""Sauna log and daily habits: storage, read models and the Discord check-in.

Sauna sessions are their own rows (duration, temperature, rounds, kind). Habits are yes/no per day, from
the list in Settings → Athlete. A day counts as answered once any habit row exists for it — tapping one
button of the check-in records the others as not done, so "no row" always means "unknown", never "no".

The Discord check-in is a message with buttons (sent by the bot, see ``discord_chat``): a habit button
toggles it, "None of these" records a day without any. Sauna isn't on it — a session is an event with
details, often several a day, logged by telling Ember (``log_sauna_session``) or on the Health page.
"""

from __future__ import annotations

import datetime
from typing import Any

from hart.analytics.habits import (
    SAUNA_KINDS,
    habit_key,
    next_morning_comparison,
    parse_habits,
    sauna_summary,
)
from hart.server import settings
from hart.server.data import DAY, rows
from hart.storage.database import Database

D = datetime.date
SOURCES = ("discord", "ember", "web")
COMPARISON_DAYS = 180
CONTEXT_DAYS = 3


class HabitError(ValueError):
    pass


def definitions(db: Database) -> list[dict[str, str]]:
    return parse_habits(settings.get(db, "habits"))


# ---------------------------------------------------------------------------
# Sauna
# ---------------------------------------------------------------------------


def _sauna_fields(
    duration_min: Any,
    temperature_c: Any = None,
    rounds: Any = None,
    kind: Any = None,
    notes: Any = None,
    start_time: Any = None,
) -> dict[str, Any]:
    def number(value: Any, name: str, low: float, high: float, cast: type = int) -> Any:
        if value in (None, "", 0, "0"):
            return None
        try:
            parsed = cast(str(value).replace(",", ".").strip().rstrip("°cC′'min ").strip())
        except ValueError as exc:
            raise HabitError(f"{name}: expected a number") from exc
        if not low <= parsed <= high:
            raise HabitError(f"{name}: expected {low:g}–{high:g}")
        return parsed

    duration = number(duration_min, "Duration (min)", 1, 240)
    if duration is None:
        raise HabitError("Duration (min) is required")
    kind_text = (str(kind or "").strip().lower() or None) if kind else None
    if kind_text is not None:
        matches = [k for k in SAUNA_KINDS if k.startswith(kind_text[:3])]
        if not matches:
            raise HabitError(f"Type: one of {', '.join(SAUNA_KINDS)}")
        kind_text = matches[0]
    when = None
    if start_time not in (None, ""):
        try:
            when = datetime.time.fromisoformat(str(start_time).strip())
        except ValueError as exc:
            raise HabitError("Start time: expected HH:MM") from exc
    return {
        "duration_min": duration,
        "temperature_c": number(temperature_c, "Temperature (°C)", 30, 130, float),
        "rounds": number(rounds, "Rounds", 1, 20),
        "kind": kind_text,
        "notes": (str(notes).strip()[:500] or None) if notes else None,
        "start_time": when,
    }


def log_sauna(db: Database, date: D, source: str, **fields: Any) -> dict[str, Any]:
    values = _sauna_fields(**fields)
    new_id = db.fetchone(
        "INSERT INTO sauna_sessions (date, start_time, duration_min, temperature_c, rounds, kind, notes, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            date,
            values["start_time"],
            values["duration_min"],
            values["temperature_c"],
            values["rounds"],
            values["kind"],
            values["notes"],
            source,
        ],
    )[0]
    return sauna_session(db, new_id) or {}


def update_sauna(db: Database, session_id: int, date: D | None = None, **fields: Any) -> dict[str, Any]:
    """Replace a session's values (fields not passed are cleared); *date* moves it to another day."""
    current = sauna_session(db, session_id)
    if current is None:
        raise HabitError(f"sauna session {session_id} not found")
    values = _sauna_fields(**fields)
    db.execute(
        "UPDATE sauna_sessions SET date = ?, start_time = ?, duration_min = ?, temperature_c = ?, rounds = ?, "
        "kind = ?, notes = ? WHERE id = ?",
        [
            date or current["date"],
            values["start_time"],
            values["duration_min"],
            values["temperature_c"],
            values["rounds"],
            values["kind"],
            values["notes"],
            session_id,
        ],
    )
    return sauna_session(db, session_id) or {}


def delete_sauna(db: Database, session_id: int) -> None:
    if sauna_session(db, session_id) is None:
        raise HabitError(f"sauna session {session_id} not found")
    db.execute("DELETE FROM sauna_sessions WHERE id = ?", [session_id])


def sauna_session(db: Database, session_id: int) -> dict[str, Any] | None:
    found = rows(db, "SELECT * FROM sauna_sessions WHERE id = ?", [session_id])
    return _sauna_view(found[0]) if found else None


def sauna_sessions(db: Database, start: D, end: D) -> list[dict[str, Any]]:
    return [
        _sauna_view(r)
        for r in rows(
            db,
            "SELECT * FROM sauna_sessions WHERE date >= ? AND date <= ? ORDER BY date, start_time NULLS LAST, id",
            [start, end],
        )
    ]


def describe_sauna(session: dict[str, Any]) -> str:
    """One line for a confirmation: ``08:00 · 2 rounds · 16′ · 90 °C · finnish``."""
    parts = [session["start_time"]] if session.get("start_time") else []
    if session.get("rounds"):
        parts.append(f"{session['rounds']} round" + ("s" if session["rounds"] != 1 else ""))
    parts.append(f"{session['duration_min']}′")
    if session.get("temperature_c"):
        parts.append(f"{session['temperature_c']:g} °C")
    if session.get("kind"):
        parts.append(session["kind"])
    return " · ".join(parts)


def day_summary(db: Database, date: D) -> dict[str, Any]:
    """Every sauna session of a day, described — what Ember confirms after logging."""
    sessions = sauna_sessions(db, date, date)
    return {"date": date, "sessions": [{"id": s["id"], "text": describe_sauna(s)} for s in sessions]}


def _sauna_view(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row.pop("created_at", None)
    if isinstance(row.get("start_time"), datetime.time):
        row["start_time"] = row["start_time"].strftime("%H:%M")
    return row


# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------


def day_habits(db: Database, date: D) -> dict[str, bool]:
    return {r["habit"]: bool(r["done"]) for r in rows(db, "SELECT habit, done FROM habit_log WHERE date = ?", [date])}


def set_habits(
    db: Database, date: D, done: dict[str, bool], source: str, *, answer_all: bool = True
) -> dict[str, bool]:
    """Record habits for a day. With *answer_all*, habits not mentioned and not yet recorded become "not done":
    the day has been answered."""
    known = {h["key"] for h in definitions(db)}
    unknown = sorted(set(done) - known - set(day_habits(db, date)))
    if unknown:
        raise HabitError(f"unknown habit(s): {', '.join(unknown)} — the list is {', '.join(sorted(known)) or 'empty'}")
    current = day_habits(db, date)
    values = dict(done)
    if answer_all:
        values = {k: False for k in known if k not in current} | values
    for key, value in values.items():
        db.execute(
            "INSERT INTO habit_log (date, habit, done, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (date, habit) DO UPDATE SET done = excluded.done, source = excluded.source, "
            "updated_at = now()",
            [date, key, bool(value), source],
        )
    return day_habits(db, date)


def resolve_habit(db: Database, name: str) -> str:
    """A habit key from a key or a label as the athlete or Ember wrote it."""
    key = habit_key(name)
    keys = [h["key"] for h in definitions(db)]
    if key in keys:
        return key
    close = [k for k in keys if key and (k.startswith(key) or key.startswith(k))]
    if len(close) == 1:
        return close[0]
    raise HabitError(f"unknown habit “{name}” — the list is {', '.join(keys) or 'empty'} (Settings → Athlete)")


def clear_day(db: Database, date: D) -> None:
    db.execute("DELETE FROM habit_log WHERE date = ?", [date])


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def _outcomes(db: Database, start: D, end: D) -> dict[D, dict[str, float | None]]:
    out: dict[D, dict[str, float | None]] = {}
    for r in rows(db, "SELECT date, hrv_last_night_ms FROM hrv_daily WHERE date BETWEEN ? AND ?", [start, end]):
        out.setdefault(r["date"], {})["hrv_last_night_ms"] = r["hrv_last_night_ms"]
    for r in rows(db, "SELECT date, resting_hr FROM daily_health WHERE date BETWEEN ? AND ?", [start, end]):
        out.setdefault(r["date"], {})["resting_hr"] = r["resting_hr"]
    for r in rows(
        db, "SELECT date, sleep_score, total_sleep_sec FROM sleep_records WHERE date BETWEEN ? AND ?", [start, end]
    ):
        entry = out.setdefault(r["date"], {})
        entry["sleep_score"] = r["sleep_score"]
        entry["sleep_hours"] = round(r["total_sleep_sec"] / 3600, 2) if r["total_sleep_sec"] else None
    return out


def comparisons(db: Database, today: D, days: int = COMPARISON_DAYS) -> dict[str, Any]:
    """Next-morning HRV, resting HR and sleep after days with vs without each habit (and sauna)."""
    start = today - days * DAY
    outcomes = _outcomes(db, start, today)
    out: dict[str, Any] = {}
    saunas = sauna_sessions(db, start, today)
    first = rows(db, "SELECT min(date) AS d FROM sauna_sessions")[0]["d"]
    if first is not None:
        # Days since the log was started (before today: tonight hasn't happened yet) count as known.
        since = max(first, start)
        observed = [since + i * DAY for i in range((today - since).days)]
        out["sauna"] = next_morning_comparison({s["date"] for s in saunas}, observed, outcomes)
    log = rows(db, "SELECT date, habit, done FROM habit_log WHERE date >= ? AND date < ?", [start, today])
    for h in definitions(db):
        answered = [r["date"] for r in log if r["habit"] == h["key"]]
        if answered:
            done = {r["date"] for r in log if r["habit"] == h["key"] and r["done"]}
            out[h["key"]] = {"label": h["label"]} | next_morning_comparison(done, answered, outcomes)
    return out


def days(db: Database, start: D, end: D) -> list[dict[str, Any]]:
    """One entry per day, newest first: sauna sessions and habit answers (None = not answered)."""
    defs = definitions(db)
    saunas = sauna_sessions(db, start, end)
    log: dict[D, dict[str, bool]] = {}
    for r in rows(db, "SELECT date, habit, done FROM habit_log WHERE date BETWEEN ? AND ?", [start, end]):
        log.setdefault(r["date"], {})[r["habit"]] = bool(r["done"])
    out = []
    d = end
    while d >= start:
        answers = log.get(d, {})
        out.append(
            {
                "date": d,
                "sauna": [s for s in saunas if s["date"] == d],
                "answered": bool(answers),
                "habits": {h["key"]: answers.get(h["key"]) for h in defs},
            }
        )
        d -= DAY
    return out


def overview(db: Database, today: D, days_back: int = 14) -> dict[str, Any]:
    """What the Health page and ``get_habits`` show."""
    year = sauna_sessions(db, today - 365 * DAY, today)
    return {
        "definitions": definitions(db),
        "sauna_summary": sauna_summary(year, today),
        "days": days(db, today - (days_back - 1) * DAY, today),
        "recent_sauna": list(reversed(year))[:10],
    }


def recent_for_context(db: Database, d: D, days_back: int = CONTEXT_DAYS) -> dict[str, Any]:
    """Sauna and habits of the days before *d* (and *d* itself), for the daily suggestion."""
    entries = [e for e in days(db, d - days_back * DAY, d) if e["sauna"] or e["answered"]]
    labels = {h["key"]: h["label"] for h in definitions(db)}
    return {
        "days": [
            {
                "date": e["date"],
                "sauna": [
                    {k: s[k] for k in ("duration_min", "temperature_c", "rounds", "kind", "start_time") if s[k]}
                    for s in e["sauna"]
                ],
                "habits_done": [labels.get(k, k) for k, v in e["habits"].items() if v],
                "answered": e["answered"],
            }
            for e in entries
        ],
        "note": "Only logged days are listed; a missing day means unknown, not 'none'.",
    }


# ---------------------------------------------------------------------------
# Discord check-in (raw component payloads; discord_chat sends and answers them)
# ---------------------------------------------------------------------------

BUTTON_ROW, BUTTON = 1, 2
SECONDARY, SUCCESS = 2, 3
PREFIX = "hart"


def checkin_message(db: Database, date: D) -> dict[str, Any] | None:
    """The check-in for *date*; ``None`` when no habits are set up."""
    defs = definitions(db)
    if not defs:
        return None
    answers = day_habits(db, date)
    buttons: list[dict[str, Any]] = []
    for h in defs:
        done = answers.get(h["key"])
        buttons.append(
            {
                "type": BUTTON,
                "style": SUCCESS if done else SECONDARY,
                "label": (h["label"] + (" ✓" if done else ""))[:80],
                "custom_id": f"{PREFIX}:habit:{date.isoformat()}:{h['key']}",
            }
        )
    buttons.append(
        {"type": BUTTON, "style": SECONDARY, "label": "None of these", "custom_id": f"{PREFIX}:none:{date.isoformat()}"}
    )
    done_labels = [h["label"] for h in defs if answers.get(h["key"])]
    if done_labels:
        status = "Logged: " + ", ".join(done_labels)
    elif answers:
        status = "Logged: none of these"
    else:
        status = "Tap what you did — nothing is saved until you do."
    return {
        "content": f"**Habits · {date:%A} {date.day} {date:%b}**\n{status}",
        "components": [
            {"type": BUTTON_ROW, "components": buttons[i : i + 5]} for i in range(0, min(len(buttons), 25), 5)
        ],
    }


def ephemeral(text: str) -> dict[str, Any]:
    return {"type": 4, "data": {"content": text, "flags": 64}}


def handle_interaction(db: Database, kind: int, data: dict[str, Any]) -> dict[str, Any] | None:
    """The interaction response for a click (kind 3) on a check-in; None when the interaction isn't one of ours."""
    parts = str(data.get("custom_id") or "").split(":")
    if len(parts) < 3 or parts[0] != PREFIX:
        return None
    action = parts[1]
    try:
        date = D.fromisoformat(parts[2])
    except ValueError:
        return None
    if not definitions(db):
        return ephemeral("No habits are set up any more (Settings → Athlete).")
    update = lambda: {"type": 7, "data": checkin_message(db, date)}  # noqa: E731 — the refreshed check-in

    if kind == 3 and action == "habit" and len(parts) == 4:
        key = parts[3]
        if key not in {h["key"] for h in definitions(db)}:  # removed from the list since the message was sent
            return ephemeral("That habit isn't on your list any more (Settings → Athlete).")
        set_habits(db, date, {key: not day_habits(db, date).get(key, False)}, "discord")
        return update()
    if kind == 3 and action == "none":
        set_habits(db, date, {h["key"]: False for h in definitions(db)}, "discord")
        return update()
    return None
