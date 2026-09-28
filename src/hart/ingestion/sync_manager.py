"""Orchestrates incremental synchronisation from Garmin Connect.

Single entry point for keeping the local database up to date:

* ``sync_garmin_health`` — daily health, sleep, HRV, and training readiness.
* ``sync_garmin_activities`` — download and parse .fit files.
* ``sync_all`` — runs both and returns a combined result map.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hart.config import HartSettings
from hart.ingestion.decoupling import decoupling_from_points
from hart.ingestion.fit_parser import FitParser
from hart.models.activity import SportType, StreamPoint, StrengthSet
from hart.models.health import HealthDay, HRVDaily, SleepRecord
from hart.storage.database import Database
from hart.storage.writers import (
    replace_strength_sets,
    update_sync_state,
    upsert_activity,
    upsert_health_day,
    upsert_hrv_daily,
    upsert_hrv_samples,
    upsert_laps,
    upsert_sleep_record,
    upsert_stream_points,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """Summary of one sync operation."""

    new_activities: int = 0
    updated_activities: int = 0
    new_health_days: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)
    sync_time: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(tz=datetime.timezone.utc)
    )

    def __str__(self) -> str:
        return (
            f"SyncResult(new={self.new_activities}, "
            f"updated={self.updated_activities}, "
            f"health={self.new_health_days}, "
            f"errors={self.errors})"
        )


# ---------------------------------------------------------------------------
# SyncManager
# ---------------------------------------------------------------------------


class SyncManager:
    """Orchestrates incremental Garmin data synchronisation.

    Parameters
    ----------
    db:
        Connected database instance.
    config:
        Application configuration.
    """

    def __init__(self, db: Database, config: HartSettings) -> None:
        self._db = db
        self._config = config
        self._garmin: Any = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_garmin_client(self) -> Any:
        """Return an authenticated garminconnect client, reusing the cached one."""
        if self._garmin is not None:
            return self._garmin

        from garminconnect import Garmin  # type: ignore[import-untyped]

        token_dir = str(self._config.garmin_token_path)
        self._config.garmin_token_path.mkdir(parents=True, exist_ok=True)

        garmin = Garmin(self._config.garmin.email, self._config.garmin.password)
        garmin.login(tokenstore=token_dir)
        try:
            garmin.client.dump(token_dir)
        except Exception:
            pass
        self._garmin = garmin
        return garmin

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_garmin_health(self, days_back: int = 7) -> SyncResult:
        """Fetch daily health, sleep, HRV, and training readiness from Garmin.

        Parameters
        ----------
        days_back:
            Number of days to look back from today.
        """
        result = SyncResult()

        if not self._config.garmin.email:
            logger.warning("Garmin credentials not configured, skipping health sync")
            return result

        garmin = self.get_garmin_client()
        today = datetime.date.today()

        for offset in range(days_back):
            date = today - datetime.timedelta(days=offset)
            date_str = date.isoformat()

            try:
                self._sync_health_day(garmin, date, date_str)
                result.new_health_days += 1
            except Exception as exc:
                logger.warning("Health sync failed for %s: %s", date_str, exc)
                result.errors += 1
                result.error_details.append(f"health:{date_str}: {exc}")

        update_sync_state(self._db, "garmin_health", last_sync_at=result.sync_time)
        logger.info("Garmin health sync complete: %s", result)
        return result

    def sync_garmin_activities(self, days_back: int = 7) -> SyncResult:
        """Download and parse .fit files for recent Garmin activities.

        Parameters
        ----------
        days_back:
            Number of days to look back from today.
        """
        result = SyncResult()

        if not self._config.garmin.email:
            logger.warning("Garmin credentials not configured, skipping activity sync")
            return result

        garmin = self.get_garmin_client()

        from garminconnect import Garmin as _Garmin  # type: ignore[import-untyped]
        DL_FMT = _Garmin.ActivityDownloadFormat

        today = datetime.date.today()
        start_date = today - datetime.timedelta(days=days_back)

        try:
            activities_list = garmin.get_activities_by_date(
                start_date.isoformat(), today.isoformat()
            )
        except Exception as exc:
            logger.error("Garmin activity list fetch failed: %s", exc)
            result.errors += 1
            result.error_details.append(f"get_activities: {exc}")
            return result

        if not activities_list:
            update_sync_state(self._db, "garmin_activities", last_sync_at=result.sync_time)
            return result

        # Skip activities already in the database
        existing_ids: set[str] = {
            str(r[0])
            for r in self._db.fetchall(
                "SELECT external_id FROM activities "
                "WHERE source = 'garmin' AND external_id IS NOT NULL"
            )
        }
        new_activities = [
            a for a in activities_list
            if str(a.get("activityId", "")) not in existing_ids
        ]
        # Activities we already have may have been renamed (or described) in
        # Garmin Connect since; the list carries the current values, so update
        # them without any extra API calls.
        result.updated_activities += self._refresh_edited(
            [a for a in activities_list if str(a.get("activityId", "")) in existing_ids]
        )

        parser = FitParser()

        for act in new_activities:
            act_id = str(act.get("activityId", ""))
            act_name = act.get("activityName") or "Activity"
            if not act_id:
                continue

            try:
                zip_bytes = garmin.download_activity(act_id, dl_fmt=DL_FMT.ORIGINAL)
                fit_data = _extract_fit(zip_bytes)
                if fit_data is None:
                    logger.debug("No .fit in download for activity %s", act_id)
                    result.errors += 1
                    continue

                with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
                    tmp.write(fit_data)
                    tmp_path = Path(tmp.name)

                try:
                    parsed = parser.parse_file(tmp_path)
                finally:
                    tmp_path.unlink(missing_ok=True)

                parsed.activity.source = "garmin"
                parsed.activity.external_id = act_id
                # The name set in Garmin Connect wins over the one in the FIT file.
                if act.get("activityName") or not parsed.activity.name:
                    parsed.activity.name = act_name
                if act.get("description"):
                    parsed.activity.description = act["description"]

                summary = _enrich_from_summary_dto(garmin, act_id, parsed.activity)
                parsed.activity.gear_id = _fetch_gear_id(garmin, act_id)

                upsert_activity(self._db, parsed.activity)
                upsert_stream_points(self._db, parsed.activity.activity_id, parsed.stream_points)
                upsert_laps(self._db, parsed.activity.activity_id, parsed.laps)
                upsert_hrv_samples(self._db, parsed.activity.activity_id, parsed.hrv_rr_intervals)
                if parsed.activity.sport_type == SportType.strength:
                    sets = _fetch_strength_sets(garmin, act_id) or parsed.strength_sets
                    replace_strength_sets(self._db, parsed.activity.activity_id, sets)
                self._sync_activity_metrics(
                    garmin, act_id, parsed.activity, summary, parsed.stream_points
                )
                result.new_activities += 1

            except Exception as exc:
                logger.error("Error syncing activity %s (%s): %s", act_id, act_name, exc)
                result.errors += 1
                result.error_details.append(f"activity:{act_id}: {exc}")

        update_sync_state(self._db, "garmin_activities", last_sync_at=result.sync_time)
        logger.info("Garmin activity sync complete: %s", result)
        return result

    def _refresh_edited(self, listed: list[dict[str, Any]]) -> int:
        """Apply names/descriptions edited in Garmin Connect to stored activities."""
        changed = 0
        for act in listed:
            name = act.get("activityName")
            if not name:
                continue
            description = act.get("description") or None
            row = self._db.fetchone(
                "SELECT activity_id, name, description FROM activities WHERE source = 'garmin' AND external_id = ?",
                [str(act["activityId"])],
            )
            if row is None or (row[1] == name and (description is None or row[2] == description)):
                continue
            self._db.execute(
                "UPDATE activities SET name = ?, description = coalesce(?, description) WHERE activity_id = ?",
                [name, description, row[0]],
            )
            logger.info("Activity %s renamed in Garmin: %r → %r", row[0], row[1], name)
            changed += 1
        return changed

    def refresh_recent_strength_sets(self, days: int, skip: set[str] | None = None) -> list[str]:
        """Re-read the sets of recent strength sessions from Garmin.

        Sets are usually corrected in Garmin Connect after the session was
        first synced (reps, weights, exercise names), so the first download
        is often not the final version.  Returns the ids whose sets changed.
        """
        if not self._config.garmin.email:
            return []
        rows = self._db.fetchall(
            "SELECT activity_id, external_id FROM activities WHERE sport_type = 'strength' "
            "AND source = 'garmin' AND external_id IS NOT NULL AND start_time >= ? ORDER BY start_time",
            [datetime.date.today() - datetime.timedelta(days=days)],
        )
        rows = [r for r in rows if r[0] not in (skip or set())]
        if not rows:
            return []
        garmin = self.get_garmin_client()
        changed = []
        for activity_id, external_id in rows:
            sets = _fetch_strength_sets(garmin, str(external_id))
            if not sets:
                continue
            stored = self._db.fetchall(
                "SELECT set_index, set_type, repetitions, weight_kg, exercise_category, exercise_name "
                "FROM strength_sets WHERE activity_id = ? ORDER BY set_index", [activity_id])
            fresh = [(s.set_index, s.set_type, s.repetitions, s.weight_kg, s.exercise_category, s.exercise_name)
                     for s in sorted(sets, key=lambda s: s.set_index)]
            if _sets_key(stored) == _sets_key(fresh):
                continue
            replace_strength_sets(self._db, activity_id, sets)
            logger.info("Strength sets of %s were edited in Garmin — updated", activity_id)
            changed.append(activity_id)
        return changed

    def refresh_recent_effort(self, days: int, skip: set[str] | None = None) -> list[str]:
        """Re-read RPE and feel of recent activities (edited in Garmin Connect
        after the first sync).  One Garmin call per activity in the window;
        returns the ids whose values changed."""
        if not self._config.garmin.email:
            return []
        rows = self._db.fetchall(
            "SELECT activity_id, external_id, rpe, feel FROM activities WHERE source = 'garmin' "
            "AND external_id IS NOT NULL AND start_time >= ? ORDER BY start_time",
            [datetime.date.today() - datetime.timedelta(days=days)],
        )
        rows = [r for r in rows if r[0] not in (skip or set())]
        if not rows:
            return []
        garmin = self.get_garmin_client()
        changed = []
        for activity_id, external_id, rpe, feel in rows:
            detail = _safe_call(garmin.get_activity, str(external_id))
            summary = (detail or {}).get("summaryDTO")
            if not summary:
                continue  # no answer: keep what we have rather than erase it
            new = (summary.get("directWorkoutRpe"), summary.get("directWorkoutFeel"))
            if new == (rpe, feel):
                continue
            self._db.execute("UPDATE activities SET rpe = ?, feel = ? WHERE activity_id = ?", [*new, activity_id])
            logger.info("RPE/feel of %s edited in Garmin: %s → %s", activity_id, (rpe, feel), new)
            changed.append(activity_id)
        return changed

    def backfill_gear(self) -> SyncResult:
        """Fetch and store gear assignments for all activities with no gear_id set.

        Calls get_activity_gear once per activity — rate-limited to avoid
        hammering the Garmin API.
        """
        import time

        result = SyncResult()

        if not self._config.garmin.email:
            logger.warning("Garmin credentials not configured, skipping gear backfill")
            return result

        garmin = self.get_garmin_client()

        rows = self._db.fetchall(
            "SELECT activity_id, external_id FROM activities "
            "WHERE gear_id IS NULL AND external_id IS NOT NULL "
            "ORDER BY start_time DESC"
        )

        logger.info("Gear backfill: %d activities to process", len(rows))

        for activity_id, external_id in rows:
            try:
                gear_uuid = _fetch_gear_id(garmin, str(external_id))
                self._db.execute(
                    "UPDATE activities SET gear_id = ? WHERE activity_id = ?",
                    [gear_uuid, activity_id],
                )
                result.updated_activities += 1
                time.sleep(0.3)  # be polite to the API
            except Exception as exc:
                logger.warning("Gear fetch failed for %s: %s", external_id, exc)
                result.errors += 1
                result.error_details.append(f"gear:{external_id}: {exc}")

        logger.info("Gear backfill complete: %s", result)
        return result

    def backfill_strength_sets(self, refresh: bool = False) -> SyncResult:
        """Fetch exercise sets from Garmin for strength activities.

        By default only activities with no stored sets are processed; pass
        ``refresh=True`` to re-fetch all (picks up edits made in Garmin Connect).
        """
        import time

        result = SyncResult()

        if not self._config.garmin.email:
            logger.warning("Garmin credentials not configured, skipping strength backfill")
            return result

        garmin = self.get_garmin_client()

        missing_only = "" if refresh else (
            "AND activity_id NOT IN (SELECT DISTINCT activity_id FROM strength_sets) "
        )
        rows = self._db.fetchall(
            "SELECT activity_id, external_id FROM activities "
            "WHERE sport_type = 'strength' AND external_id IS NOT NULL "
            f"{missing_only}ORDER BY start_time DESC"
        )

        logger.info("Strength backfill: %d activities to process", len(rows))

        for activity_id, external_id in rows:
            try:
                sets = _fetch_strength_sets(garmin, str(external_id))
                if sets:
                    replace_strength_sets(self._db, activity_id, sets)
                    result.updated_activities += 1
                time.sleep(0.3)  # be polite to the API
            except Exception as exc:
                logger.warning("Strength sets fetch failed for %s: %s", external_id, exc)
                result.errors += 1
                result.error_details.append(f"strength:{external_id}: {exc}")

        logger.info("Strength backfill complete: %s", result)
        return result

    def backfill_vo2max(
        self,
        start: datetime.date | None = None,
        end: datetime.date | None = None,
    ) -> dict[str, Any]:
        """Fill ``daily_health.vo2max_run`` / ``vo2max_cycle`` for past dates.

        Fetches the max-metrics range endpoint in yearly chunks (one API call per
        year instead of one per day).  Only dates that already have a
        ``daily_health`` row are written, through the merge-upsert, so no other
        health field is touched and no stored VO2max is replaced by NULL.
        Defaults: from the first ``daily_health`` date to today.
        """
        if not self._config.garmin.email:
            raise RuntimeError("Garmin credentials not configured (GARMIN_EMAIL)")

        if start is None:
            first = self._db.fetchone("SELECT MIN(date) FROM daily_health")
            start = first[0] if first and first[0] else datetime.date.today()
        end = end or datetime.date.today()

        garmin = self.get_garmin_client()
        values: dict[datetime.date, tuple[float | None, float | None]] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + datetime.timedelta(days=VO2MAX_CHUNK_DAYS - 1), end)
            values.update(_parse_max_metrics(_fetch_max_metrics_range(garmin, chunk_start, chunk_end)))
            chunk_start = chunk_end + datetime.timedelta(days=1)

        existing = {
            r[0] for r in self._db.fetchall(
                "SELECT date FROM daily_health WHERE date >= ? AND date <= ?", [start, end]
            )
        }
        updated = 0
        no_health_row: list[str] = []
        for day, (run, cycle) in sorted(values.items()):
            if not start <= day <= end:
                continue
            if day not in existing:
                no_health_row.append(day.isoformat())
                continue
            upsert_health_day(self._db, HealthDay(date=day, vo2max_run=run, vo2max_cycle=cycle))
            updated += 1

        result = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "estimates": len(values),
            "updated": updated,
            "no_health_row": len(no_health_row),
        }
        logger.info("VO2max backfill complete: %s", result)
        return result

    def sync_all(self) -> dict[str, SyncResult]:
        """Run health and activity sync; return results keyed by source."""
        logger.info("Starting full Garmin sync")
        results = {
            "garmin_health": self.sync_garmin_health(),
            "garmin_activities": self.sync_garmin_activities(),
        }
        logger.info(
            "Full sync complete: %d new activities, %d errors",
            sum(r.new_activities for r in results.values()),
            sum(r.errors for r in results.values()),
        )
        return results

    # ------------------------------------------------------------------
    # Per-day health sync
    # ------------------------------------------------------------------

    def _sync_health_day(self, garmin: Any, date: datetime.date, date_str: str) -> None:
        """Fetch and persist all health data for a single day."""
        # Daily health stats
        stats = _safe_call(garmin.get_stats, date_str)
        if stats and isinstance(stats, dict):
            # VO2max isn't in the daily summary; the max-metrics service has an
            # entry only on days Garmin produced a new estimate.
            vo2_run, vo2_cycle = _parse_max_metrics(
                _safe_call(garmin.get_max_metrics, date_str)
            ).get(date, (None, None))
            stress_pct = _fetch_stress_percentiles(garmin, date_str)
            hr_pct = _fetch_hr_percentiles(garmin, date_str)

            readiness_data = _safe_call(garmin.get_training_readiness, date_str)
            training_readiness: int | None = None
            if isinstance(readiness_data, dict):
                training_readiness = _safe_int(
                    readiness_data.get("score") or readiness_data.get("trainingReadinessScore")
                )
            elif isinstance(readiness_data, list) and readiness_data:
                first = readiness_data[0]
                if isinstance(first, dict):
                    training_readiness = _safe_int(
                        first.get("score") or first.get("trainingReadinessScore")
                    )

            health_day = HealthDay(
                date=date,
                resting_hr=_safe_int(stats.get("restingHeartRate")),
                min_hr=hr_pct.get("min_hr") or _safe_int(stats.get("minHeartRate")),
                max_hr_day=_safe_int(stats.get("maxHeartRate")),
                hr_p25=hr_pct.get("hr_p25"),
                hr_p50=hr_pct.get("hr_p50"),
                hr_p75=hr_pct.get("hr_p75"),
                avg_stress=_safe_int(stats.get("averageStressLevel")),
                max_stress=_safe_int(stats.get("maxStressLevel")),
                stress_p50=stress_pct.get("stress_p50"),
                stress_p75=stress_pct.get("stress_p75"),
                stress_p90=stress_pct.get("stress_p90"),
                stress_p95=stress_pct.get("stress_p95"),
                high_stress_duration=_safe_int(stats.get("highStressDuration")),
                medium_stress_duration=_safe_int(stats.get("mediumStressDuration")),
                low_stress_duration=_safe_int(stats.get("lowStressDuration")),
                rest_stress_duration=_safe_int(stats.get("restStressDuration")),
                body_battery_high=_safe_int(stats.get("bodyBatteryHighestValue")),
                body_battery_low=_safe_int(stats.get("bodyBatteryLowestValue")),
                body_battery_start=_safe_int(stats.get("bodyBatteryAtWakeTime")),
                training_readiness=training_readiness,
                vo2max_run=vo2_run,
                vo2max_cycle=vo2_cycle,
                respiration_avg=_safe_float(stats.get("avgWakingRespirationValue")),
                respiration_min=_safe_float(stats.get("lowestRespirationValue")),
                respiration_max=_safe_float(stats.get("highestRespirationValue")),
                steps=_safe_int(stats.get("totalSteps")),
                active_calories=_safe_int(stats.get("activeKilocalories")),
                total_calories=_safe_int(stats.get("totalKilocalories")),
            )
            upsert_health_day(self._db, health_day)

        # Sleep
        sleep_data = _safe_call(garmin.get_sleep_data, date_str)
        if sleep_data and isinstance(sleep_data, dict):
            dto = sleep_data.get("dailySleepDTO", {})
            if dto and isinstance(dto, dict):
                def _epoch_ms(val: Any) -> datetime.datetime | None:
                    if val is None:
                        return None
                    try:
                        return datetime.datetime.fromtimestamp(
                            int(val) / 1000.0, tz=datetime.timezone.utc
                        )
                    except (ValueError, TypeError, OSError):
                        return None

                sleep_score: int | None = None
                scores = sleep_data.get("sleepScores") or dto.get("sleepScores") or {}
                if isinstance(scores, dict):
                    overall = scores.get("overall") or {}
                    sleep_score = _safe_int(
                        overall.get("value") if isinstance(overall, dict) else scores.get("overallScore")
                    )

                upsert_sleep_record(self._db, SleepRecord(
                    date=date,
                    sleep_start=_epoch_ms(dto.get("sleepStartTimestampGMT")),
                    sleep_end=_epoch_ms(dto.get("sleepEndTimestampGMT")),
                    total_sleep_sec=_safe_int(dto.get("sleepTimeSeconds")),
                    deep_sleep_sec=_safe_int(dto.get("deepSleepSeconds")),
                    light_sleep_sec=_safe_int(dto.get("lightSleepSeconds")),
                    rem_sleep_sec=_safe_int(dto.get("remSleepSeconds")),
                    awake_sec=_safe_int(dto.get("awakeSleepSeconds")),
                    sleep_score=sleep_score,
                    avg_spo2=_safe_float(dto.get("averageSpO2Value")),
                    avg_respiration=_safe_float(dto.get("averageRespirationValue")),
                    avg_hr_sleep=_safe_int(dto.get("avgHeartRate")),
                ))

        # HRV
        hrv_data = _safe_call(garmin.get_hrv_data, date_str)
        if hrv_data and isinstance(hrv_data, dict):
            summary = hrv_data.get("hrvSummary", {})
            if summary and isinstance(summary, dict):
                baseline = summary.get("baseline") or {}
                upsert_hrv_daily(self._db, HRVDaily(
                    date=date,
                    hrv_weekly_avg_ms=_safe_float(summary.get("weeklyAvg")),
                    hrv_last_night_ms=_safe_float(summary.get("lastNightAvg")),
                    hrv_last_night_5min_high=_safe_float(summary.get("lastNight5MinHigh")),
                    hrv_status=summary.get("status"),
                    baseline_low_ms=_safe_float(baseline.get("balancedLow")),
                    baseline_high_ms=_safe_float(baseline.get("balancedUpper")),
                ))

    def _sync_activity_metrics(
        self,
        garmin: Any,
        ext_id: str,
        activity: Any,
        summary: dict[str, Any],
        stream_points: list[StreamPoint] | None = None,
    ) -> None:
        """Fetch HR zones and EF from Garmin data for a single activity, and
        compute aerobic decoupling from the parsed FIT streams.

        Called immediately after upsert_activity so every newly synced activity
        gets its metrics row in one pass — no separate backfill needed.
        """
        sport = activity.sport_type
        sport_str = sport.value if hasattr(sport, "value") else str(sport)
        act_date = (
            activity.start_time.date()
            if hasattr(activity.start_time, "date")
            else str(activity.start_time)[:10]
        )

        # HR zones from Garmin device — reflect the zones as configured at the
        # time of the activity, no static threshold needed.
        hr_zone_seconds: dict[str, float] | None = None
        raw_zones = _safe_call(garmin.get_activity_hr_in_timezones, ext_id)
        if raw_zones:
            hr_zone_seconds = {
                f"Z{z['zoneNumber']}": round(z["secsInZone"], 1)
                for z in raw_zones
                if z.get("secsInZone") is not None
            }

        # Efficiency factor: NP/avg_HR (bike) or speed/avg_HR (run).
        # No FTP dependency — valid regardless of current threshold.
        np_val = _safe_float(summary.get("normalizedPower")) or activity.normalized_power
        avg_hr = _safe_float(summary.get("averageHR")) or _safe_float(getattr(activity, "avg_hr", None))
        avg_speed = _safe_float(summary.get("averageMovingSpeed"))

        ef: float | None = None
        if sport == SportType.bike and np_val and avg_hr and avg_hr > 0:
            ef = round(np_val / avg_hr, 4)
        elif sport == SportType.run and avg_speed and avg_hr and avg_hr > 0:
            ef = round(avg_speed / (avg_hr / 60.0), 4)

        # Aerobic decoupling: steady bike/run sessions of 45+ moving minutes.
        decoupling: float | None = None
        if stream_points:
            try:
                decoupling = decoupling_from_points(stream_points, sport)
            except Exception as exc:
                logger.warning("Decoupling failed for activity %s: %s", activity.activity_id, exc)

        # TSS from Garmin's own training load (already FTP-adjusted on device).
        tss = _safe_float(activity.garmin_training_load)
        tss_method = "garmin" if tss is not None else None

        # Running dynamics (Garmin strideLength in cm → m)
        sl = _safe_float(summary.get("strideLength"))

        try:
            self._db.execute(
                """INSERT OR REPLACE INTO activity_metrics
                (activity_id, sport_type, date, tss, tss_method,
                 hr_zone_seconds, efficiency_factor, aerobic_decoupling_pct,
                 avg_ground_contact_ms, avg_vertical_osc_mm, avg_vertical_ratio_pct,
                 avg_stride_length_m, swolf, estimated_calories, estimated_kj,
                 carb_calories, fat_calories)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    activity.activity_id, sport_str, act_date,
                    tss, tss_method,
                    json.dumps(hr_zone_seconds) if hr_zone_seconds else None,
                    ef, decoupling,
                    _safe_float(summary.get("groundContactTime")),
                    _safe_float(summary.get("verticalOscillation")),
                    _safe_float(summary.get("verticalRatio")),
                    round(sl / 100.0, 3) if sl is not None else None,
                    None,        # swolf
                    _safe_int(summary.get("calories")),
                    round(float(summary["totalWork"]), 1) if summary.get("totalWork") else None,
                    None, None,  # carb/fat calories not in Garmin API
                ],
            )
            logger.debug("Metrics written for activity %s", activity.activity_id)
        except Exception as exc:
            logger.warning("Failed to write metrics for activity %s: %s", activity.activity_id, exc)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


VO2MAX_CHUNK_DAYS = 366


def _fetch_max_metrics_range(
    garmin: Any, start: datetime.date, end: datetime.date
) -> Any:
    """Max-metrics (VO2max) entries for a date range in one call.

    garminconnect only wraps the single-day form (``get_max_metrics``); the
    service takes ``/{start}/{end}`` the same way.
    """
    return garmin.connectapi(
        f"{garmin.garmin_connect_metrics_url}/{start.isoformat()}/{end.isoformat()}"
    )


def _parse_max_metrics(
    entries: Any,
) -> dict[datetime.date, tuple[float | None, float | None]]:
    """Map date → (run VO2max, cycling VO2max) from a max-metrics response.

    Each entry has ``generic`` (running) and ``cycling`` blocks, either of
    which may be null; the precise (one-decimal) value is preferred.
    """
    out: dict[datetime.date, tuple[float | None, float | None]] = {}
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for index, key in ((0, "generic"), (1, "cycling")):
            block = entry.get(key)
            if not isinstance(block, dict) or not block.get("calendarDate"):
                continue
            value = _safe_float(block.get("vo2MaxPreciseValue"))
            if value is None:
                value = _safe_float(block.get("vo2MaxValue"))
            if value is None:
                continue
            try:
                day = datetime.date.fromisoformat(str(block["calendarDate"])[:10])
            except ValueError:
                continue
            pair = list(out.get(day, (None, None)))
            pair[index] = value
            out[day] = (pair[0], pair[1])
    return out


def _safe_call(method: Any, *args: Any) -> Any:
    """Call a garminconnect API method, returning None on any failure."""
    try:
        return method(*args)
    except Exception as exc:
        logger.debug("API call %s failed: %s", getattr(method, "__name__", method), exc)
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _percentile(sorted_vals: list[float], p: int) -> float:
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def _fetch_stress_percentiles(garmin: Any, date_str: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    data = _safe_call(garmin.get_stress_data, date_str)
    if not isinstance(data, dict):
        return result

    vals_array = data.get("stressValuesArray") or []
    measured = sorted(
        v[1] for v in vals_array
        if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], (int, float)) and v[1] > 0
    )
    if measured:
        result["stress_p50"] = _percentile(measured, 50)
        result["stress_p75"] = _percentile(measured, 75)
        result["stress_p90"] = _percentile(measured, 90)
        result["stress_p95"] = _percentile(measured, 95)
    return result


def _fetch_hr_percentiles(garmin: Any, date_str: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    data = _safe_call(garmin.get_heart_rates, date_str)
    if not isinstance(data, dict):
        return result

    vals_array = data.get("heartRateValues") or []
    measured = sorted(
        v[1] for v in vals_array
        if isinstance(v, list) and len(v) >= 2 and v[1] is not None and v[1] > 0
    )
    if measured:
        result["min_hr"] = measured[0]
        result["hr_p25"] = _percentile(measured, 25)
        result["hr_p50"] = _percentile(measured, 50)
        result["hr_p75"] = _percentile(measured, 75)
    return result


def _extract_fit(data: bytes) -> bytes | None:
    """Extract a .fit file from a Garmin ORIGINAL download (ZIP or raw bytes)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".fit"):
                    return zf.read(name)
    except zipfile.BadZipFile:
        if len(data) > 12:
            return data
    return None


def _enrich_from_summary_dto(garmin: Any, act_id: str, activity: Any) -> dict[str, Any]:
    """Fetch the Garmin activity detail, write summaryDTO fields onto *activity*, and
    return the raw summaryDTO dict for downstream use (e.g. metrics computation)."""
    try:
        detail = _safe_call(garmin.get_activity, act_id)
        if not detail:
            return {}
        summary = detail.get("summaryDTO", {})
        activity.rpe = summary.get("directWorkoutRpe")
        activity.feel = summary.get("directWorkoutFeel")
        activity.garmin_training_load = summary.get("activityTrainingLoad")
        activity.training_effect_label = summary.get("trainingEffectLabel")
        activity.body_battery_delta = summary.get("differenceBodyBattery")
        activity.begin_stamina = summary.get("beginPotentialStamina")
        activity.end_stamina = summary.get("endPotentialStamina")
        moving = summary.get("movingDuration")
        if moving is not None and activity.moving_seconds is None:
            activity.moving_seconds = int(moving)
        np_val = summary.get("normalizedPower")
        if np_val is not None and activity.normalized_power is None:
            activity.normalized_power = float(np_val)
        return summary
    except Exception as exc:
        logger.debug("summaryDTO fetch failed for activity %s: %s", act_id, exc)
        return {}


def _sets_key(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Comparable form of sets: weights rounded (floats from the API vs stored doubles)."""
    return [(i, t, r, round(w, 2) if w is not None else None, c, n) for i, t, r, w, c, n in rows]


def _fetch_strength_sets(garmin: Any, act_id: str) -> list[StrengthSet]:
    """Return named, user-corrected exercise sets from the Garmin API."""
    from hart.analytics.strength import parse_garmin_exercise_sets

    return parse_garmin_exercise_sets(_safe_call(garmin.get_activity_exercise_sets, act_id))


def _fetch_gear_id(garmin: Any, act_id: str) -> str | None:
    """Return the uuid of the first Bike-type gear assigned to an activity, or None."""
    gear_list = _safe_call(garmin.get_activity_gear, act_id)
    if not gear_list:
        return None
    # Prefer explicit Bike type; fall back to first item if none tagged Bike
    for item in gear_list:
        if isinstance(item, dict) and item.get("gearTypeName", "").lower() == "bike":
            return item.get("uuid")
    return None
