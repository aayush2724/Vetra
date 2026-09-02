"""Healthy-baseline physiology per species.

Values are adult resting reference ranges from standard veterinary texts; see
`docs/physiology-references.md` for the citation behind each row. `low`/`high`
bracket the normal range, and the generator samples an individual's personal
baseline inside that bracket — real animals sit at a stable point within the
range, they do not wander across it at random.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Range:
    low: float
    high: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def span(self) -> float:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class SpeciesProfile:
    """Resting reference ranges and short-term variability for one species."""

    name: str
    heart_rate: Range
    body_temperature: Range
    respiratory_rate: Range
    spo2: Range
    # Minutes ruminating per 1-minute sample, expressed as a duty cycle 0-1.
    rumination_duty: Range
    # Typical daytime peak of the 0-100 activity index.
    activity_peak: Range
    # Metres covered in an active minute while grazing.
    stride_m_per_min: Range
    # Minute-to-minute sensor + physiological jitter (std-dev), per channel.
    noise: dict[str, float]


# Rumination duty cycles reflect ~7-9 h/day for cattle and buffalo and
# ~6-8 h/day for small ruminants, spread across the animal's resting periods.
PROFILES: dict[str, SpeciesProfile] = {
    "cattle": SpeciesProfile(
        name="cattle",
        heart_rate=Range(48, 84),
        body_temperature=Range(38.0, 39.3),
        respiratory_rate=Range(26, 50),
        spo2=Range(95, 99),
        rumination_duty=Range(0.30, 0.42),
        activity_peak=Range(45, 70),
        stride_m_per_min=Range(8, 22),
        noise={
            "heart_rate": 3.0,
            "body_temperature": 0.10,
            "respiratory_rate": 2.2,
            "spo2": 0.5,
            "activity_index": 6.0,
            "rumination_min": 0.10,
            "step_regularity": 0.03,
        },
    ),
    "buffalo": SpeciesProfile(
        name="buffalo",
        heart_rate=Range(40, 60),
        body_temperature=Range(37.2, 38.6),
        respiratory_rate=Range(12, 36),
        spo2=Range(95, 99),
        rumination_duty=Range(0.32, 0.45),
        activity_peak=Range(35, 60),
        stride_m_per_min=Range(6, 18),
        noise={
            "heart_rate": 2.5,
            "body_temperature": 0.10,
            "respiratory_rate": 2.0,
            "spo2": 0.5,
            "activity_index": 5.5,
            "rumination_min": 0.10,
            "step_regularity": 0.03,
        },
    ),
    "goat": SpeciesProfile(
        name="goat",
        heart_rate=Range(70, 110),
        body_temperature=Range(38.6, 40.0),
        respiratory_rate=Range(15, 30),
        spo2=Range(95, 99),
        rumination_duty=Range(0.24, 0.36),
        activity_peak=Range(55, 85),
        stride_m_per_min=Range(10, 28),
        noise={
            "heart_rate": 4.5,
            "body_temperature": 0.12,
            "respiratory_rate": 1.8,
            "spo2": 0.6,
            "activity_index": 8.0,
            "rumination_min": 0.10,
            "step_regularity": 0.04,
        },
    ),
    "sheep": SpeciesProfile(
        name="sheep",
        heart_rate=Range(70, 90),
        body_temperature=Range(38.3, 39.9),
        respiratory_rate=Range(16, 34),
        spo2=Range(95, 99),
        rumination_duty=Range(0.26, 0.38),
        activity_peak=Range(50, 78),
        stride_m_per_min=Range(9, 24),
        noise={
            "heart_rate": 4.0,
            "body_temperature": 0.12,
            "respiratory_rate": 1.9,
            "spo2": 0.6,
            "activity_index": 7.5,
            "rumination_min": 0.10,
            "step_regularity": 0.04,
        },
    ),
}


def profile_for(species: str) -> SpeciesProfile:
    try:
        return PROFILES[species]
    except KeyError as exc:
        raise KeyError(
            f"no physiology profile for {species!r}; known: {sorted(PROFILES)}"
        ) from exc
