"""Athlete profile model."""

from __future__ import annotations

from pydantic import BaseModel


class AthleteProfile(BaseModel):
    """Static/semi-static athlete profile data."""

    name: str
    weight_kg: float
    max_hr: int
    ftp: int | None = None
    lthr: int | None = None
    functional_threshold_pace_sec_km: int | None = None
    css_sec_100m: int | None = None
    pool_length_m: int = 25
    power_meter: str | None = None
    power_balance: str | None = None
