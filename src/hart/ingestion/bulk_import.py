"""Bulk import of activities from a Garmin export zip file.

Garmin's "Request Your Data" feature provides a zip archive containing all
recorded .fit files (typically under ``DI_CONNECT/DI-CONNECT-FITNESS/``).
This module extracts the archive, parses every .fit file, and upserts the
data into the database.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hart.config import HartSettings
from hart.ingestion.fit_parser import FitParser, FitParseResult
from hart.storage.database import Database
from hart.storage.writers import (
    replace_strength_sets,
    upsert_activity,
    upsert_hrv_samples,
    upsert_laps,
    upsert_stream_points,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    """Summary of a bulk import operation."""

    total_files: int = 0
    activities_imported: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"ImportResult(total_files={self.total_files}, "
            f"imported={self.activities_imported}, "
            f"skipped={self.skipped}, "
            f"errors={self.errors})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def import_garmin_export(
    zip_path: Path,
    db: Database,
    config: HartSettings,
    fit_output_dir: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> ImportResult:
    """Import all activity .fit files from a Garmin export zip archive.

    Parameters
    ----------
    zip_path:
        Path to the Garmin export ``.zip`` file.
    db:
        Database instance (must already be connected).
    config:
        Application configuration (used for power-balance settings).
    fit_output_dir:
        Directory to copy .fit files into for long-term storage.
    progress_callback:
        Optional ``(current, total, message)`` callback for progress reporting.

    Returns
    -------
    ImportResult
        Counts of files processed, imported, skipped, and errored.
    """
    if not zip_path.exists():
        raise FileNotFoundError(f"Garmin export zip not found: {zip_path}")

    fit_output_dir.mkdir(parents=True, exist_ok=True)
    parser = FitParser()
    result = ImportResult()

    with tempfile.TemporaryDirectory(prefix="garmin_export_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Extract the zip
        _report(progress_callback, 0, 0, f"Extracting {zip_path.name}...")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp_path)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Invalid zip file: {zip_path}") from exc

        # Find all .fit files (Garmin exports nest them in subdirectories)
        fit_files = sorted(tmp_path.rglob("*.fit"))
        result.total_files = len(fit_files)

        if not fit_files:
            logger.warning("No .fit files found in %s", zip_path)
            return result

        logger.info("Found %d .fit files in Garmin export", len(fit_files))
        _report(progress_callback, 0, len(fit_files), f"Processing {len(fit_files)} .fit files...")

        for idx, fit_file in enumerate(fit_files):
            _report(
                progress_callback,
                idx + 1,
                len(fit_files),
                f"Processing {fit_file.name}",
            )

            try:
                parsed = _parse_fit_safe(parser, fit_file)
            except ValueError as exc:
                # Not an activity file (no session message) — skip silently
                logger.debug("Skipping non-activity file %s: %s", fit_file.name, exc)
                result.skipped += 1
                continue
            except Exception as exc:
                logger.warning("Error parsing %s: %s", fit_file.name, exc)
                result.errors += 1
                result.error_details.append(f"{fit_file.name}: {exc}")
                continue

            if parsed is None:
                result.skipped += 1
                continue

            # Check for duplicates.  Start time also matches rows whose ID
            # predates a sport remap (e.g. strength once stored as "_other").
            if (
                db.fetchone(
                    "SELECT 1 FROM activities WHERE activity_id = ? OR start_time = ?",
                    [parsed.activity.activity_id, parsed.activity.start_time],
                )
                is not None
            ):
                logger.debug(
                    "Activity %s already exists, skipping",
                    parsed.activity.activity_id,
                )
                result.skipped += 1
                continue

            # Copy .fit file to output directory
            dest_fit = fit_output_dir / fit_file.name
            if not dest_fit.exists():
                shutil.copy2(fit_file, dest_fit)
            parsed.activity.fit_file_path = str(dest_fit)

            # Persist to database
            try:
                _persist_parsed_result(db, parsed, config)
                result.activities_imported += 1
            except Exception as exc:
                logger.warning(
                    "Error persisting activity %s: %s",
                    parsed.activity.activity_id,
                    exc,
                )
                result.errors += 1
                result.error_details.append(f"{parsed.activity.activity_id}: {exc}")

    logger.info("Bulk import complete: %s", result)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_fit_safe(parser: FitParser, fit_path: Path) -> FitParseResult | None:
    """Parse a .fit file, returning ``None`` for non-activity files.

    Raises :class:`ValueError` for files that lack a session message (these
    are settings, device info, or monitoring files — not activities).
    Other exceptions propagate.
    """
    return parser.parse_file(fit_path)


def _persist_parsed_result(
    db: Database,
    parsed: FitParseResult,
    config: HartSettings,
) -> None:
    """Write a parsed FIT result to the database, applying power corrections."""
    activity = parsed.activity

    if parsed.has_favero_assioma and parsed.is_single_sided_power:
        logger.info("Favero Assioma single-sided detected for %s — raw power values stored", activity.activity_id)

    upsert_activity(db, activity)
    upsert_stream_points(db, activity.activity_id, parsed.stream_points)
    upsert_laps(db, activity.activity_id, parsed.laps)
    upsert_hrv_samples(db, activity.activity_id, parsed.hrv_rr_intervals)
    replace_strength_sets(db, activity.activity_id, parsed.strength_sets)


def _report(
    callback: Callable[[int, int, str], None] | None,
    current: int,
    total: int,
    message: str,
) -> None:
    """Invoke the progress callback if provided, otherwise log."""
    if callback is not None:
        callback(current, total, message)
    else:
        if total > 0:
            logger.info("[%d/%d] %s", current, total, message)
        else:
            logger.info("%s", message)
