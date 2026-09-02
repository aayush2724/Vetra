"""Simulate continuous multi-day telemetry for a herd.

The simulator builds each animal from three layers, in this order:

1. A personal healthy baseline drawn once from the species reference range.
2. Circadian and environmental dynamics — grazing peaks, diurnal temperature,
   the coupling of heart and respiratory rate to movement and to heat load.
3. A disease signature scaled by a severity that ramps up from an onset time.

Layer 2 is what makes the problem non-trivial: a hard threshold on respiratory
rate would fire every hot afternoon, so the models have to learn the deviation
from an animal's *own* rhythm rather than from a population constant.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..config import RANDOM_SEED, SAMPLE_INTERVAL_MIN
from ..schema import CONDITIONS, SPECIES
from .conditions import signature_for
from .species import SpeciesProfile, profile_for

MINUTES_PER_DAY = 24 * 60

# Farm centroid: a representative smallholder location (Uttar Pradesh, India).
FARM_LAT, FARM_LON = 26.8467, 80.9462
GRAZING_RADIUS_M = 400.0

# Metres per degree, adequate for a few-hundred-metre paddock.
M_PER_DEG_LAT = 111_320.0


@dataclass(slots=True)
class AnimalPlan:
    """Everything the simulator needs to render one animal's history."""

    animal_id: str
    species: str
    condition: str
    onset_minute: int          # index at which severity starts to rise
    ramp_minutes: int          # how long it takes to reach plateau severity
    peak_severity: float       # plateau severity in [0, 1]


def temperature_humidity_index(temp_c: np.ndarray, humidity: np.ndarray) -> np.ndarray:
    """Livestock THI. Above ~72 cattle begin to show measurable heat load."""
    return 0.8 * temp_c + (humidity / 100.0) * (temp_c - 14.4) + 46.4


def _circadian_activity(minute_of_day: np.ndarray) -> np.ndarray:
    """Grazing rhythm in [0, 1]: peaks after dawn and before dusk, near zero at night."""
    hour = minute_of_day / 60.0
    morning = np.exp(-0.5 * ((hour - 7.5) / 1.8) ** 2)
    evening = np.exp(-0.5 * ((hour - 17.0) / 2.0) ** 2)
    midday = 0.35 * np.exp(-0.5 * ((hour - 12.5) / 2.5) ** 2)
    return np.clip(morning + evening + midday, 0.0, 1.0)


def _ambient_conditions(
    minute_of_day: np.ndarray, day_index: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Diurnal ambient temperature and humidity for a hot-season Indian farm."""
    hour = minute_of_day / 60.0
    # Coolest around 05:00, hottest around 15:00.
    diurnal = -np.cos((hour - 5.0) / 24.0 * 2 * np.pi)
    day_offset = rng.normal(0.0, 1.5, size=day_index.max() + 1)[day_index]

    temp = 30.0 + 7.0 * diurnal + day_offset
    temp += rng.normal(0.0, 0.4, size=temp.shape)

    # Humidity moves opposite to temperature.
    humidity = 62.0 - 18.0 * diurnal + rng.normal(0.0, 2.5, size=temp.shape)
    return np.clip(temp, -5.0, 52.0), np.clip(humidity, 5.0, 99.0)


def _severity_curve(n: int, plan: AnimalPlan, chronic: bool) -> np.ndarray:
    """Severity in [0, 1] over time: flat for chronic disease, ramped for acute."""
    if plan.condition == "healthy":
        return np.zeros(n)
    if chronic:
        # Chronic disease is already established when monitoring begins.
        return np.full(n, plan.peak_severity)

    severity = np.zeros(n)
    ramp_end = min(n, plan.onset_minute + plan.ramp_minutes)
    if plan.onset_minute < n:
        ramp = np.linspace(0.0, plan.peak_severity, max(ramp_end - plan.onset_minute, 1))
        severity[plan.onset_minute:ramp_end] = ramp
        severity[ramp_end:] = plan.peak_severity
    return severity


def simulate_animal(
    plan: AnimalPlan,
    start: datetime,
    days: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Render one animal's minute-resolution telemetry as a DataFrame."""
    profile: SpeciesProfile = profile_for(plan.species)
    signature = signature_for(plan.condition)

    n = days * MINUTES_PER_DAY // SAMPLE_INTERVAL_MIN
    t = np.arange(n)
    minute_of_day = (t * SAMPLE_INTERVAL_MIN) % MINUTES_PER_DAY
    day_index = (t * SAMPLE_INTERVAL_MIN) // MINUTES_PER_DAY

    # --- Layer 1: this animal's personal healthy set-point ----------------
    def personal(r) -> float:
        # Individuals sit near the middle of the range far more often than at
        # the edges, so draw from a truncated normal rather than a uniform.
        value = rng.normal(r.mid, r.span / 5.0)
        return float(np.clip(value, r.low, r.high))

    hr_base = personal(profile.heart_rate)
    temp_base = personal(profile.body_temperature)
    rr_base = personal(profile.respiratory_rate)
    spo2_base = personal(profile.spo2)
    rumination_base = personal(profile.rumination_duty)
    activity_peak = personal(profile.activity_peak)
    stride = personal(profile.stride_m_per_min)

    # --- Layer 2: circadian + environmental dynamics ----------------------
    ambient_temp, humidity = _ambient_conditions(minute_of_day, day_index, rng)
    severity = _severity_curve(n, plan, signature.chronic)

    if signature.heat_driven:
        # A heat-stressed animal is one exposed to a genuine heat load, so push
        # this animal's environment into the range where THI actually bites.
        ambient_temp = ambient_temp + 6.0 * severity
        humidity = np.clip(humidity + 12.0 * severity, 5.0, 99.0)

    thi = temperature_humidity_index(ambient_temp, humidity)
    heat_load = np.clip((thi - 72.0) / 12.0, 0.0, 1.5)

    drive = _circadian_activity(minute_of_day)
    # Heat suppresses grazing regardless of health status.
    drive = drive * (1.0 - 0.25 * np.clip(heat_load, 0.0, 1.0))

    noise = profile.noise

    def scaled_noise(channel: str) -> np.ndarray:
        base = noise.get(channel, 1.0)
        factor = 1.0 + (signature.noise_scale.get(channel, 1.0) - 1.0) * severity
        return rng.normal(0.0, 1.0, size=n) * base * factor

    activity = activity_peak * drive + scaled_noise("activity_index")
    activity = np.clip(activity, 0.0, 100.0)
    effort = activity / 100.0

    heart_rate = hr_base + 0.55 * hr_base * effort + 4.0 * heat_load + scaled_noise("heart_rate")
    respiratory_rate = (
        rr_base + 0.45 * rr_base * effort + 9.0 * heat_load + scaled_noise("respiratory_rate")
    )
    body_temperature = (
        temp_base
        + 0.35 * np.sin((minute_of_day / MINUTES_PER_DAY) * 2 * np.pi - np.pi / 2)
        + 0.30 * effort
        + 0.45 * heat_load
        + scaled_noise("body_temperature")
    )
    spo2 = spo2_base - 0.8 * effort + scaled_noise("spo2")

    # Ruminants chew the cud while resting, so rumination is the inverse of grazing.
    rumination = rumination_base * (1.0 - 0.85 * drive) * 2.0 + scaled_noise("rumination_min")

    step_regularity = 0.90 - 0.04 * effort + scaled_noise("step_regularity")
    distance = stride * effort + rng.normal(0.0, 1.0, size=n)

    # --- Layer 3: disease signature, scaled by severity -------------------
    channel_arrays = {
        "heart_rate": heart_rate,
        "respiratory_rate": respiratory_rate,
        "body_temperature": body_temperature,
        "spo2": spo2,
        "activity_index": activity,
        "rumination_min": rumination,
        "step_regularity": step_regularity,
    }
    for channel, delta in signature.deltas.items():
        if channel in channel_arrays:
            channel_arrays[channel] = channel_arrays[channel] + delta * severity

    if signature.distance_factor != 1.0:
        distance = distance * (1.0 - (1.0 - signature.distance_factor) * severity)

    # A cardiac animal cannot bring its rate down when it stops moving; encode
    # that decoupling explicitly, since the mean rate alone under-describes it.
    if plan.condition == "cardiac_disorder":
        channel_arrays["heart_rate"] = channel_arrays["heart_rate"] + 12.0 * severity * (1.0 - effort)

    heart_rate = np.clip(channel_arrays["heart_rate"], 10.0, 250.0)
    respiratory_rate = np.clip(channel_arrays["respiratory_rate"], 4.0, 120.0)
    body_temperature = np.clip(channel_arrays["body_temperature"], 30.0, 45.0)
    spo2 = np.clip(channel_arrays["spo2"], 50.0, 100.0)
    activity = np.clip(channel_arrays["activity_index"], 0.0, 100.0)
    rumination = np.clip(channel_arrays["rumination_min"], 0.0, 1.0)
    step_regularity = np.clip(channel_arrays["step_regularity"], 0.0, 1.0)
    distance = np.clip(distance, 0.0, 2000.0)

    # --- GPS: bounded random walk around the farm -------------------------
    heading = rng.uniform(0, 2 * np.pi, size=n).cumsum() % (2 * np.pi)
    dx = np.cumsum(distance * np.cos(heading))
    dy = np.cumsum(distance * np.sin(heading))
    radius = np.hypot(dx, dy)
    # Pull the animal back whenever the walk drifts outside the paddock.
    over = radius > GRAZING_RADIUS_M
    scale = np.where(over, GRAZING_RADIUS_M / np.maximum(radius, 1e-6), 1.0)
    dx, dy = dx * scale, dy * scale

    m_per_deg_lon = M_PER_DEG_LAT * np.cos(np.deg2rad(FARM_LAT))
    latitude = FARM_LAT + dy / M_PER_DEG_LAT
    longitude = FARM_LON + dx / m_per_deg_lon

    timestamps = pd.date_range(
        start=start, periods=n, freq=f"{SAMPLE_INTERVAL_MIN}min", tz=timezone.utc
    )

    return pd.DataFrame(
        {
            "animal_id": plan.animal_id,
            "species": plan.species,
            "timestamp": timestamps,
            "heart_rate": heart_rate.round(2),
            "body_temperature": body_temperature.round(3),
            "respiratory_rate": respiratory_rate.round(2),
            "spo2": spo2.round(2),
            "activity_index": activity.round(2),
            "rumination_min": rumination.round(4),
            "ambient_temp_c": ambient_temp.round(2),
            "ambient_humidity": humidity.round(2),
            "latitude": latitude.round(6),
            "longitude": longitude.round(6),
            "step_regularity": step_regularity.round(4),
            "distance_m": distance.round(2),
            "battery_pct": np.clip(100.0 - t / n * rng.uniform(5, 25), 0, 100).round(1),
            "rssi_dbm": np.clip(rng.normal(-65, 8, size=n), -110, -30).round(1),
            "label": plan.condition,
            "severity": severity.round(4),
        }
    )


def build_herd_plans(
    n_animals: int,
    rng: np.random.Generator,
    days: int,
    healthy_fraction: float = 0.35,
) -> list[AnimalPlan]:
    """Assign species, condition and disease timing across the herd.

    Healthy animals are deliberately over-represented relative to a uniform
    split, because a herd where one animal in eight has each disease is not a
    herd any farmer would recognise.
    """
    diseases = [c for c in CONDITIONS if c != "healthy"]
    n_healthy = int(round(n_animals * healthy_fraction))
    n_sick = n_animals - n_healthy

    assignments = ["healthy"] * n_healthy
    for i in range(n_sick):
        assignments.append(diseases[i % len(diseases)])
    rng.shuffle(assignments)

    total_minutes = days * MINUTES_PER_DAY
    plans: list[AnimalPlan] = []
    for i, condition in enumerate(assignments):
        species = SPECIES[i % len(SPECIES)]
        # Onset lands inside the first two-thirds so every sick animal shows
        # both a healthy stretch and a symptomatic stretch.
        onset = int(rng.integers(MINUTES_PER_DAY // 4, max(int(total_minutes * 0.66), MINUTES_PER_DAY // 2)))
        plans.append(
            AnimalPlan(
                animal_id=f"{species[:2].upper()}-{i:04d}",
                species=species,
                condition=condition,
                onset_minute=onset,
                ramp_minutes=int(rng.integers(6 * 60, 36 * 60)),
                peak_severity=float(rng.uniform(0.55, 1.0)),
            )
        )
    return plans


def generate_dataset(
    n_animals: int = 240,
    days: int = 5,
    seed: int = RANDOM_SEED,
    start: datetime | None = None,
    healthy_fraction: float = 0.35,
) -> pd.DataFrame:
    """Generate a full herd history as one tidy DataFrame."""
    rng = np.random.default_rng(seed)
    if start is None:
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)  # peak Indian hot season

    plans = build_herd_plans(n_animals, rng, days, healthy_fraction)
    frames = [simulate_animal(plan, start, days, rng) for plan in plans]
    return pd.concat(frames, ignore_index=True)
