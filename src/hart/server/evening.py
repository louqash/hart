"""Evening message to Discord (time set in Settings → Notifications, 22:00 by default).

Built by code from what already exists — tomorrow's suggestion (made at
20:00, refreshed after late sessions or coach changes), the coach's plan,
today's sessions and grades, open alerts — so it costs no Claude usage.
Sent once a day; a restart after 22:00 doesn't send it twice.
"""

from __future__ import annotations

import datetime
from typing import Any

from hart.config import HartSettings
from hart.server import state
from hart.server.data import DAY, TRAINING_SPORTS, alerts, local_today, readiness_on, recent_sessions, rows

SENT_KEY = "evening_message_sent_on"
FAILED_KEY = "evening_message_failed_at"
RETRY_AFTER = datetime.timedelta(minutes=30)
WAIT_FOR_SUGGESTION = datetime.timedelta(minutes=30)
REC_LABELS = {"as_planned": "as planned", "modify": "modify the coach's session", "replace": "replace it",
              "rest": "rest", "free_choice": "no coach plan — free choice"}


class DiscordError(Exception):
    pass


def configured(config: HartSettings) -> bool:
    d = config.discord
    return bool(d.webhook_url or (d.bot_token and d.channel_id))


def send(config: HartSettings, text: str) -> None:
    import httpx

    d = config.discord
    if d.webhook_url:
        response = httpx.post(d.webhook_url, json={"content": text[:2000], "allowed_mentions": {"parse": []}},
                              timeout=10.0)
    elif d.bot_token and d.channel_id:
        response = httpx.post(f"https://discord.com/api/v10/channels/{d.channel_id}/messages",
                              headers={"Authorization": f"Bot {d.bot_token}"},
                              json={"content": text[:2000], "allowed_mentions": {"parse": []}}, timeout=10.0)
    else:
        raise DiscordError("Discord isn't configured (set HART_DISCORD_WEBHOOK_URL)")
    if response.status_code not in (200, 201, 204):
        raise DiscordError(f"Discord answered {response.status_code}: {response.text[:200]}")


def _minutes(seconds: float | None) -> str:
    m = round((seconds or 0) / 60)
    return f"{m // 60}:{m % 60:02d} h" if m >= 60 else f"{m}′"


def build(db: Any, config: HartSettings) -> str:
    from hart.server.suggestions import coach_plan_for, for_display, race_on

    today = local_today(config)
    tomorrow = today + DAY
    base = f"https://{config.server.public_host}" if config.server.public_host else ""
    lines = [f"**🌲 Tomorrow — {tomorrow:%A %d %b}**"]

    race = race_on(db, tomorrow)
    if race:
        lines.append(f"🏁 Race day — **{race['name']}**. Trust the taper.")
    else:
        coach = coach_plan_for(db, tomorrow)
        if coach:
            lines.append("**Coach:** " + "; ".join(
                f"{r['title']}" + (f" ({r['duration_min']}′)" if r["duration_min"] else "")
                + (" — replaced by the accepted suggestion" if r["replaced_by"] else "") for r in coach))
        s = for_display(db, tomorrow)
        if s and s["status"] == "ok":
            lines.append(f"**Suggestion ({REC_LABELS.get(s['recommendation'], s['recommendation'])}):** {s['summary']}")
            garmin = {r["title"]: r["garmin_status"] for r in rows(
                db, "SELECT title, garmin_status FROM planned_sessions WHERE date = ? AND suggestion_id = ?",
                [tomorrow, s["id"]])}
            for x in s.get("sessions") or []:
                tag = " · on Garmin ✓" if garmin.get(x["title"][:120]) == "sent" else ""
                lines.append(f"• {x['title']} — {x['duration_min']}′ {x['intensity']}{tag}")
            for c in (s.get("cautions") or [])[:2]:
                lines.append(f"⚠️ {c}")
        elif s and s["status"] == "failed":
            lines.append("Suggestion couldn't be made — check the Plan page.")
        else:
            lines.append("No suggestion for tomorrow yet.")

    done = [x for x in recent_sessions(db, today, today) if x["sport_type"] in TRAINING_SPORTS]
    if done:
        parts = []
        for x in sorted(done, key=lambda x: x["start_time"]):
            grade = (x.get("grade") or {}).get("letter")
            parts.append(f"{x['name'] or x['sport_type']} {_minutes(x['duration_s'])}" + (f" · {grade}" if grade else ""))
        lines.append("**Today:** " + "; ".join(parts))
    else:
        lines.append("**Today:** no training logged.")

    readiness = readiness_on(db, today, state.get_thresholds(db))
    if readiness["level"] != "unknown":
        lines.append(f"Readiness today was {readiness['level']}.")

    al = alerts(db, config)
    warnings = []
    if al["garmin_blocked"] == "garmin_auth":
        warnings.append("Garmin login expired — run `hart auth` on the laptop")
    if al.get("health_overdue"):
        warnings.append(f"{al['health_overdue']} health check(s) due")
    if al.get("proposed_notes"):
        warnings.append(f"{al['proposed_notes']} note(s) waiting for approval")
    if (al.get("claude") or {}).get("token_warning"):
        warnings.append(f"Claude token expires {al['claude']['token_expires']}")
    if warnings:
        lines.append("🔔 " + " · ".join(warnings))
    if base:
        lines.append(f"<{base}/plan>")
    return "\n".join(lines)


def due(db: Any, config: HartSettings, now_local: datetime.datetime) -> bool:
    """After 22:00, once per day, when Discord is set up and the message is on.
    Waits (until 22:30) for tomorrow's suggestion if it's still being written."""
    from hart.server.suggestions import pending

    from hart.server import settings

    if not configured(config) or not settings.get(db, "evening_message_enabled"):
        return False
    send_from = settings.get_time(db, "evening_message_time")
    if now_local.time() < send_from or state.get_setting(db, SENT_KEY) == now_local.date().isoformat():
        return False
    failed = state.get_setting(db, FAILED_KEY)
    if failed and now_local - datetime.datetime.fromisoformat(failed) < RETRY_AFTER:
        return False
    wait_until = (datetime.datetime.combine(now_local.date(), send_from) + WAIT_FOR_SUGGESTION).time()
    return not (pending(db, now_local.date() + DAY) and now_local.time() < wait_until)


def run(db: Any, config: HartSettings, *, test: bool = False) -> dict[str, Any]:
    text = build(db, config)
    try:
        send(config, ("🧪 Test — " if test else "") + text)
    except Exception:  # retried after RETRY_AFTER, not every scheduler tick
        if not test:
            now = datetime.datetime.now(datetime.timezone.utc).astimezone()
            state.set_setting(db, FAILED_KEY, now.isoformat())
        raise
    if not test:
        state.set_setting(db, SENT_KEY, local_today(config).isoformat())
    return {"sent": True, "chars": len(text)}
