"""Parse Garmin .fit files into structured activity, stream, and lap data.

Uses the ``fitparse`` library to decode FIT protocol binary files produced by
Garmin watches and bike computers.  This is the richest data
source available — it contains high-resolution time-series, HRV R-R intervals,
running dynamics from the HRM-Pro Plus, and device metadata that lets us detect
single-sided power meters like the Favero Assioma L.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitparse  # type: ignore[import-untyped]

from hart.models.activity import Activity, Lap, SportType, StreamPoint, StrengthSet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Semicircles → degrees conversion factor (FIT protocol stores GPS coords as
# signed 32-bit integers in semicircle units).
_SEMICIRCLE_TO_DEG: float = 180.0 / (2**31)

# FIT sport enum → our SportType mapping.
_FIT_SPORT_MAP: dict[str, SportType] = {
    "running": SportType.run,
    "cycling": SportType.bike,
    "swimming": SportType.swim,
    "weight_training": SportType.strength,
    "strength_training": SportType.strength,
    "cardio_training": SportType.other,
    "training": SportType.other,
    "walking": SportType.other,
    "hiking": SportType.other,
    "transition": SportType.other,
    "multisport": SportType.other,
    "open_water_swimming": SportType.swim,
    "indoor_cycling": SportType.bike,
    "indoor_running": SportType.run,
    "virtual_ride": SportType.bike,
    "e_bike_riding": SportType.bike,
    "trail_running": SportType.run,
    "track_running": SportType.run,
}

# FIT sub_sport values that override the sport mapping.  Garmin watches record
# strength sessions as sport=training / sub_sport=strength_training.
_FIT_SUB_SPORT_MAP: dict[str, SportType] = {
    "strength_training": SportType.strength,
}

# FIT SDK ``exercise_category`` enum (used by ``set`` messages).
_FIT_EXERCISE_CATEGORY: dict[int, str] = dict(
    enumerate(
        [
            "BENCH_PRESS",
            "CALF_RAISE",
            "CARDIO",
            "CARRY",
            "CHOP",
            "CORE",
            "CRUNCH",
            "CURL",
            "DEADLIFT",
            "FLYE",
            "HIP_RAISE",
            "HIP_STABILITY",
            "HIP_SWING",
            "HYPEREXTENSION",
            "LATERAL_RAISE",
            "LEG_CURL",
            "LEG_RAISE",
            "LUNGE",
            "OLYMPIC_LIFT",
            "PLANK",
            "PLYO",
            "PULL_UP",
            "PUSH_UP",
            "ROW",
            "SHOULDER_PRESS",
            "SHOULDER_STABILITY",
            "SHRUG",
            "SIT_UP",
            "SQUAT",
            "TOTAL_BODY",
            "TRICEPS_EXTENSION",
            "WARM_UP",
            "RUN",
            "BIKE",
        ]
    )
)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class FitParseResult:
    """Container for everything extracted from a single .fit file."""

    activity: Activity
    stream_points: list[StreamPoint]
    laps: list[Lap]
    hrv_rr_intervals: list[tuple[int, float]]  # (global_index, rr_ms)
    device_info: dict[str, Any]
    strength_sets: list[StrengthSet] = field(default_factory=list)

    # Power-meter flags (the caller decides whether to apply correction)
    has_favero_assioma: bool = False
    is_single_sided_power: bool = False


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class FitParser:
    """Stateless parser for Garmin .fit files.

    Usage::

        parser = FitParser()
        result = parser.parse_file(Path("activity.fit"))
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_file(self, fit_path: Path) -> FitParseResult:
        """Parse a single .fit file and return structured data.

        Parameters
        ----------
        fit_path:
            Path to a ``.fit`` file on disk.

        Returns
        -------
        FitParseResult
            Parsed activity metadata, time-series, laps, HRV, and device info.

        Raises
        ------
        FileNotFoundError
            If *fit_path* does not exist.
        ValueError
            If the file does not contain a valid ``session`` message (i.e. it
            is not an activity file — could be device settings, etc.).
        """
        if not fit_path.exists():
            raise FileNotFoundError(f"FIT file not found: {fit_path}")

        fit_file = fitparse.FitFile(str(fit_path))

        # Collect messages by type
        sessions: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        lap_msgs: list[dict[str, Any]] = []
        hrv_msgs: list[dict[str, Any]] = []
        device_msgs: list[dict[str, Any]] = []
        set_msgs: list[dict[str, Any]] = []

        for message in fit_file.get_messages():
            msg_type = message.name
            msg_fields = self._message_to_dict(message)
            if msg_type == "session":
                sessions.append(msg_fields)
            elif msg_type == "record":
                records.append(msg_fields)
            elif msg_type == "lap":
                lap_msgs.append(msg_fields)
            elif msg_type == "hrv":
                hrv_msgs.append(msg_fields)
            elif msg_type == "device_info":
                device_msgs.append(msg_fields)
            elif msg_type == "set":
                set_msgs.append(msg_fields)

        if not sessions:
            raise ValueError(f"No session message found in {fit_path} — file is likely not an activity recording.")

        # Use the first (and usually only) session message.
        session = sessions[0]

        # Detect Favero Assioma
        has_favero, is_single_sided, device_info = self._parse_device_info(device_msgs)

        # Build the Activity model
        activity = self._build_activity(session, fit_path)

        # Build stream points
        stream_points = self._build_stream_points(records)

        # Build laps
        laps = self._build_laps(lap_msgs)

        # Build HRV R-R intervals
        hrv_rr = self._build_hrv(hrv_msgs)

        return FitParseResult(
            activity=activity,
            stream_points=stream_points,
            laps=laps,
            hrv_rr_intervals=hrv_rr,
            device_info=device_info,
            strength_sets=self._build_strength_sets(set_msgs),
            has_favero_assioma=has_favero,
            is_single_sided_power=is_single_sided,
        )

    # ------------------------------------------------------------------
    # Activity building
    # ------------------------------------------------------------------

    def _build_activity(self, session: dict[str, Any], fit_path: Path) -> Activity:
        """Convert a FIT session message dict into an :class:`Activity`."""

        start_time = self._get_datetime(session, "start_time")
        end_timestamp = self._get_datetime(session, "timestamp")
        sport_raw = str(session.get("sport", "other")).lower()
        sub_sport = session.get("sub_sport")
        sport_type = _FIT_SUB_SPORT_MAP.get(str(sub_sport).lower(), _FIT_SPORT_MAP.get(sport_raw, SportType.other))

        elapsed = self._get_int(session, "total_elapsed_time")
        if elapsed is None and start_time and end_timestamp:
            elapsed = int((end_timestamp - start_time).total_seconds())

        # Speed → km/h conversion
        avg_speed_ms = self._get_float(session, "avg_speed")
        avg_speed_kmh = (avg_speed_ms * 3.6) if avg_speed_ms is not None else None

        # Pace calculation for running / swimming
        distance = self._get_float(session, "total_distance")
        avg_pace: float | None = None
        if distance and distance > 0 and elapsed:
            if sport_type == SportType.run:
                avg_pace = elapsed / (distance / 1000.0)
            elif sport_type == SportType.swim:
                avg_pace = elapsed / (distance / 100.0)

        activity_id = self._make_activity_id(start_time, sport_type)

        return Activity(
            activity_id=activity_id,
            source="fit",
            external_id=None,
            sport_type=sport_type,
            sub_type=str(sub_sport) if sub_sport else None,
            name=None,
            description=None,
            start_time=start_time or datetime.datetime.now(tz=datetime.UTC),
            elapsed_seconds=elapsed or 0,
            moving_seconds=(self._get_int(session, "total_moving_time") or self._get_int(session, "total_timer_time")),
            distance_meters=distance,
            total_elevation_m=self._get_float(session, "total_ascent"),
            avg_hr=self._get_int(session, "avg_heart_rate"),
            max_hr=self._get_int(session, "max_heart_rate"),
            avg_power=self._get_int(session, "avg_power"),
            max_power=self._get_int(session, "max_power"),
            normalized_power=self._get_int(session, "normalized_power"),
            avg_cadence=self._get_int(session, "avg_cadence"),
            avg_pace_sec_km=avg_pace,
            avg_speed_kmh=avg_speed_kmh,
            calories=self._get_int(session, "total_calories"),
            avg_temperature=self._get_float(session, "avg_temperature"),
            training_effect_aerobic=self._get_float(session, "total_training_effect"),
            training_effect_anaerobic=self._get_float(session, "total_anaerobic_training_effect"),
            fit_file_path=str(fit_path),
            imported_at=datetime.datetime.now(tz=datetime.UTC),
        )

    # ------------------------------------------------------------------
    # Stream points
    # ------------------------------------------------------------------

    def _build_stream_points(self, records: list[dict[str, Any]]) -> list[StreamPoint]:
        """Convert FIT record messages into a list of :class:`StreamPoint`."""
        points: list[StreamPoint] = []

        for rec in records:
            ts = self._get_datetime(rec, "timestamp")
            if ts is None:
                continue

            # Convert position from semicircles to degrees
            lat_raw = rec.get("position_lat")
            lon_raw = rec.get("position_long")
            lat = (lat_raw * _SEMICIRCLE_TO_DEG) if lat_raw is not None else None
            lon = (lon_raw * _SEMICIRCLE_TO_DEG) if lon_raw is not None else None

            # Speed: m/s → km/h
            speed_ms = self._get_float(rec, "speed") or self._get_float(rec, "enhanced_speed")
            speed_kmh = (speed_ms * 3.6) if speed_ms is not None else None

            # Altitude: prefer enhanced_altitude for sub-meter resolution
            altitude = self._get_float(rec, "enhanced_altitude") or self._get_float(rec, "altitude")

            points.append(
                StreamPoint(
                    timestamp_sec=ts.timestamp(),
                    heart_rate=self._get_int(rec, "heart_rate"),
                    power=self._get_int(rec, "power"),
                    cadence=self._get_int(rec, "cadence"),
                    speed=speed_kmh,
                    altitude=altitude,
                    distance=self._get_float(rec, "distance"),
                    latitude=lat,
                    longitude=lon,
                    temperature=self._get_float(rec, "temperature"),
                    grade_percent=self._get_float(rec, "grade"),
                    ground_contact_time_ms=self._get_float(rec, "ground_contact_time"),
                    vertical_oscillation_mm=self._get_float(rec, "vertical_oscillation"),
                    vertical_ratio_pct=self._get_float(rec, "vertical_ratio"),
                    stride_length_m=self._get_float(rec, "step_length"),
                    respiration_rate=self._get_float(rec, "respiration_rate"),
                )
            )

        return points

    # ------------------------------------------------------------------
    # Laps
    # ------------------------------------------------------------------

    def _build_laps(self, lap_msgs: list[dict[str, Any]]) -> list[Lap]:
        """Convert FIT lap messages into a list of :class:`Lap`."""
        laps: list[Lap] = []

        for idx, msg in enumerate(lap_msgs):
            elapsed = self._get_int(msg, "total_elapsed_time")
            if elapsed is None:
                continue

            distance = self._get_float(msg, "total_distance")
            avg_pace: float | None = None
            if distance and distance > 0 and elapsed:
                avg_pace = elapsed / (distance / 1000.0)

            laps.append(
                Lap(
                    lap_index=idx,
                    start_time=self._get_datetime(msg, "start_time"),
                    elapsed_seconds=elapsed,
                    moving_seconds=(self._get_int(msg, "total_moving_time") or self._get_int(msg, "total_timer_time")),
                    distance_meters=distance,
                    avg_hr=self._get_int(msg, "avg_heart_rate"),
                    max_hr=self._get_int(msg, "max_heart_rate"),
                    avg_power=self._get_int(msg, "avg_power"),
                    avg_cadence=self._get_int(msg, "avg_cadence"),
                    avg_pace_sec_km=avg_pace,
                    total_elevation_m=self._get_float(msg, "total_ascent"),
                    avg_temperature=self._get_float(msg, "avg_temperature"),
                )
            )

        return laps

    # ------------------------------------------------------------------
    # Strength sets
    # ------------------------------------------------------------------

    def _build_strength_sets(self, set_msgs: list[dict[str, Any]]) -> list[StrengthSet]:
        """Convert FIT ``set`` messages into :class:`StrengthSet` rows.

        FIT stores the watch's auto-detected exercise as raw enum ints (the
        first entry of the ``category`` array is the top guess).  Only the
        category is decoded here — the Garmin API sync replaces these rows
        with named, user-corrected exercises when available.
        """
        sets: list[StrengthSet] = []
        for idx, msg in enumerate(set_msgs):
            category = msg.get("category")
            if isinstance(category, (list, tuple)):
                category = category[0] if category else None
            category_name = _FIT_EXERCISE_CATEGORY.get(category) if isinstance(category, int) else None
            set_type = str(msg.get("set_type") or "active").lower()
            sets.append(
                StrengthSet(
                    set_index=idx,
                    set_type=set_type,
                    start_time=self._get_datetime(msg, "start_time"),
                    duration_sec=self._get_float(msg, "duration"),
                    repetitions=self._get_int(msg, "repetitions"),
                    weight_kg=self._get_float(msg, "weight"),
                    exercise_category=category_name if set_type == "active" else None,
                )
            )
        return sets

    # ------------------------------------------------------------------
    # HRV
    # ------------------------------------------------------------------

    def _build_hrv(self, hrv_msgs: list[dict[str, Any]]) -> list[tuple[int, float]]:
        """Extract R-R intervals from FIT HRV messages.

        FIT HRV messages contain a ``time`` field that is an array of R-R
        interval durations in seconds.  ``None`` entries indicate invalid /
        padding values and are skipped.

        Returns
        -------
        list[tuple[int, float]]
            Pairs of ``(global_sample_index, rr_interval_ms)``.
        """
        intervals: list[tuple[int, float]] = []
        global_idx = 0

        for msg in hrv_msgs:
            rr_values = msg.get("time")
            if rr_values is None:
                continue

            # rr_values may be a single float or a list
            if not isinstance(rr_values, (list, tuple)):
                rr_values = [rr_values]

            for rr_sec in rr_values:
                if rr_sec is not None and rr_sec > 0:
                    intervals.append((global_idx, rr_sec * 1000.0))
                    global_idx += 1

        return intervals

    # ------------------------------------------------------------------
    # Device info / Favero Assioma detection
    # ------------------------------------------------------------------

    def _parse_device_info(self, device_msgs: list[dict[str, Any]]) -> tuple[bool, bool, dict[str, Any]]:
        """Inspect device_info messages for power-meter metadata.

        Returns
        -------
        tuple[bool, bool, dict]
            (has_favero_assioma, is_single_sided_power, aggregated_device_info)
        """
        has_favero = False
        is_single_sided = False
        aggregated: dict[str, Any] = {
            "devices": [],
            "power_meter_manufacturer": None,
            "power_meter_product": None,
        }

        for msg in device_msgs:
            manufacturer = str(msg.get("manufacturer", "")).lower()
            product_name = str(msg.get("product_name", "")).lower()
            product = str(msg.get("product", "")).lower()
            device_type = str(msg.get("device_type", "")).lower()
            device_index = str(msg.get("device_index", "")).lower()

            device_entry: dict[str, Any] = {
                "manufacturer": manufacturer,
                "product_name": product_name,
                "product": product,
                "device_type": device_type,
                "device_index": device_index,
            }
            aggregated["devices"].append(device_entry)

            # Detect Favero Assioma
            is_favero = manufacturer == "favero" or "favero" in product_name
            is_assioma = "assioma" in product_name or "assioma" in product

            if is_favero or is_assioma:
                has_favero = True
                aggregated["power_meter_manufacturer"] = "favero"
                aggregated["power_meter_product"] = product_name or product

                # Detect single-sided (Uno / left-only)
                if any(kw in product_name or kw in product for kw in ("uno", "left", " l")):
                    is_single_sided = True
                elif "left" in device_index:
                    is_single_sided = True

        return has_favero, is_single_sided, aggregated

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _message_to_dict(message: Any) -> dict[str, Any]:
        """Convert a fitparse message to a plain dict, preserving raw values."""
        result: dict[str, Any] = {}
        for field_data in message.fields:
            result[field_data.name] = field_data.value
        return result

    @staticmethod
    def _get_datetime(data: dict[str, Any], key: str) -> datetime.datetime | None:
        """Safely extract a datetime from *data*, returning ``None`` on failure."""
        val = data.get(key)
        if val is None:
            return None
        if isinstance(val, datetime.datetime):
            if val.tzinfo is None:
                return val.replace(tzinfo=datetime.UTC)
            return val
        # fitparse sometimes returns a string; try parsing it
        try:
            dt = datetime.datetime.fromisoformat(str(val))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return dt
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _get_float(data: dict[str, Any], key: str) -> float | None:
        """Safely extract a float, returning ``None`` for missing / invalid."""
        val = data.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _get_int(data: dict[str, Any], key: str) -> int | None:
        """Safely extract an int, returning ``None`` for missing / invalid."""
        val = data.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _make_activity_id(
        start_time: datetime.datetime | None,
        sport_type: SportType,
    ) -> str:
        """Generate a deterministic activity ID from start time and sport."""
        if start_time is not None:
            ts_str = start_time.strftime("%Y%m%d_%H%M%S")
        else:
            ts_str = "unknown"
        return f"fit_{ts_str}_{sport_type.value}"
