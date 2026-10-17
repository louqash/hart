"""Joining recordings of one session: which recordings belong together and what the joined one adds up to.

The watch was stopped by mistake (or for a café stop) and started again, so one session is two recordings of
the same sport. Joined, it is one activity: elapsed time runs from the first start to the last end (the gap
counts like a pause), moving time, distance, climbing, calories, work and Garmin's training load add up,
averages are weighted by moving time, and normalized power is combined from the parts' fourth powers (NP is
the fourth root of the mean fourth power, so this is exact up to the 30-second window at the seams). Training
effect doesn't add up: the higher part's stands in for it.
"""

from __future__ import annotations

import datetime
from typing import Any

from hart.server.data import is_indoor

# A later recording may start a moment before the earlier one's rounded end.
OVERLAP_TOLERANCE_SEC = 5


def end_time(activity: dict[str, Any]) -> datetime.datetime:
    return activity["start_time"] + datetime.timedelta(seconds=activity.get("elapsed_seconds") or 0)


def gap_seconds(earlier: dict[str, Any], later: dict[str, Any]) -> float:
    """Seconds from the end of *earlier* to the start of *later* (negative: they overlap)."""
    return (later["start_time"] - end_time(earlier)).total_seconds()


def same_kind(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same sport, both indoor or both outdoor; for "other" (walks, hikes, yoga…) the same activity type."""
    if a["sport_type"] != b["sport_type"] or is_indoor(a.get("sub_type")) != is_indoor(b.get("sub_type")):
        return False
    return a["sport_type"] != "other" or (a.get("sub_type") or "") == (b.get("sub_type") or "")


def join_chains(activities: list[dict[str, Any]], max_gap_minutes: int) -> list[list[str]]:
    """Runs of consecutive recordings that look like one session, as lists of activity ids (two or more).

    Consecutive means nothing else was recorded in between; each one starts at most *max_gap_minutes* after
    the previous one ended. 0 minutes: nothing is joined.
    """
    if max_gap_minutes <= 0:
        return []
    chains: list[list[str]] = []
    current: list[dict[str, Any]] = []
    for a in sorted(activities, key=lambda x: x["start_time"]):
        if (
            current
            and same_kind(current[-1], a)
            and -OVERLAP_TOLERANCE_SEC <= gap_seconds(current[-1], a) <= max_gap_minutes * 60
        ):
            current.append(a)
            continue
        if len(current) > 1:
            chains.append([x["activity_id"] for x in current])
        current = [a]
    if len(current) > 1:
        chains.append([x["activity_id"] for x in current])
    return chains


def join_problem(parts: list[dict[str, Any]]) -> str | None:
    """Why these recordings (ordered by start) can't be joined, or None."""
    if len(parts) < 2:
        return "Pick at least two activities to join."
    if len({p["sport_type"] for p in parts}) > 1:
        return "Only activities of the same sport can be joined."
    for earlier, later in zip(parts, parts[1:]):
        if gap_seconds(earlier, later) < -OVERLAP_TOLERANCE_SEC:
            return "These activities overlap in time, so they aren't parts of one recording."
    return None


# ---------------------------------------------------------------------------
# The joined activity
# ---------------------------------------------------------------------------


def _seconds(a: dict[str, Any]) -> float:
    return float(a.get("moving_seconds") or a.get("elapsed_seconds") or 0)


def _present(parts: list[dict[str, Any]], key: str) -> list[Any]:
    return [p[key] for p in parts if p.get(key) is not None]


def _sum(parts: list[dict[str, Any]], key: str) -> float | None:
    values = _present(parts, key)
    return sum(values) if values else None


def _max(parts: list[dict[str, Any]], key: str) -> Any:
    values = _present(parts, key)
    return max(values) if values else None


def _weighted(parts: list[dict[str, Any]], key: str, weights: list[float]) -> float | None:
    pairs = [(p[key], w) for p, w in zip(parts, weights) if p.get(key) is not None and w > 0]
    total = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total if total else None


def _last(parts: list[dict[str, Any]], key: str) -> Any:
    """The last part's value that is set — an RPE is given when the session is over."""
    values = _present(parts, key)
    return values[-1] if values else None


def _round(value: float | None, digits: int = 0) -> float | int | None:
    if value is None:
        return None
    return round(value) if digits == 0 else round(value, digits)


def combined_np(parts: list[dict[str, Any]], weights: list[float]) -> float | None:
    """Normalized power of the parts together: the fourth root of their time-weighted fourth powers."""
    pairs = [(p["normalized_power"], w) for p, w in zip(parts, weights) if p.get("normalized_power") and w > 0]
    total = sum(w for _, w in pairs)
    if not total:
        return None
    return (sum(np**4 * w for np, w in pairs) / total) ** 0.25


def joined_description(parts: list[dict[str, Any]], key: str = "description") -> str | None:
    """The parts' texts in order, each once."""
    texts: list[str] = []
    for p in parts:
        text = (p.get(key) or "").strip()
        if text and text not in texts:
            texts.append(text)
    return "\n\n".join(texts) or None


def combine_activity(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """The joined activity from its parts (ordered by start): the first part's identity — id, Garmin id, name,
    sport, gear — with the totals and averages of all of them."""
    first, last = parts[0], parts[-1]
    weights = [_seconds(p) for p in parts]
    moving = sum(weights) if any(p.get("moving_seconds") is not None for p in parts) else None
    distance = _sum(parts, "distance_meters")
    time_s = sum(weights)
    te_part = max(parts, key=lambda p: p.get("training_effect_aerobic") or -1)
    joined = dict(first)
    joined.update(
        {
            "description": joined_description(parts),
            "elapsed_seconds": int(round((end_time(last) - first["start_time"]).total_seconds())),
            "moving_seconds": int(round(moving)) if moving is not None else None,
            "distance_meters": distance,
            "total_elevation_m": _sum(parts, "total_elevation_m"),
            "avg_hr": _round(_weighted(parts, "avg_hr", weights)),
            "max_hr": _max(parts, "max_hr"),
            "avg_power": _round(_weighted(parts, "avg_power", weights)),
            "max_power": _max(parts, "max_power"),
            "normalized_power": _round(combined_np(parts, weights)),
            "avg_cadence": _round(_weighted(parts, "avg_cadence", weights)),
            "avg_speed_kmh": (
                round(distance / time_s * 3.6, 2)
                if distance and time_s
                else _round(_weighted(parts, "avg_speed_kmh", weights), 2)
            ),
            "avg_pace_sec_km": (
                round(time_s / (distance / 1000), 1)
                if distance and time_s and _present(parts, "avg_pace_sec_km")
                else None
            ),
            "calories": _round(_sum(parts, "calories")),
            "avg_temperature": _round(_weighted(parts, "avg_temperature", weights), 1),
            "training_effect_aerobic": _max(parts, "training_effect_aerobic"),
            "training_effect_anaerobic": _max(parts, "training_effect_anaerobic"),
            "training_effect_label": te_part.get("training_effect_label") or first.get("training_effect_label"),
            "garmin_training_load": _round(_sum(parts, "garmin_training_load"), 1),
            "rpe": _last(parts, "rpe"),
            "feel": _last(parts, "feel"),
            "body_battery_delta": _round(_sum(parts, "body_battery_delta")),
            "begin_stamina": first.get("begin_stamina"),
            "end_stamina": last.get("end_stamina"),
        }
    )
    return joined


def combine_metrics(
    parts: list[dict[str, Any]], metrics: list[dict[str, Any] | None], joined: dict[str, Any]
) -> dict[str, Any]:
    """``activity_metrics`` of the joined activity: load, time in zones, calories and work add up; efficiency
    comes from the joined averages; running dynamics are time-weighted. Decoupling needs the joined stream and
    is computed by the caller."""
    weights = [_seconds(p) for p, m in zip(parts, metrics) if m]
    rows = [m for m in metrics if m]
    zones: dict[str, float] = {}
    for m in rows:
        for zone, seconds in (m.get("hr_zone_seconds") or {}).items():
            zones[zone] = round(zones.get(zone, 0.0) + (seconds or 0), 1)
    ef: float | None = None
    sport, hr = joined["sport_type"], joined.get("avg_hr")
    if sport == "bike" and joined.get("normalized_power") and hr:
        ef = round(joined["normalized_power"] / hr, 4)
    elif sport == "run" and joined.get("distance_meters") and joined.get("moving_seconds") and hr:
        ef = round(joined["distance_meters"] / joined["moving_seconds"] / (hr / 60.0), 4)
    load = _sum(rows, "tss")
    return {
        "activity_id": joined["activity_id"],
        "sport_type": sport,
        "date": joined["start_time"].date(),
        "tss": round(load, 1) if load is not None else None,
        "tss_method": next((m["tss_method"] for m in rows if m.get("tss_method")), None),
        "hr_zone_seconds": zones or None,
        "efficiency_factor": ef,
        "avg_ground_contact_ms": _round(_weighted(rows, "avg_ground_contact_ms", weights), 1),
        "avg_vertical_osc_mm": _round(_weighted(rows, "avg_vertical_osc_mm", weights), 1),
        "avg_vertical_ratio_pct": _round(_weighted(rows, "avg_vertical_ratio_pct", weights), 2),
        "avg_stride_length_m": _round(_weighted(rows, "avg_stride_length_m", weights), 3),
        "swolf": _round(_weighted(rows, "swolf", weights), 1),
        "estimated_calories": _round(_sum(rows, "estimated_calories")),
        "estimated_kj": _round(_sum(rows, "estimated_kj"), 1),
        "carb_calories": _sum(rows, "carb_calories"),
        "fat_calories": _sum(rows, "fat_calories"),
    }
