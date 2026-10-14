"""Evening message to Discord (time set in Settings → Notifications, 22:00 by default).

Built by code from what already exists, so it costs no Claude usage. Two embeds:
the day's summary (every session with duration, distance, heart rate, power,
load and grade; sauna; last night's sleep and HRV, resting HR, steps; form;
habits answered in the check-in, sent earlier — see run_checkin) and tomorrow (the suggestion — made at 20:00, refreshed after
late sessions or coach changes — the coach's plan, open alerts).
Sent once a day; a restart after 22:00 doesn't send it twice.
"""

from __future__ import annotations

import datetime
from typing import Any

from hart.config import HartSettings
from hart.server import state
from hart.server.data import DAY, TRAINING_SPORTS, alerts, local_today, one, readiness_on, recent_sessions, rows

SENT_KEY = "evening_message_sent_on"
FAILED_KEY = "evening_message_failed_at"
RETRY_AFTER = datetime.timedelta(minutes=30)
WAIT_FOR_SUGGESTION = datetime.timedelta(minutes=30)
# How the suggestion relates to the coach's plan, shown above the summary.
REC_LABELS = {
    "as_planned": "✅ **As planned** — the coach's session fits",
    "modify": "✏️ **Adjusted** — the coach's session, changed",
    "replace": "🔄 **Replaced** — something else instead of the coach's session",
    "rest": "🛌 **Rest**",
    "free_choice": "🧭 **Free choice** — no coach session",
}
SPORT_ICONS = {"swim": "🏊", "bike": "🚴", "run": "🏃", "strength": "🏋️", "other": "•"}
READINESS = {  # embed colour (the app's palette) and label
    "green": (0x6EB886, "🟢 Green"),
    "amber": (0xE2A45F, "🟠 Amber"),
    "red": (0xE5604A, "🔴 Red"),
    "unknown": (0x5B7564, "⚪ Unknown"),
}


class DiscordError(Exception):
    pass


def configured(config: HartSettings) -> bool:
    d = config.discord
    return bool(d.webhook_url or (d.bot_token and d.channel_id))


API = "https://discord.com/api/v10"


def send(config: HartSettings, message: dict[str, Any], *, channel_id: int | str | None = None) -> dict[str, Any]:
    """Post a message (``content`` and/or ``embeds``) as the bot when there is one — the same Ember that
    answers replies in Discord — otherwise through the webhook. *channel_id* (bot only) posts into another
    channel or a thread. Returns the created message (with its ``id``) when Discord sends it back."""
    import httpx

    body = {**message, "allowed_mentions": {"parse": []}}
    if "content" in body:
        body["content"] = body["content"][:2000]
    d = config.discord
    if d.bot_token and d.channel_id:
        response = httpx.post(
            f"{API}/channels/{channel_id or d.channel_id}/messages",
            headers={"Authorization": f"Bot {d.bot_token}"},
            json=body,
            timeout=10.0,
        )
    elif d.webhook_url:
        identity = {"username": d.username, "avatar_url": d.avatar_url}
        response = httpx.post(
            d.webhook_url,
            params={"wait": "true"},  # returns the message, like the bot API
            json={**body, **{k: v for k, v in identity.items() if v}},
            timeout=10.0,
        )
    else:
        raise DiscordError(
            "Discord isn't configured (set DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID, or HART_DISCORD_WEBHOOK_URL)"
        )
    if response.status_code not in (200, 201, 204):
        raise DiscordError(f"Discord answered {response.status_code}: {response.text[:200]}")
    try:
        return response.json() if response.status_code != 204 else {}
    except (ValueError, AttributeError):
        return {}


def start_thread(config: HartSettings, message_id: str, name: str) -> str:
    """Open a thread on one of the bot's messages (bot only; webhooks can't); returns the thread's id."""
    import httpx

    d = config.discord
    response = httpx.post(
        f"{API}/channels/{d.channel_id}/messages/{message_id}/threads",
        headers={"Authorization": f"Bot {d.bot_token}"},
        json={"name": name[:100], "auto_archive_duration": 10080},
        timeout=10.0,
    )
    if response.status_code not in (200, 201):
        raise DiscordError(f"Discord answered {response.status_code}: {response.text[:200]}")
    return str(response.json()["id"])


def edit(config: HartSettings, channel_id: str, message_id: str, message: dict[str, Any]) -> None:
    """Replace one of the bot's messages (bot only) — e.g. to take its buttons away once they're used up."""
    import httpx

    d = config.discord
    response = httpx.patch(
        f"{API}/channels/{channel_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {d.bot_token}"},
        json={**message, "allowed_mentions": {"parse": []}},
        timeout=10.0,
    )
    if response.status_code not in (200, 201):
        raise DiscordError(f"Discord answered {response.status_code}: {response.text[:200]}")


def _minutes(seconds: float | None) -> str:
    m = round((seconds or 0) / 60)
    return f"{m // 60}:{m % 60:02d} h" if m >= 60 else f"{m}′"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _distance(sport: str, meters: float | None) -> str | None:
    if not meters:
        return None
    return f"{meters:,.0f} m".replace(",", " ") if sport == "swim" else f"{meters / 1000:.1f} km"


def session_line(x: dict[str, Any]) -> str:
    """``🚴 **Endurance ride** · 1:02 h · 28.4 km · ♥ 128 · 182 W · load 54 · **B**``"""
    parts = [f"{SPORT_ICONS.get(x['sport_type'], '•')} **{x['name'] or x['sport_type']}**", _minutes(x["duration_s"])]
    parts.append(_distance(x["sport_type"], x.get("distance_meters")) or "")
    if x.get("avg_hr"):
        parts.append(f"♥ {x['avg_hr']:.0f}")
    power = x.get("normalized_power") or x.get("avg_power")
    if x["sport_type"] == "bike" and power:
        parts.append(f"{power:.0f} W")
    if x.get("load"):
        parts.append(f"load {x['load']:.0f}")
    grade = (x.get("grade") or {}).get("letter")
    if grade:
        parts.append(f"**{grade}**")
    return " · ".join(p for p in parts if p)


def day_summary(db: Any, config: HartSettings, day: datetime.date) -> dict[str, Any]:
    """The day as a Discord embed: sessions, sauna, body numbers, form, habits."""
    from hart.server import habits

    readiness = readiness_on(db, day, state.get_thresholds(db))
    sessions = sorted(
        (x for x in recent_sessions(db, day, day) if x["sport_type"] in TRAINING_SPORTS),
        key=lambda x: x["start_time"],
    )
    lines = [session_line(x) for x in sessions] or ["Rest day — no training"]
    if len(sessions) > 1:
        total_s = sum(x["duration_s"] or 0 for x in sessions)
        total_load = sum(x.get("load") or 0 for x in sessions)
        lines.append(
            f"Σ {_plural(len(sessions), 'session')} · {_minutes(total_s)}"
            + (f" · load {total_load:.0f}" if total_load else "")
        )
    saunas = habits.sauna_sessions(db, day, day)
    if saunas:
        lines.append("")
        lines += [f"🧖 Sauna · {habits.describe_sauna(x)}" for x in saunas]

    fields: list[dict[str, Any]] = []
    sleep = one(db, "SELECT total_sleep_sec, sleep_score FROM sleep_records WHERE date = ?", [day]) or {}
    hrv = one(db, "SELECT hrv_last_night_ms FROM hrv_daily WHERE date = ?", [day]) or {}
    night = []
    if sleep.get("total_sleep_sec"):
        night.append(
            _minutes(sleep["total_sleep_sec"])
            + (f" · score {sleep['sleep_score']}" if sleep.get("sleep_score") else "")
        )
    if hrv.get("hrv_last_night_ms"):
        night.append(f"HRV {hrv['hrv_last_night_ms']:.0f} ms")
    if night:
        fields.append({"name": "😴 Last night", "value": "\n".join(night), "inline": True})
    health = one(db, "SELECT resting_hr, steps FROM daily_health WHERE date = ?", [day]) or {}
    body = []
    if health.get("resting_hr"):
        body.append(f"Resting HR {health['resting_hr']:.0f}")
    if health.get("steps"):
        body.append(f"{health['steps']:,} steps".replace(",", " "))
    if body:
        fields.append({"name": "❤️ Body", "value": "\n".join(body), "inline": True})
    load = one(db, "SELECT ctl, atl, tsb FROM daily_training_load WHERE sport_type = 'combined' AND date = ?", [day])
    if load and load.get("ctl") is not None:
        fields.append(
            {
                "name": "📈 Form",
                "value": f"Fitness {load['ctl']:.0f} · Fatigue {load['atl']:.0f}\nForm {load['tsb']:+.0f}",
                "inline": True,
            }
        )
    if readiness["level"] != "unknown":
        fields.append({"name": "Readiness this morning", "value": READINESS[readiness["level"]][1], "inline": True})
    answers = habits.day_habits(db, day)
    if answers:
        done = [h["label"] for h in habits.definitions(db) if answers.get(h["key"])]
        fields.append({"name": "✅ Habits", "value": ", ".join(done) or "None of them", "inline": True})
    elif state.get_setting(db, CHECKIN_SENT_KEY) == day.isoformat():
        fields.append(
            {"name": "✅ Habits", "value": "Not answered yet — the check-in's buttons still work", "inline": True}
        )

    embed: dict[str, Any] = {
        "title": f"📒 Today · {day:%A} {day.day} {day:%b}",
        "description": _clip("\n".join(lines), 4000),
        "color": READINESS.get(readiness["level"], READINESS["unknown"])[0],
        "fields": fields[:25],
    }
    if config.server.public_host:
        embed["url"] = f"https://{config.server.public_host}/sessions"
    return embed


def build(db: Any, config: HartSettings) -> dict[str, Any]:
    """The evening message: today's summary, then tomorrow's plan with the alerts."""
    from hart.server.suggestions import coach_plan_for, for_display, race_on

    today = local_today(config)
    tomorrow = today + DAY
    base = f"https://{config.server.public_host}" if config.server.public_host else ""
    readiness = readiness_on(db, today, state.get_thresholds(db))
    colour = READINESS.get(readiness["level"], READINESS["unknown"])[0]
    fields: list[dict[str, Any]] = []
    title = f"🌲 Tomorrow · {tomorrow:%A} {tomorrow.day} {tomorrow:%b}"
    description = ""

    race = race_on(db, tomorrow)
    if race:
        title = f"🏁 Race day tomorrow · {race['name']}"
        description = "Trust the taper. Sleep, fuel, and enjoy it."
    else:
        coach = coach_plan_for(db, tomorrow)
        s = for_display(db, tomorrow)
        if s and s["status"] == "ok":
            description = REC_LABELS.get(s["recommendation"], s["recommendation"]) + "\n\n" + s["summary"]
            colour = READINESS.get(s.get("readiness") or "", (colour,))[0]
            garmin = {
                r["title"]: r["garmin_status"]
                for r in rows(
                    db,
                    "SELECT title, garmin_status FROM planned_sessions WHERE date = ? AND suggestion_id = ?",
                    [tomorrow, s["id"]],
                )
            }
            for x in (s.get("sessions") or [])[:4]:
                value = f"{x['duration_min']}′ · {x['intensity']}"
                if garmin.get(x["title"][:120]) == "sent":
                    value += " · on Garmin ✓"
                fields.append(
                    {"name": _clip(f"{SPORT_ICONS.get(x['sport_type'], '•')} {x['title']}", 256), "value": value}
                )
            if coach and s["recommendation"] in ("modify", "replace"):
                fields.append(
                    {
                        "name": "📋 Coach's plan",
                        "value": "\n".join(
                            (f"~~{r['title']}~~" if r["replaced_by"] else r["title"])
                            + (f" · {r['duration_min']}′" if r["duration_min"] else "")
                            for r in coach
                        ),
                    }
                )
            take = (s.get("context") or {}).get("coach_take") or {}
            if take.get("stance") in ("partly", "disagree") and take.get("text"):
                fields.append({"name": "🗣️ Ember's take on the coach's plan", "value": _clip(take["text"], 1024)})
            cautions = (s.get("cautions") or [])[:2]
            if cautions:
                fields.append({"name": "⚠️ Watch out", "value": "\n".join(f"• {c}" for c in cautions)})
        else:
            if coach:
                fields.append(
                    {
                        "name": "📋 Coach's plan",
                        "value": "\n".join(
                            r["title"] + (f" · {r['duration_min']}′" if r["duration_min"] else "") for r in coach
                        ),
                    }
                )
            description = (
                "Tomorrow's suggestion couldn't be made — check the Plan page."
                if s and s["status"] == "failed"
                else "No suggestion for tomorrow yet."
            )

    al = alerts(db, config)
    notices = []
    if al["garmin_blocked"] == "garmin_auth":
        notices.append("Garmin login expired — run hart auth on the laptop")
    problem = (al.get("claude") or {}).get("problem") or {}
    if problem.get("status") == "auth_failed":
        notices.append("Claude can't sign in — renew the token (claude setup-token)")
    if (al.get("claude") or {}).get("token_warning"):
        notices.append(f"Claude token expires {al['claude']['token_expires']}")
    if al.get("health_overdue"):
        notices.append(_plural(al["health_overdue"], "health check") + " due")
    if al.get("proposed_notes"):
        notices.append(_plural(al["proposed_notes"], "note") + " waiting for approval")

    embed: dict[str, Any] = {"title": _clip(title, 256), "color": colour, "fields": fields[:25]}
    if description:
        embed["description"] = _clip(description, 4000)
    if base:
        embed["url"] = f"{base}/plan"
    if notices:
        embed["footer"] = {"text": "🔔 " + " · ".join(notices)}
    return {"embeds": [day_summary(db, config, today), embed]}


def due(db: Any, config: HartSettings, now_local: datetime.datetime) -> bool:
    """After 22:00, once per day, when Discord is set up and the message is on.
    Waits (until 22:30) for tomorrow's suggestion if it's still being written."""
    from hart.server import settings
    from hart.server.suggestions import pending

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
    message = build(db, config)
    if test:
        message["content"] = "🧪 Test message — the real one arrives in the evening."
    try:
        send(config, message)
    except Exception:  # retried after RETRY_AFTER, not every scheduler tick
        if not test:
            now = datetime.datetime.now(datetime.UTC).astimezone()
            state.set_setting(db, FAILED_KEY, now.isoformat())
        raise
    if not test:
        state.set_setting(db, SENT_KEY, local_today(config).isoformat())
    return {"sent": True}


# ---------------------------------------------------------------------------
# Habit check-in (its own time, before the evening message so the summary includes the answers)
# ---------------------------------------------------------------------------

CHECKIN_SENT_KEY = "habit_checkin_sent_on"
CHECKIN_FAILED_KEY = "habit_checkin_failed_at"


def checkin_configured(config: HartSettings) -> bool:
    """Buttons need the bot: a webhook can't carry them."""
    return bool(config.discord.bot_token and config.discord.channel_id)


def checkin_due(db: Any, config: HartSettings, now_local: datetime.datetime) -> bool:
    """After the check-in time, once per day, when the bot is set up, the check-in is on and there are habits."""
    from hart.server import habits, settings

    if not checkin_configured(config) or not settings.get(db, "habit_checkin_enabled"):
        return False
    if now_local.time() < settings.get_time(db, "habit_checkin_time"):
        return False
    if state.get_setting(db, CHECKIN_SENT_KEY) == now_local.date().isoformat():
        return False
    failed = state.get_setting(db, CHECKIN_FAILED_KEY)
    if failed and now_local - datetime.datetime.fromisoformat(failed) < RETRY_AFTER:
        return False
    return bool(habits.definitions(db))


def run_checkin(db: Any, config: HartSettings) -> dict[str, Any]:
    from hart.server import habits

    message = habits.checkin_message(db, local_today(config))
    if message is None:
        return {"sent": False, "reason": "no habits set up"}
    try:
        send(config, message)
    except Exception:  # retried after RETRY_AFTER
        state.set_setting(db, CHECKIN_FAILED_KEY, datetime.datetime.now(datetime.UTC).astimezone().isoformat())
        raise
    state.set_setting(db, CHECKIN_SENT_KEY, local_today(config).isoformat())
    return {"sent": True}
