"""First-run seeding from the seed directory (``HART_SEED_DIR``, default ``data/seed``).

All files are optional (see ``examples/seed`` and docs/configuration.md):

* ``races.json`` — ``[{name, race_date, distance, priority, notes?}]``
* ``annotations.json`` — ``[{kind, label, start_date, end_date?}]``
* ``athlete_notes.json`` — ``[{category, title, body, valid_from?, valid_to?, rules?}]``
* ``lab_results.json`` — ``[{date, markers: [{name, value, unit?, reference_range?, flag?}]}]``

Races and annotations are seeded once (deleting them later sticks); seeded
notes start as ``proposed`` and become active after approval; lab results
are imported idempotently on every start.  A long training break found in the
data is added as a detected annotation.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from hart.analytics.phase import detect_layoffs
from hart.server import state
from hart.storage.database import Database

logger = logging.getLogger(__name__)

SEASON_DETECTION_KEY = "season_detection"



def _read(seed_dir: Path, name: str) -> list[dict[str, Any]]:
    path = seed_dir / name
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return data


def _is_empty(db: Database, table: str) -> bool:
    return db.fetchone(f"SELECT count(*) FROM {table}")[0] == 0


RACES_SEEDED = "seeded_races"
ANNOTATIONS_SEEDED = "seeded_annotations"


def _seed_once(db: Database, key: str, table: str) -> bool:
    """True the first time only.  An existing database with rows counts as
    seeded, so deleting every race or annotation later sticks."""
    if state.get_setting(db, key) or not _is_empty(db, table):
        state.set_setting(db, key, True)
        return False
    state.set_setting(db, key, True)
    return True


def seed_races(db: Database, seed_dir: Path) -> int:
    races = _read(seed_dir, "races.json")
    if not races or not _seed_once(db, RACES_SEEDED, "races"):
        return 0
    for r in races:
        db.execute(
            "INSERT INTO races (name, race_date, distance, priority, notes) VALUES (?, ?, ?, ?, ?)",
            [r["name"], r["race_date"], r.get("distance", "other"), r.get("priority", "B"), r.get("notes")],
        )
    return len(races)


def seed_annotations(db: Database, seed_dir: Path | None = None, today: datetime.date | None = None) -> dict[str, Any]:
    """Annotations from the seed file plus the detected training break and no-watch period."""
    detected = detect_layoffs(db, today=today)
    summary = detected.as_dict()
    state.set_setting(db, SEASON_DETECTION_KEY, summary)
    if not _seed_once(db, ANNOTATIONS_SEEDED, "annotations"):
        return {"inserted": 0, "detected": summary}

    rows: list[tuple[str, str, datetime.date, datetime.date | None, str]] = []
    if detected.layoff:
        rows.append((
            "other",
            "Training break",
            detected.layoff.last_activity_date,
            detected.layoff.return_date - datetime.timedelta(days=1),
            "detected",
        ))
    if detected.no_device:
        rows.append(("no_device", "Watch not worn", detected.no_device[0], detected.no_device[1], "detected"))
    for a in _read(seed_dir, "annotations.json") if seed_dir else []:
        rows.append((a["kind"], a["label"], a["start_date"], a.get("end_date"), "manual"))

    for row in rows:
        db.execute(
            "INSERT INTO annotations (kind, label, start_date, end_date, source) VALUES (?, ?, ?, ?, ?)",
            list(row),
        )
    return {"inserted": len(rows), "detected": summary}


def refresh_proposed_seed_notes(db: Database, notes: list[dict[str, Any]]) -> int:
    """Apply seed-file edits to seed notes that are still awaiting approval.

    Matched by title.  Notes that were approved, archived, or edited (edits
    set ``source`` to ``manual``) are never touched.
    """
    updated = 0
    for note in notes:
        rules = json.dumps(note["rules"]) if note.get("rules") else None
        row = db.fetchone(
            "SELECT id FROM athlete_notes WHERE title = ? AND source = 'seed' AND status = 'proposed' "
            "AND (body IS DISTINCT FROM ? OR CAST(rules AS VARCHAR) IS DISTINCT FROM ? "
            "OR valid_from IS DISTINCT FROM CAST(? AS DATE) OR valid_to IS DISTINCT FROM CAST(? AS DATE))",
            [note["title"], note["body"], rules, note.get("valid_from"), note.get("valid_to")],
        )
        if row is None:
            continue
        db.execute(
            "UPDATE athlete_notes SET category = ?, body = ?, rules = ?, valid_from = ?, valid_to = ?, "
            "updated_at = current_timestamp WHERE id = ?",
            [note["category"], note["body"], rules, note.get("valid_from"), note.get("valid_to"), row[0]],
        )
        updated += 1
    return updated


def seed_notes(db: Database, seed_dir: Path) -> int:
    path = seed_dir / "athlete_notes.json"
    if not path.is_file():
        return 0
    notes = json.loads(path.read_text(encoding="utf-8"))
    if not _is_empty(db, "athlete_notes"):
        return refresh_proposed_seed_notes(db, notes)
    for note in notes:
        db.execute(
            "INSERT INTO athlete_notes (category, title, body, valid_from, valid_to, rules, status, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'proposed', 'seed')",
            [
                note["category"], note["title"], note["body"],
                note.get("valid_from"), note.get("valid_to"),
                json.dumps(note["rules"]) if note.get("rules") else None,
            ],
        )
    return len(notes)


def seed_all(db: Database, seed_dir: Path) -> dict[str, Any]:
    from hart.server.health import sync_checks
    from hart.server.labs import import_lab_results, remap_keys
    from hart.server.season_ops import seed_phases

    labs_file = seed_dir / "lab_results.json"
    if not labs_file.is_file():
        labs_file = seed_dir.parent / "blood_results.json"  # location before the rename to hart
    result = {
        "races": seed_races(db, seed_dir),
        "annotations": seed_annotations(db, seed_dir),
        "notes": seed_notes(db, seed_dir),
        "phases": seed_phases(db, datetime.date.today()),
        "lab_results": import_lab_results(db, labs_file),
        "lab_keys_remapped": remap_keys(db),
    }
    result["health_checks"] = sync_checks(db, datetime.date.today())
    logger.info("Seeding: %s", result)
    return result
