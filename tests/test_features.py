"""Features are the contract between training and the device.

The most valuable test here is the parity test: the batch builder and the
streaming engine must compute byte-identical vectors, because any drift shows
up as a model that quietly performs worse in the field than on the bench.
"""
from __future__ import annotations

import numpy as np
import pytest

from vetra_ml.config import BASELINE_DOWNSAMPLE, SAMPLES_PER_WINDOW
from vetra_ml.features import (
    CORE_CHANNELS, N_FEATURES, _slope, build_windows, feature_names, trailing_baseline,
    window_features,
)


def test_feature_names_are_unique_and_match_width():
    names = feature_names()
    assert len(names) == N_FEATURES
    assert len(set(names)) == len(names)


def test_slope_recovers_a_known_trend():
    assert _slope(np.arange(10, dtype=float)) == pytest.approx(1.0)
    assert _slope(np.full(10, 5.0)) == pytest.approx(0.0)
    assert _slope(np.arange(10, dtype=float)[::-1]) == pytest.approx(-1.0)
    # A single sample has no trend to measure and must not divide by zero.
    assert _slope(np.array([3.0])) == 0.0


def test_window_features_shape_and_finiteness(herd):
    animal = herd[herd.animal_id == herd.animal_id.iloc[0]].head(SAMPLES_PER_WINDOW)
    vector = window_features(animal, None, animal.species.iloc[0])
    assert vector.shape == (N_FEATURES,)
    assert vector.dtype == np.float32
    # A NaN here would propagate into the model as a silent garbage prediction.
    assert np.isfinite(vector).all()


def test_missing_baseline_zeroes_deviation_features(herd):
    animal = herd[herd.animal_id == herd.animal_id.iloc[0]].head(SAMPLES_PER_WINDOW)
    names = feature_names()
    vector = window_features(animal, None, animal.species.iloc[0])
    for channel in CORE_CHANNELS:
        assert vector[names.index(f"{channel}__devbase")] == 0.0


def test_baseline_deviation_tracks_the_baseline(herd):
    animal = herd[herd.animal_id == herd.animal_id.iloc[0]].head(SAMPLES_PER_WINDOW)
    names = feature_names()
    baseline = {c: 0.0 for c in CORE_CHANNELS}
    vector = window_features(animal, baseline, animal.species.iloc[0])
    # With a zero baseline the deviation must equal the window mean.
    hr_mean = vector[names.index("heart_rate__mean")]
    assert vector[names.index("heart_rate__devbase")] == pytest.approx(hr_mean, rel=1e-5)


def test_species_one_hot_is_exclusive(herd):
    names = feature_names()
    onehot = [i for i, n in enumerate(names) if n.startswith("species__")]
    for species in herd.species.unique():
        animal = herd[herd.species == species].head(SAMPLES_PER_WINDOW)
        vector = window_features(animal, None, species)
        assert sum(vector[i] for i in onehot) == pytest.approx(1.0)


def test_build_windows_labels_only_above_the_severity_threshold(herd):
    X, y, meta = build_windows(herd)
    assert len(X) == len(y) == len(meta)
    assert X.shape[1] == N_FEATURES

    # Below the labelling threshold an animal is still presenting as healthy.
    low = meta.severity < 0.15
    assert (y[low.to_numpy()] == "healthy").all()


def test_build_windows_baseline_never_leaks_future_samples(herd):
    """The trailing baseline is shifted; the earliest windows must have none."""
    _, _, meta = build_windows(herd)
    first = meta.groupby("animal_id").head(1)
    assert not first.has_baseline.any()


def test_window_is_excluded_from_its_own_baseline():
    """A window must never contribute to the baseline it is compared against,
    otherwise an hour of fever drags the baseline up and looks less abnormal."""
    minutes = 24 * 60
    history = np.full((minutes // BASELINE_DOWNSAMPLE, len(CORE_CHANNELS)), 70.0)
    start, end = minutes - SAMPLES_PER_WINDOW, minutes
    # Corrupt exactly the window's own downsampled samples.
    window_rows = [k for k in range(len(history)) if start <= 5 * k + 4 < end]
    assert len(window_rows) == SAMPLES_PER_WINDOW // BASELINE_DOWNSAMPLE
    history[window_rows] = 500.0

    baseline = trailing_baseline(history, start, end)
    assert baseline is not None
    assert all(v == 70.0 for v in baseline.values())


def test_baseline_needs_six_hours_of_older_history():
    history = np.full((400, len(CORE_CHANNELS)), 70.0)
    # 6 h of history plus the window itself is the first point it becomes usable.
    assert trailing_baseline(history, 300, 360) is None
    assert trailing_baseline(history, 360, 420) is not None


def test_streaming_and_batch_features_agree(herd, stub_engine):
    """The device and the trainer must compute the same vector, exactly.

    This is the test that protects against training/serving skew: if the two
    code paths ever diverge, every field prediction degrades invisibly. It runs
    the real streaming engine over an animal's day and compares every window
    it diagnoses with the batch builder's row for the same window.
    """
    animal_id = herd.animal_id.iloc[0]
    animal = herd[herd.animal_id == animal_id].sort_values("timestamp").reset_index(drop=True)

    X, _, meta = build_windows(animal)

    rows = animal.assign(timestamp=animal.timestamp.astype(str)).to_dict("records")
    streamed = stub_engine.ingest_many(rows)

    assert len(streamed) == len(X) > 0
    for i, diagnosis in enumerate(streamed):
        assert str(meta.window_end.iloc[i]) == diagnosis.timestamp
        assert bool(meta.has_baseline.iloc[i]) == diagnosis.baseline_ready
        np.testing.assert_array_equal(diagnosis.features, X[i])
    assert meta.has_baseline.any(), "the comparison must cover windows with a baseline"
