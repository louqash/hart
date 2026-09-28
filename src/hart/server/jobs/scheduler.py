"""Time-based triggers.

A tick every 30 seconds decides which jobs are due.  Fired slots are kept in
memory; after a restart the startup check covers anything missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

from hart.server import state
from hart.server.jobs.pipeline import has_sleep_for
from hart.server.jobs.runner import JobRunner
from hart.storage.database import Database

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
# Times of day come from Settings → Schedule (hart.server.settings).
MORNING_WATCH_EVERY_MIN = 15
BACKUP_WINDOW_LENGTH = datetime.timedelta(hours=1)  # a restart inside the window still backs up
SWEEP_DAYS = 3
MAX_GRADE_FAILURES = 2


def ungraded_recent(db: Database, days: int = SWEEP_DAYS) -> list[str]:
    """Recent activities with no graded/ungraded result, fewer than 2 failed
    attempts, and no grading job queued or running."""
    return [r[0] for r in db.fetchall(
        "SELECT a.activity_id FROM activities a WHERE a.start_time >= current_date - ? * INTERVAL 1 DAY "
        "AND NOT EXISTS (SELECT 1 FROM session_grades g WHERE g.activity_id = a.activity_id "
        "  AND g.status IN ('graded', 'ungraded')) "
        "AND (SELECT count(*) FROM session_grades g WHERE g.activity_id = a.activity_id AND g.status = 'failed') < ? "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dedupe_key = 'grade:' || a.activity_id "
        "  AND j.status IN ('queued', 'running')) ORDER BY a.start_time",
        [days, MAX_GRADE_FAILURES],
    )]
STALE_AFTER = datetime.timedelta(minutes=60)
RECENT_SYNC = datetime.timedelta(minutes=20)


def last_successful_sync(db: Database) -> datetime.datetime | None:
    row = db.fetchone("SELECT max(finished_at) FROM jobs WHERE type = 'sync' AND status = 'ok'")
    return row[0] if row and row[0] is not None else None


def garmin_blocked(db: Database, now_utc: datetime.datetime) -> str | None:
    """Reason scheduled syncs are held back, or None."""
    if state.get_setting(db, state.GARMIN_AUTH_PAUSED):
        return "garmin_auth"
    until = state.get_setting(db, state.GARMIN_BACKOFF_UNTIL)
    if until and datetime.datetime.fromisoformat(until) > now_utc:
        return "garmin_rate_limited"
    return None


class Scheduler:
    def __init__(self, db: Database, runner: JobRunner, tz: str, config: Any = None) -> None:
        self._config = config
        self._db = db
        self._runner = runner
        self._tz = ZoneInfo(tz)
        self._fired: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.on_startup()
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick, datetime.datetime.now(tz=datetime.timezone.utc))
            except Exception:  # noqa: BLE001 — a bad tick must not kill the scheduler
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def _suggest(self, for_date: datetime.date, kind: str, trigger: str) -> None:
        self._runner.enqueue("suggest", {"date": for_date.isoformat(), "kind": kind, "trigger": trigger},
                             trigger="schedule", dedupe_key=f"suggest:{for_date}")

    def suggestions_due(self, local: datetime.datetime) -> list[str]:
        """Fallback for today's final suggestion (end of the morning check) and
        tomorrow's preliminary one (Settings → Schedule).

        Slots live in memory, so after a restart the first tick catches up on
        both — today's final suggestion is still made after a restart."""
        from hart.server import settings
        from hart.server.grading import claude_paused
        from hart.server.suggestions import FINAL_CUTOFF_HOUR, finals, preliminary_count

        fired: list[str] = []
        today = local.date()
        with contextlib.closing(self._db.cursor()) as cur:
            if claude_paused(cur):
                return fired
            fallback = settings.get_time(cur, "morning_watch_end")
            preliminary = settings.get_time(cur, "preliminary_time")
            slot = f"suggest_final:{today}"
            if fallback <= local.time() and local.hour < FINAL_CUTOFF_HOUR and slot not in self._fired:
                self._fired.add(slot)
                if not finals(cur, today):
                    self._suggest(today, "final", "fallback")
                    fired.append(slot)
            slot = f"suggest_prelim:{today}"
            tomorrow = today + datetime.timedelta(days=1)
            if local.time() >= preliminary and slot not in self._fired:
                self._fired.add(slot)
                if not preliminary_count(cur, tomorrow):
                    self._suggest(tomorrow, "preliminary", "schedule")
                    fired.append(slot)
        return fired

    def on_startup(self) -> None:
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        with contextlib.closing(self._db.cursor()) as cur:
            if state.is_demo(cur):
                return
            last = last_successful_sync(cur)
            if garmin_blocked(cur, now):
                return
        if last is None or now - last > STALE_AFTER:
            self._runner.enqueue("sync", {}, trigger="startup", dedupe_key="sync")
            # The startup sync stands in for this hour's scheduled one.
            local = now.astimezone(self._tz)
            self._fired.add(f"hourly:{local.date()}:{local.hour}")

    def tick(self, now_utc: datetime.datetime) -> list[str]:
        """Enqueue due jobs; returns the slots fired (for tests)."""
        local = now_utc.astimezone(self._tz)
        today = local.date()
        fired: list[str] = []
        with contextlib.closing(self._db.cursor()) as cur:
            if state.is_demo(cur):
                return fired

        sweep_slot = f"sweep:{today}:{local.hour}"
        if sweep_slot not in self._fired:
            self._fired.add(sweep_slot)
            with contextlib.closing(self._db.cursor()) as cur:
                from hart.server.grading import claude_paused

                if not claude_paused(cur):
                    for activity_id in ungraded_recent(cur):
                        self._runner.enqueue("grade", {"activity_id": activity_id, "trigger": "sweep"},
                                             trigger="schedule", dedupe_key=f"grade:{activity_id}")
                        fired.append(f"grade:{activity_id}")

        fired += self.suggestions_due(local)

        if self._config is not None:
            from hart.server import evening

            with contextlib.closing(self._db.cursor()) as cur:
                if evening.due(cur, self._config, local):
                    job = self._runner.enqueue("evening_message", {}, trigger="schedule", dedupe_key="evening_message")
                    if job.get("status") == "queued":
                        fired.append(f"evening:{today}")

        health_slot = f"health:{today}"
        if health_slot not in self._fired:
            # Reminder rules are cheap and deterministic: re-evaluate once a day.
            from hart.server.health import sync_checks

            self._fired.add(health_slot)
            with contextlib.closing(self._db.cursor()) as cur:
                sync_checks(cur, today)

        from hart.server import settings

        with contextlib.closing(self._db.cursor()) as cur:
            backup_at = settings.get_time(cur, "backup_time")
            morning = (settings.get_time(cur, "morning_watch_start"), settings.get_time(cur, "morning_watch_end"))
            hourly = (settings.get(cur, "hourly_first"), settings.get(cur, "hourly_last"))
        backup_end = (datetime.datetime.combine(today, backup_at) + BACKUP_WINDOW_LENGTH).time()
        backup_slot = f"backup:{today}"
        if backup_at <= local.time() < backup_end and backup_slot not in self._fired:
            self._runner.enqueue("backup", {}, trigger="schedule", dedupe_key="backup")
            self._fired.add(backup_slot)
            fired.append(backup_slot)

        with contextlib.closing(self._db.cursor()) as cur:
            if garmin_blocked(cur, now_utc):
                return fired

            start, end = morning
            if start <= local.time() < end:
                minutes = local.hour * 60 + local.minute - (start.hour * 60 + start.minute)
                slot = f"morning:{today}:{minutes // MORNING_WATCH_EVERY_MIN}"
                if slot not in self._fired and not has_sleep_for(cur, today):
                    self._runner.enqueue("sync_light", {}, trigger="schedule", dedupe_key="sync_light")
                    fired.append(slot)
                self._fired.add(slot)

            if hourly[0] <= local.hour <= hourly[1]:
                slot = f"hourly:{today}:{local.hour}"
                if slot not in self._fired:
                    last = last_successful_sync(cur)
                    if last is None or now_utc - last > RECENT_SYNC:
                        self._runner.enqueue("sync", {}, trigger="schedule", dedupe_key="sync")
                        fired.append(slot)
                    self._fired.add(slot)

        # Forget slots from previous days.
        self._fired = {s for s in self._fired if str(today) in s}
        return fired
