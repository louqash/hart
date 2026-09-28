"""Nightly database backups to the NAS.

``EXPORT DATABASE`` writes a directory of Parquet files plus the schema.  It
goes to ``<name>.partial`` first and is renamed only on success, so a failed
run never leaves something that looks like a good backup.

The backup directory is an NFS share.  If it isn't mounted, Docker's bind
mount would silently point at an empty local directory, so the job requires
a marker file (``.hart-backups``) and fails loudly without it.
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path
from typing import Any

from hart.server.jobs.runner import JobFailed
from hart.storage.database import Database

MARKER = ".hart-backups"
KEEP_DAILY = 14
KEEP_WEEKLY = 8  # Sunday backups beyond the daily window


def _backup_date(path: Path) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(path.name)
    except ValueError:
        return None


def prune_backups(backup_dir: Path, today: datetime.date) -> list[str]:
    """Keep the last KEEP_DAILY days, plus Sundays for KEEP_WEEKLY weeks."""
    removed: list[str] = []
    daily_cutoff = today - datetime.timedelta(days=KEEP_DAILY)
    weekly_cutoff = today - datetime.timedelta(weeks=KEEP_WEEKLY)
    for path in sorted(backup_dir.iterdir()):
        if path.name.endswith(".partial") and path.is_dir():
            shutil.rmtree(path)  # leftovers from a crashed run
            removed.append(path.name)
            continue
        day = _backup_date(path)
        if day is None or not path.is_dir():
            continue
        keep = day > daily_cutoff or (day.weekday() == 6 and day > weekly_cutoff)
        if not keep:
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


def run_backup(
    db: Database,
    backup_dir: Path | None,
    garmin_token_dir: Path,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    if backup_dir is None:
        raise JobFailed("HART_BACKUP_DIR is not configured")
    if not (backup_dir / MARKER).is_file():
        raise JobFailed(
            f"Backup target not available: {backup_dir / MARKER} missing (is the share mounted? "
            "create the empty marker file once to confirm the directory)"
        )

    today = today or datetime.date.today()
    final = backup_dir / today.isoformat()
    partial = backup_dir / f"{today.isoformat()}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()

    db.execute("CHECKPOINT")
    target = str(partial / "db").replace("'", "''")
    db.execute(f"EXPORT DATABASE '{target}' (FORMAT parquet)")
    if garmin_token_dir.is_dir():
        shutil.copytree(garmin_token_dir, partial / "garmin_tokens")

    if final.exists():
        shutil.rmtree(final)  # a manual backup earlier the same day
    partial.rename(final)

    size = sum(p.stat().st_size for p in final.rglob("*") if p.is_file())
    return {"path": str(final), "bytes": size, "pruned": prune_backups(backup_dir, today)}
