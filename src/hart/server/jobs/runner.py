"""Persistent job queue.

Jobs are rows in the ``jobs`` table; the in-memory priority queue only holds
ids.  One worker executes jobs sequentially in a thread, each with its own
DuckDB cursor, so a sync never runs concurrently with another sync.

``enqueue`` is thread-safe: MCP tools run in worker threads and call it too.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import itertools
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from hart.storage.database import Database

logger = logging.getLogger(__name__)

Handler = Callable[[Database, dict[str, Any]], dict[str, Any] | None]

# Jobs run in lanes, one worker each, so a long Claude grading run never
# delays a Garmin sync (and vice versa).
LANES: dict[str, str] = {
    "grade": "claude",
    "suggest": "claude",
    "garmin_workout": "claude",
    "plan_import": "claude",
}  # everything else: "io"


# Jobs that reach Garmin, Claude or Discord: refused on a `hart demo` database.
DEMO_BLOCKED = frozenset(
    {
        "sync",
        "sync_light",
        "grade",
        "suggest",
        "garmin_workout",
        "evening_message",
        "vo2max_backfill",
        "intervals_backfill",
        "plan_import",
    }
)


def lane_of(job_type: str) -> str:
    return LANES.get(job_type, "io")


PRIORITY: dict[str, int] = {
    "sync": 0,
    "sync_light": 0,
    "plan_import": 0,  # Ember is waiting for it in a chat
    "suggest": 1,
    "garmin_workout": 1,
    "grade": 2,
    "backup": 3,
    "housekeeping": 3,
}

_JOB_COLUMNS = (
    "id, type, payload, status, attempts, error, result, trigger, dedupe_key, created_at, started_at, finished_at"
)


class JobFailed(Exception):
    """Raised by a handler to fail a job while still recording its result."""

    def __init__(self, message: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _job_row(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = [c.strip() for c in _JOB_COLUMNS.split(",")]
    job = dict(zip(keys, row))
    for key in ("payload", "result"):
        if isinstance(job.get(key), str):
            job[key] = json.loads(job[key])
    return job


class JobRunner:
    def __init__(self, db: Database, handlers: dict[str, Handler]) -> None:
        self._db = db
        self._handlers = handlers
        self._lock = threading.Lock()
        self._seq = itertools.count()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[str, asyncio.PriorityQueue[tuple[int, int, int]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._current: dict[str, asyncio.Future[None]] = {}
        self.on_finished: list[Callable[[dict[str, Any]], None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def _worker(self) -> asyncio.Task[None] | None:  # health check: the io lane is the critical one
        return self._workers.get("io")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queues = {"io": asyncio.PriorityQueue(), "claude": asyncio.PriorityQueue()}
        self._recover()
        for lane in self._queues:
            self._workers[lane] = asyncio.create_task(self._work(lane), name=f"job-runner-{lane}")

    async def stop(self, timeout: float = 20.0) -> None:
        for worker in self._workers.values():
            worker.cancel()
        for worker in self._workers.values():
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        for current in self._current.values():
            if not current.done():
                # A thread can't be cancelled; give the running job time to finish.
                # If it doesn't, the next startup marks it as interrupted.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(current), timeout)

    def _recover(self) -> None:
        """Mark jobs interrupted by a restart; re-queue jobs that never started."""
        with contextlib.closing(self._db.cursor()) as cur:
            # Duplicates used to be stored as 'skipped' rows; they carry no information.
            cur.execute("DELETE FROM jobs WHERE status = 'skipped'")
            cur.execute(
                "UPDATE jobs SET status = 'error', error = 'interrupted by restart', "
                "finished_at = ? WHERE status = 'running'",
                [_now()],
            )
            queued = cur.fetchall("SELECT id, type FROM jobs WHERE status = 'queued' ORDER BY id")
        for job_id, job_type in queued:
            self._queues[lane_of(job_type)].put_nowait((PRIORITY.get(job_type, 9), next(self._seq), job_id))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        trigger: str = "manual",
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a job.

        With *dedupe_key*, if a job with the same key is already queued or
        running, nothing is created: the result has ``status`` set to
        ``already_queued`` / ``already_running``, the existing ``job_id``
        (so callers can wait on it), and a ``message`` for the user.
        """
        if job_type not in self._handlers:
            raise ValueError(f"Unknown job type: {job_type}")
        with self._lock, contextlib.closing(self._db.cursor()) as cur:
            existing = None
            if dedupe_key:
                existing = cur.fetchone(
                    "SELECT id, type, status FROM jobs WHERE dedupe_key = ? "
                    "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
                    [dedupe_key],
                )
            if existing:
                existing_id, existing_type, existing_status = existing
                return {
                    "job_id": existing_id,
                    "status": f"already_{existing_status}",
                    "message": f"{existing_type} #{existing_id} is already {existing_status}",
                }
            job_id = cur.fetchone(
                "INSERT INTO jobs (type, payload, status, trigger, dedupe_key, created_at) "
                "VALUES (?, ?, 'queued', ?, ?, ?) RETURNING id",
                [job_type, _dumps(payload or {}), trigger, dedupe_key, _now()],
            )[0]

        if self._loop is None or not self._queues:
            raise RuntimeError("JobRunner is not started")
        item = (PRIORITY.get(job_type, 9), next(self._seq), job_id)
        self._loop.call_soon_threadsafe(self._queues[lane_of(job_type)].put_nowait, item)
        return {"job_id": job_id, "status": "queued"}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with contextlib.closing(self._db.cursor()) as cur:
            row = cur.fetchone(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", [job_id])
        return _job_row(row) if row else None

    def list_jobs(
        self,
        *,
        job_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if job_type:
            clauses.append("type = ?")
            params.append(job_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with contextlib.closing(self._db.cursor()) as cur:
            rows = cur.fetchall(
                f"SELECT {_JOB_COLUMNS} FROM jobs {where} ORDER BY id DESC LIMIT ?",
                [*params, limit],
            )
        return [_job_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def _work(self, lane: str) -> None:
        queue = self._queues[lane]
        while True:
            _, _, job_id = await queue.get()
            self._current[lane] = asyncio.ensure_future(asyncio.to_thread(self._run_job, job_id))
            try:
                await asyncio.shield(self._current[lane])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — _run_job records its own failures
                logger.exception("Job %s crashed the worker step", job_id)

    def _run_job(self, job_id: int) -> None:
        with contextlib.closing(self._db.cursor()) as cur:
            row = cur.fetchone(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", [job_id])
            if row is None:
                return
            job = _job_row(row)
            if job["status"] != "queued":
                return
            cur.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ? WHERE id = ?",
                [_now(), job_id],
            )
            status, error, result = "ok", None, None
            try:
                from hart.server import state

                if job["type"] in DEMO_BLOCKED and state.is_demo(cur):
                    raise JobFailed("Demo database: syncs, Claude runs and Garmin uploads are turned off")
                result = self._handlers[job["type"]](cur, job["payload"] or {})
            except JobFailed as exc:
                status, error, result = "error", str(exc), exc.result
            except Exception as exc:  # noqa: BLE001
                logger.exception("Job %s (%s) failed", job_id, job["type"])
                status, error = "error", f"{type(exc).__name__}: {exc}"
            cur.execute(
                "UPDATE jobs SET status = ?, error = ?, result = ?, finished_at = ? WHERE id = ?",
                [status, error, _dumps(result), _now(), job_id],
            )
            job.update(status=status, error=error, result=result)
        for hook in self.on_finished:
            try:
                hook(job)
            except Exception:  # noqa: BLE001
                logger.exception("on_finished hook failed for job %s", job_id)
