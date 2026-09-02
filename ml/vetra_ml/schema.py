"""Canonical telemetry contract.

Every layer of Vetra speaks this vocabulary: the sensor simulator publishes it
over MQTT, the edge agent stores it in SQLite, the feature builder consumes it
and the cloud API serves it. Changing a field here is a breaking change for the
whole system, so the constants below are the single source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any

# --- Raw sensor channels -------------------------------------------------
# Order matters: feature vectors are built by iterating this tuple.
VITAL_SIGNALS: tuple[str, ...] = (
    "heart_rate",          # beats per minute
    "body_temperature",    # degrees Celsius
    "respiratory_rate",    # breaths per minute
    "spo2",                # peripheral oxygen saturation, %
    "activity_index",      # normalised movement intensity, 0-100
    "rumination_min",      # minutes ruminating per sampling interval
)

# Channels describing the animal's environment, needed for heat-stress logic.
ENV_SIGNALS: tuple[str, ...] = (
    "ambient_temp_c",
    "ambient_humidity",
)

# Derived from consecutive GPS fixes rather than measured directly.
MOTION_SIGNALS: tuple[str, ...] = (
    "step_regularity",     # 0-1, gait rhythm consistency (1 = perfectly even)
    "distance_m",          # metres travelled since previous sample
)

ALL_SIGNALS: tuple[str, ...] = VITAL_SIGNALS + ENV_SIGNALS + MOTION_SIGNALS

# --- Diagnostic label space ---------------------------------------------
# Index position is the class id used by every trained model. Never reorder.
CONDITIONS: tuple[str, ...] = (
    "healthy",
    "cardiac_disorder",
    "respiratory_disease",
    "obesity",
    "diabetes",
    "heat_stress",
    "infectious_disease",
    "mobility_disorder",
)

CONDITION_TO_ID: dict[str, int] = {c: i for i, c in enumerate(CONDITIONS)}
ID_TO_CONDITION: dict[int, str] = {i: c for i, c in enumerate(CONDITIONS)}

# Human-facing copy used by the alert module and the dashboard.
CONDITION_LABELS: dict[str, str] = {
    "healthy": "Healthy",
    "cardiac_disorder": "Cardiac disorder",
    "respiratory_disease": "Respiratory disease",
    "obesity": "Obesity / over-conditioning",
    "diabetes": "Diabetes / metabolic disorder",
    "heat_stress": "Heat stress",
    "infectious_disease": "Infectious disease",
    "mobility_disorder": "Mobility disorder / lameness",
}

# How fast a farmer needs to act. Drives alert severity and routing.
CONDITION_URGENCY: dict[str, str] = {
    "healthy": "none",
    "cardiac_disorder": "critical",
    "respiratory_disease": "high",
    "obesity": "low",
    "diabetes": "medium",
    "heat_stress": "high",
    "infectious_disease": "critical",
    "mobility_disorder": "medium",
}

SPECIES: tuple[str, ...] = ("cattle", "buffalo", "goat", "sheep")


@dataclass(slots=True)
class TelemetryRecord:
    """One sensor sample for one animal at one instant."""

    animal_id: str
    species: str
    timestamp: str                  # ISO-8601, UTC, e.g. 2026-09-02T11:30:00+00:00

    heart_rate: float
    body_temperature: float
    respiratory_rate: float
    spo2: float
    activity_index: float
    rumination_min: float

    ambient_temp_c: float
    ambient_humidity: float

    latitude: float
    longitude: float
    step_regularity: float
    distance_m: float

    # Device housekeeping — carried through so the dashboard can flag dead collars.
    battery_pct: float = 100.0
    rssi_dbm: float = -60.0

    # Ground truth. Present in generated training data, absent in the field.
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TelemetryRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.timestamp)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Validation ----------------------------------------------------------
# Physiologically impossible readings mean a broken sensor, not a sick animal.
# Anything outside these bounds is dropped before it can poison a diagnosis.
PLAUSIBLE_RANGE: dict[str, tuple[float, float]] = {
    "heart_rate": (10.0, 250.0),
    "body_temperature": (30.0, 45.0),
    "respiratory_rate": (4.0, 120.0),
    "spo2": (50.0, 100.0),
    "activity_index": (0.0, 100.0),
    "rumination_min": (0.0, 1.0 * 60),
    "ambient_temp_c": (-10.0, 55.0),
    "ambient_humidity": (0.0, 100.0),
    "step_regularity": (0.0, 1.0),
    "distance_m": (0.0, 2000.0),
}


class ValidationError(ValueError):
    """Raised when an incoming payload cannot be trusted as a reading."""


def validate(record: TelemetryRecord) -> list[str]:
    """Return a list of problems with `record`; empty list means it is usable."""
    problems: list[str] = []

    if not record.animal_id:
        problems.append("animal_id is empty")
    if record.species not in SPECIES:
        problems.append(f"unknown species {record.species!r}")
    try:
        datetime.fromisoformat(record.timestamp)
    except (TypeError, ValueError):
        problems.append(f"timestamp {record.timestamp!r} is not ISO-8601")

    for signal, (low, high) in PLAUSIBLE_RANGE.items():
        value = getattr(record, signal, None)
        if value is None:
            problems.append(f"{signal} is missing")
            continue
        if not isinstance(value, (int, float)):
            problems.append(f"{signal} is not numeric")
        elif not (low <= float(value) <= high):
            problems.append(f"{signal}={value} outside plausible range [{low}, {high}]")

    if not (-90.0 <= record.latitude <= 90.0):
        problems.append(f"latitude {record.latitude} out of bounds")
    if not (-180.0 <= record.longitude <= 180.0):
        problems.append(f"longitude {record.longitude} out of bounds")

    return problems


def parse_payload(payload: dict[str, Any]) -> TelemetryRecord:
    """Build a validated record from an MQTT/REST payload, or raise."""
    try:
        record = TelemetryRecord.from_dict(payload)
    except TypeError as exc:
        raise ValidationError(f"malformed payload: {exc}") from exc

    problems = validate(record)
    if problems:
        raise ValidationError("; ".join(problems))
    return record
