"""Job handlers: what each job type does."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from hart.config import HartSettings
from hart.server import state
from hart.server.data import TRAINING_SPORTS
from hart.server.jobs.backup import run_backup
from hart.server.jobs.pipeline import run_sync_pipeline
from hart.server.jobs.runner import JobFailed
from hart.storage.database import Database

if TYPE_CHECKING:
    from hart.server.jobs.runner import JobRunner

MANUAL_DAYS = 7
MAX_SYNC_DAYS = 365  # a history backfill (`hart sync all --days N`); Garmin rate-limits long runs
SCHEDULED_DAYS = 2
MAX_BACKOFF_HOURS = 6


def apply_garmin_state(db: Database, error_code: str | None) -> None:
    """Pause scheduled syncs on auth failure; back off on rate limits."""
    if error_code == "garmin_auth":
        state.set_setting(db, state.GARMIN_AUTH_PAUSED, True)
    elif error_code == "garmin_rate_limited":
        streak = int(state.get_setting(db, state.GARMIN_RATE_LIMIT_STREAK, 0)) + 1
        hours = min(2 ** (streak - 1), MAX_BACKOFF_HOURS)
        until = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=hours)
        state.set_setting(db, state.GARMIN_RATE_LIMIT_STREAK, streak)
        state.set_setting(db, state.GARMIN_BACKOFF_UNTIL, until.isoformat())
    elif error_code is None:
        state.delete_setting(db, state.GARMIN_AUTH_PAUSED)
        state.delete_setting(db, state.GARMIN_RATE_LIMIT_STREAK)
        state.delete_setting(db, state.GARMIN_BACKOFF_UNTIL)


def _fail_on_garmin_errors(result: dict[str, Any]) -> dict[str, Any]:
    code = result.get("error_code")
    if code == "garmin_auth":
        raise JobFailed("Garmin login expired — run `hart auth` on the laptop", result)
    if code == "garmin_rate_limited":
        raise JobFailed("Garmin rate limit hit — scheduled syncs back off", result)
    if code == "garmin_not_configured":
        raise JobFailed("Garmin credentials missing (GARMIN_EMAIL) — nothing was synced", result)
    return result


def make_handlers(config: HartSettings, runner_ref: dict[str, JobRunner]) -> dict[str, Any]:
    """Build the handler table.  *runner_ref* holds the runner once it exists,
    so handlers can chain follow-up jobs."""

    def enqueue_grades(activity_ids: list[str], trigger: str) -> None:
        for activity_id in activity_ids:
            runner_ref["runner"].enqueue(
                "grade",
                {"activity_id": activity_id, "trigger": trigger},
                trigger="chain",
                dedupe_key=f"grade:{activity_id}",
            )

    def enqueue_suggestion(for_date: datetime.date, kind: str, trigger: str) -> dict[str, Any]:
        return runner_ref["runner"].enqueue(
            "suggest",
            {"date": for_date.isoformat(), "kind": kind, "trigger": trigger},
            trigger="chain" if trigger == "sync" else trigger,
            dedupe_key=f"suggest:{for_date}",
        )

    def after_sync(db: Database, result: dict[str, Any], days: int) -> None:
        """Plan matching and suggestion chaining (spec steps 8–9)."""
        from hart.server import suggestions
        from hart.server.data import local_today
        from hart.server.plan import auto_match

        today = local_today(config)
        result["plan_matched"] = auto_match(db, [today - datetime.timedelta(days=i) for i in range(days + 1)])
        now = suggestions.local_now(config)
        if (
            result.get("sleep_today")
            and now.hour < suggestions.FINAL_CUTOFF_HOUR
            and suggestions.should_generate_final(db, today)
        ):
            result["suggestion"] = enqueue_suggestion(today, "final", "sync")["status"]
        tomorrow = today + datetime.timedelta(days=1)
        new_today = [
            a
            for a in result.get("new_activity_ids") or []
            if db.fetchone(
                "SELECT 1 FROM activities WHERE activity_id = ? AND CAST(start_time AS DATE) = ? AND sport_type IN "
                f"({', '.join('?' for _ in TRAINING_SPORTS)})",
                [a, today, *TRAINING_SPORTS],
            )
        ]
        # A session done today: tomorrow's suggestion now, rather than at the evening time — as soon as
        # today's plan is complete, and again (once) for a late extra session after it was made.
        count = suggestions.preliminary_count(db, tomorrow)
        if new_today and (count == 1 or (count == 0 and suggestions.today_done(db, today))):
            result["suggestion_tomorrow"] = enqueue_suggestion(tomorrow, "preliminary", "sync")["status"]

    def sync(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        days = payload.get("days") or (MANUAL_DAYS if payload.get("manual") else SCHEDULED_DAYS)
        days = min(int(days), MAX_SYNC_DAYS)
        result = run_sync_pipeline(db, config, health_days=days, activity_days=days)
        apply_garmin_state(db, result["error_code"])
        # New activities get graded (spec step 9), even after a partial sync — but a history backfill
        # grades only the last week, not months of old sessions (each grade is a Claude run).
        new_ids = result.get("new_activity_ids") or []
        if days > MANUAL_DAYS and new_ids:
            since = datetime.date.today() - datetime.timedelta(days=MANUAL_DAYS)
            new_ids = [
                a
                for a in new_ids
                if db.fetchone(
                    "SELECT 1 FROM activities WHERE activity_id = ? AND CAST(start_time AS DATE) >= ?", [a, since]
                )
            ]
        enqueue_grades(new_ids, "sync")

        # Edited in Garmin after the session was graded: grade it again on the final data.
        # RPE/feel from Garmin only counts when there's no feedback given in the app (it wins).
        def graded(a: str) -> bool:
            return (
                db.fetchone("SELECT 1 FROM session_grades WHERE activity_id = ? AND status = 'graded'", [a]) is not None
            )

        sets = [a for a in result.get("strength_edited_ids") or [] if graded(a)]
        effort = [
            a
            for a in result.get("effort_edited_ids") or []
            if a not in sets
            and graded(a)
            and db.fetchone(
                "SELECT 1 FROM session_feedback WHERE activity_id = ? AND (rpe IS NOT NULL OR feel IS NOT NULL)", [a]
            )
            is None
        ]
        described = [
            a
            for a in result.get("description_edited_ids") or []
            if a not in sets
            and a not in effort
            and graded(a)
            and db.fetchone("SELECT 1 FROM session_feedback WHERE activity_id = ? AND comment IS NOT NULL", [a]) is None
        ]  # a note written in hart wins, so a Garmin edit doesn't change what the grade read
        enqueue_grades(sets, "sets_edited")
        enqueue_grades(effort, "effort_edited")
        enqueue_grades(described, "description_edited")
        after_sync(db, result, days)
        return _fail_on_garmin_errors(result)

    def suggest(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        from hart.server.suggestions import SuggestionsPaused, generate

        claude = runner_ref.get("claude")
        if claude is None:
            raise JobFailed("Claude runner not available")
        try:
            return generate(
                db,
                datetime.date.fromisoformat(payload["date"]),
                payload.get("kind", "manual"),
                claude,
                runner_ref["runner"].loop,
                config,
                trigger=payload.get("trigger", "manual"),
            )
        except SuggestionsPaused as exc:
            raise JobFailed(str(exc)) from exc

    def grade(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        from hart.server.grading import GradingPaused, grade_activity

        claude = runner_ref.get("claude")
        if claude is None:
            raise JobFailed("Claude runner not available")
        from hart.server import grade_message

        trigger = payload.get("trigger", "manual")
        try:
            result = grade_activity(
                db,
                payload["activity_id"],
                claude,
                runner_ref["runner"].loop,
                config,
                trigger=trigger,
                force=bool(payload.get("force")),
            )
        except GradingPaused as exc:
            raise JobFailed(f"{exc} — the hourly sweep retries later") from exc
        if result.get("status") == "graded" and grade_message.should_send(db, config, trigger):
            result["discord"] = grade_message.send(db, config, payload["activity_id"])
        return result

    def plan_import(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        """Pasted coach text → the Plan page's parser → a proposal the athlete applies on the Plan page."""
        import asyncio

        from hart.server import plan, season_ops, settings
        from hart.server.data import local_today

        claude = runner_ref.get("claude")
        if claude is None:
            raise JobFailed("Claude runner not available")
        default = datetime.date.fromisoformat(payload["default_date"]) if payload.get("default_date") else None
        coro = plan.parse_paste(
            claude, settings.get(db, "model_parse"), payload["text"], local_today(config), default_date=default
        )
        try:
            parsed = asyncio.run_coroutine_threadsafe(coro, runner_ref["runner"].loop).result(timeout=200)
            return season_ops.create_plan_import(
                db,
                parsed["sessions"],
                payload.get("reason") or "Your coach's plan, pasted in a chat",
                parsed["warnings"],
            )
        except (plan.PlanError, season_ops.SeasonError) as exc:
            raise JobFailed(str(exc)) from exc

    def evening_message(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        from hart.server import evening

        try:
            return evening.run(db, config, test=bool(payload.get("test")))
        except evening.DiscordError as exc:
            raise JobFailed(str(exc)) from exc

    def garmin_workout(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        from hart.server import garmin_workouts
        from hart.server.data import local_today

        try:
            if payload.get("delete_workout_id"):
                return garmin_workouts.delete_remote(db, config, str(payload["delete_workout_id"]))
            claude = runner_ref.get("claude")
            if claude is None:
                raise JobFailed("Claude runner not available")
            return garmin_workouts.send(
                db, int(payload["planned_id"]), claude, runner_ref["runner"].loop, config, local_today(config)
            )
        except garmin_workouts.GarminError as exc:
            raise JobFailed(str(exc)) from exc

    def sync_light(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        result = run_sync_pipeline(db, config, light=True)
        apply_garmin_state(db, result["error_code"])
        _fail_on_garmin_errors(result)
        if result["sleep_today"]:
            # Last night's sleep arrived: escalate to the full pipeline.
            runner_ref["runner"].enqueue("sync", {"reason": "sleep arrived"}, trigger="chain", dedupe_key="sync")
        return result

    def backup(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        return run_backup(db, config.server.backup_dir, config.garmin_token_path)

    def vo2max_backfill(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        from hart.ingestion.sync_manager import SyncManager

        if not config.garmin.email:
            raise JobFailed("Garmin credentials missing (GARMIN_EMAIL) — nothing was fetched")

        def day(key: str) -> datetime.date | None:
            value = payload.get(key)
            return datetime.date.fromisoformat(value) if value else None

        return SyncManager(db, config).backfill_vo2max(start=day("start"), end=day("end"))

    def intervals_backfill(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        from hart.ingestion.sync_manager import SyncManager

        if not config.garmin.email:
            raise JobFailed("Garmin credentials missing (GARMIN_EMAIL) — nothing was fetched")
        return SyncManager(db, config).backfill_intervals(days=int(payload.get("days") or 60))

    def decoupling_backfill(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        from hart.ingestion.decoupling import backfill_decoupling
        from hart.storage.views import refresh_all_views

        result = backfill_decoupling(db, only_missing=bool(payload.get("only_missing")))
        refresh_all_views(db)  # weekly / efficiency views average decoupling
        return result

    return {
        "sync": sync,
        "sync_light": sync_light,
        "backup": backup,
        "vo2max_backfill": vo2max_backfill,
        "decoupling_backfill": decoupling_backfill,
        "intervals_backfill": intervals_backfill,
        "grade": grade,
        "suggest": suggest,
        "garmin_workout": garmin_workout,
        "evening_message": evening_message,
        "plan_import": plan_import,
    }
