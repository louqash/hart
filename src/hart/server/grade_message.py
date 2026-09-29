"""Discord message after a session is graded (Settings → Notifications).

Built by code from the stored grade — no Claude usage — and posted like the
evening message (the bot when there is one, otherwise the webhook). The bulk
"grade past sessions" backfill doesn't post: it would flood the channel.
"""

from __future__ import annotations

import logging
from typing import Any

from hart.config import HartSettings
from hart.server import evening, settings, state
from hart.server.data import one
from hart.storage.database import Database

logger = logging.getLogger(__name__)

SILENT_TRIGGERS = {"backfill"}
LETTER_COLORS = {"A": 0x6EB886, "B": 0x9CC27A, "C": 0xE2A45F, "D": 0xE27D60, "E": 0xE5604A}
DIMENSIONS = (("score_execution", "Execution"), ("score_response", "Response"), ("score_context", "Context fit"))


def should_send(db: Database, config: HartSettings, trigger: str) -> bool:
    return (
        trigger not in SILENT_TRIGGERS
        and evening.configured(config)
        and bool(settings.get(db, "grade_message_enabled"))
    )


def build(db: Database, config: HartSettings, activity_id: str) -> dict[str, Any] | None:
    from hart.server.grading import latest_grade

    grade = latest_grade(db, activity_id)
    activity = one(
        db,
        "SELECT name, sport_type, start_time, coalesce(moving_seconds, elapsed_seconds) AS duration_seconds, "
        "distance_meters, avg_hr "
        "FROM activities WHERE activity_id = ?",
        [activity_id],
    )
    if not grade or grade["status"] != "graded" or not activity:
        return None
    icon = evening.SPORT_ICONS.get(activity["sport_type"], "•")
    name = activity["name"] or activity["sport_type"].title()
    regraded = (grade.get("version") or 1) > 1
    previous = (
        one(
            db,
            "SELECT letter FROM session_grades WHERE activity_id = ? AND status = 'graded' AND version < ? "
            "ORDER BY version DESC LIMIT 1",
            [activity_id, grade["version"]],
        )
        if regraded
        else None
    )
    was = f", was {previous['letter']}" if previous and previous.get("letter") else ""
    title = f"{icon} {name} · {grade['letter']}" + (f" (re-graded{was})" if regraded else "")
    facts = [f"{activity['start_time']:%a %d %b}", evening._minutes(activity["duration_seconds"])]
    if activity.get("distance_meters"):
        facts.append(f"{activity['distance_meters'] / 1000:.1f} km")
    if activity.get("avg_hr"):
        facts.append(f"avg HR {activity['avg_hr']:.0f}")
    fields = [
        {"name": label, "value": f"{grade[key]}/5", "inline": True}
        for key, label in DIMENSIONS
        if grade.get(key) is not None
    ]
    if grade.get("highlights"):
        fields.append({"name": "👍 Went well", "value": "\n".join(f"• {h}" for h in grade["highlights"][:2])})
    if grade.get("concerns"):
        fields.append({"name": "👀 Watch", "value": "\n".join(f"• {c}" for c in grade["concerns"][:2])})
    embed: dict[str, Any] = {
        "title": evening._clip(title, 256),
        "description": evening._clip(grade.get("summary") or "", 4000),
        "color": LETTER_COLORS.get(grade["letter"], 0x5B7564),
        "fields": fields,
        "footer": {"text": " · ".join(facts) + (" · low confidence" if grade.get("confidence") == "low" else "")},
    }
    if config.server.public_host:
        embed["url"] = f"https://{config.server.public_host}/sessions/{activity_id}"
    return {"embeds": [embed]}


THREADS_KEY = "grade_messages"  # activity id → {"message_id", "thread_id"} of its first grade message


def send(db: Database, config: HartSettings, activity_id: str) -> str:
    """Post the grade; never fails the grading job (the grade is saved either way). The first grade of a
    session is a message in the channel; later ones (re-grades) go into a thread on it — with the bot;
    a webhook can't open threads, so there they're new messages marked as re-graded."""
    try:
        message = build(db, config, activity_id)
        if message is None:
            return "skipped"
        posted = state.get_setting(db, THREADS_KEY, {}) or {}
        first = posted.get(activity_id)
        bot = bool(config.discord.bot_token and config.discord.channel_id)
        if first and bot:
            thread_id = first.get("thread_id")
            if not thread_id:
                name = (message["embeds"][0]["title"].split(" · ")[0] + " · re-grades").strip()
                thread_id = evening.start_thread(config, first["message_id"], name)
                first["thread_id"] = thread_id
                state.set_setting(db, THREADS_KEY, posted)
            evening.send(config, message, channel_id=thread_id)
            return "sent (thread)"
        created = evening.send(config, message)
        if created.get("id"):
            posted[activity_id] = {"message_id": str(created["id"]), "thread_id": None}
            state.set_setting(db, THREADS_KEY, posted)
        return "sent"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Discord grade message for %s failed: %s", activity_id, exc)
        return f"failed: {exc}"[:200]
