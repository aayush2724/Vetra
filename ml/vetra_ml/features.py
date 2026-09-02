"""Turn a rolling window of raw samples into one diagnostic feature vector.

Two design decisions drive this module:

* **The same function serves training and the edge device.** `window_features`
  is called by the batch builder here and by `inference/engine.py` on the
  device. If the two ever diverge the model silently degrades in the field, so
  there is exactly one implementation.

* **Both absolute and self-relative views are extracted.** Species-normalised
  absolutes catch chronic conditions that were already present at enrollment
  (obesity, diabetes), while deviation from the animal's own trailing baseline
  catches acute change (fever, panting, lameness). Either view alone misses
  half the label space.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import SAMPLES_PER_WINDOW, WINDOW_STRIDE_MINUTES
from .schema import SPECIES
from .synth.generator import temperature_humidity_index
from .synth.species import profile_for

# Channels that get the full statistical treatment.
CORE_CHANNELS: tuple[str, ...] = (
    "heart_rate",
    "body_temperature",
    "respiratory_rate",
    "spo2",
    "activity_index",
    "rumination_min",
    "step_regularity",
)

# Per-channel summary statistics, in a fixed order.
STATS: tuple[str, ...] = ("mean", "std", "min", "max", "slope", "zspecies", "devbase")

# How much history feeds the animal's own baseline, in samples.
BASELINE_MINUTES = 24 * 60


def _species_reference(species: str) -> dict[str, tuple[float, float]]:
    """Midpoint and span of the healthy range for each core channel."""
    p = profile_for(species)
    ranges = {
        "heart_rate": p.heart_rate,
        "body_temperature": p.body_temperature,
        "respiratory_rate": p.respiratory_rate,
        "spo2": p.spo2,
    }
    ref = {k: (r.mid, max(r.span, 1e-6)) for k, r in ranges.items()}
    # Behavioural channels have no textbook range; use the simulator's own
    # healthy envelope so the normalisation stays on a comparable scale.
    ref["activity_index"] = (p.activity_peak.mid / 2.0, max(p.activity_peak.span, 1e-6))
    ref["rumination_min"] = (p.rumination_duty.mid, max(p.rumination_duty.span, 1e-6))
    ref["step_regularity"] = (0.88, 0.12)
    return ref


def _slope(values: np.ndarray) -> float:
    """Least-squares trend per sample. Rising fever and falling SpO2 are trends."""
    n = values.size
    if n < 2:
        return 0.0
    x = np.arange(n, dtype=np.float64)
    x -= x.mean()
    denom = float((x * x).sum())
    if denom <= 0:
        return 0.0
    return float((x * (values - values.mean())).sum() / denom)


def feature_names() -> list[str]:
    """Feature vector column names, in the exact order `window_features` emits."""
    names: list[str] = []
    for channel in CORE_CHANNELS:
        names.extend(f"{channel}__{stat}" for stat in STATS)
    names += [
        "distance_sum",
        "distance_max",
        "thi_mean",
        "thi_max",
        "ambient_temp_mean",
        "ambient_humidity_mean",
        "hr_cv",
        "activity_cv",
        "hr_activity_corr",
        "hr_at_rest",
        "rr_per_effort",
        "spo2_min",
        "rumination_fraction",
        "active_minutes_frac",
        "hour_sin",
        "hour_cos",
    ]
    names += [f"species__{s}" for s in SPECIES]
    return names


N_FEATURES = len(feature_names())


def window_features(
    window: pd.DataFrame,
    baseline: dict[str, float] | None,
    species: str,
) -> np.ndarray:
    """Compute one feature vector from `window`, a time-ordered slice of samples.

    `baseline` maps channel name to that animal's trailing median. Pass None
    during the enrollment period, when no history exists yet; the deviation
    features then read zero, which is the honest "no evidence of change" value.
    """
    ref = _species_reference(species)
    values: list[float] = []

    for channel in CORE_CHANNELS:
        series = window[channel].to_numpy(dtype=np.float64)
        mean = float(series.mean())
        mid, span = ref[channel]
        base = baseline.get(channel) if baseline else None
        values.extend(
            [
                mean,
                float(series.std()),
                float(series.min()),
                float(series.max()),
                _slope(series),
                (mean - mid) / span,
                0.0 if base is None else mean - float(base),
            ]
        )

    distance = window["distance_m"].to_numpy(dtype=np.float64)
    ambient_t = window["ambient_temp_c"].to_numpy(dtype=np.float64)
    ambient_h = window["ambient_humidity"].to_numpy(dtype=np.float64)
    thi = temperature_humidity_index(ambient_t, ambient_h)

    hr = window["heart_rate"].to_numpy(dtype=np.float64)
    activity = window["activity_index"].to_numpy(dtype=np.float64)
    spo2 = window["spo2"].to_numpy(dtype=np.float64)
    rumination = window["rumination_min"].to_numpy(dtype=np.float64)

    hr_mean = float(hr.mean())
    activity_mean = float(activity.mean())

    # Resting heart rate: the quietest quarter of the window. A healthy animal
    # drops its rate when it stops; a cardiac one does not.
    rest_cut = float(np.percentile(activity, 25))
    resting = hr[activity <= rest_cut]
    hr_at_rest = float(resting.mean()) if resting.size else hr_mean

    # Correlation between effort and heart rate. Near zero means the rate has
    # stopped tracking exertion, which is itself a cardiac warning sign.
    if hr.std() > 1e-9 and activity.std() > 1e-9:
        hr_activity_corr = float(np.corrcoef(hr, activity)[0, 1])
    else:
        hr_activity_corr = 0.0

    effort = max(activity_mean / 100.0, 0.05)
    rr_mean = float(window["respiratory_rate"].mean())

    hour = float(pd.Timestamp(window["timestamp"].iloc[-1]).hour)

    values.extend(
        [
            float(distance.sum()),
            float(distance.max()),
            float(thi.mean()),
            float(thi.max()),
            float(ambient_t.mean()),
            float(ambient_h.mean()),
            float(hr.std() / hr_mean) if hr_mean > 1e-9 else 0.0,
            float(activity.std() / activity_mean) if activity_mean > 1e-9 else 0.0,
            hr_activity_corr,
            hr_at_rest,
            rr_mean / effort,
            float(spo2.min()),
            float(rumination.mean()),
            float((activity > 15.0).mean()),
            float(np.sin(2 * np.pi * hour / 24.0)),
            float(np.cos(2 * np.pi * hour / 24.0)),
        ]
    )

    values.extend(1.0 if species == s else 0.0 for s in SPECIES)

    return np.asarray(values, dtype=np.float32)


def _trailing_baselines(animal: pd.DataFrame) -> pd.DataFrame:
    """Rolling median of each channel over prior history only.

    Shifted by one sample so a window can never contribute to the baseline it
    is compared against — that leak would make acute change look normal.
    """
    return (
        animal[list(CORE_CHANNELS)]
        .shift(1)
        .rolling(BASELINE_MINUTES, min_periods=SAMPLES_PER_WINDOW)
        .median()
    )


def build_windows(
    df: pd.DataFrame,
    stride_minutes: int = WINDOW_STRIDE_MINUTES,
    label_severity_threshold: float = 0.15,
) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    """Slice a raw telemetry frame into (X, y, metadata) for training.

    A window inherits the animal's condition only once mean severity crosses
    `label_severity_threshold`. Below that the animal is genuinely still
    presenting as healthy, and labelling it as diseased would train the model
    to hallucinate illness from noise.
    """
    feature_rows: list[np.ndarray] = []
    labels: list[str] = []
    meta_rows: list[dict] = []

    has_severity = "severity" in df.columns

    for animal_id, animal in df.groupby("animal_id", sort=True):
        animal = animal.sort_values("timestamp").reset_index(drop=True)
        species = animal["species"].iloc[0]
        baselines = _trailing_baselines(animal)

        n = len(animal)
        for end in range(SAMPLES_PER_WINDOW, n + 1, stride_minutes):
            start = end - SAMPLES_PER_WINDOW
            window = animal.iloc[start:end]

            base_row = baselines.iloc[end - 1]
            baseline = None if base_row.isna().any() else base_row.to_dict()

            feature_rows.append(window_features(window, baseline, species))

            severity = float(window["severity"].mean()) if has_severity else 0.0
            condition = animal["label"].iloc[0] if "label" in animal else "healthy"
            label = condition if severity >= label_severity_threshold else "healthy"

            labels.append(label)
            meta_rows.append(
                {
                    "animal_id": animal_id,
                    "species": species,
                    "window_end": window["timestamp"].iloc[-1],
                    "severity": severity,
                    "true_condition": condition,
                    "has_baseline": baseline is not None,
                }
            )

    X = np.vstack(feature_rows).astype(np.float32) if feature_rows else np.empty((0, N_FEATURES), np.float32)
    y = np.asarray(labels)
    meta = pd.DataFrame(meta_rows)
    return X, y, meta
