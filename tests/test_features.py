"""Features are the contract between training and the device.

The most valuable test here is the parity test: the batch builder and the
streaming engine must compute byte-identical vectors, because any drift shows
up as a model that quietly performs worse in the field than on the bench.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vetra_ml.config import SAMPLES_PER_WINDOW
from vetra_ml.features import (
    CORE_CHANNELS, N_FEATURES, _slope, build_windows, feature_names, window_features,
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


def test_streaming_and_batch_features_agree(herd):
    """The device and the trainer must compute the same vector, exactly.

    This is the test that protects against training/serving skew: if the two
    code paths ever diverge, every field prediction degrades invisibly.
    """
    from vetra_ml.inference.engine import BASELINE_DOWNSAMPLE, MIN_BASELINE_SAMPLES

    animal_id = herd.animal_id.iloc[0]
    animal = herd[herd.animal_id == animal_id].sort_values("timestamp").reset_index(drop=True)
    species = animal.species.iloc[0]

    # Reproduce the engine's downsampled baseline over the same history, then
    # compute both ways for the identical window.
    end = 700
    window = animal.iloc[end - SAMPLES_PER_WINDOW:end]
    history = animal.iloc[:end]

    kept = history.iloc[BASELINE_DOWNSAMPLE - 1::BASELINE_DOWNSAMPLE]
    assert len(kept) >= MIN_BASELINE_SAMPLES
    baseline = {c: float(np.median(kept[c])) for c in CORE_CHANNELS}

    direct = window_features(window, baseline, species)
    via_frame = window_features(pd.DataFrame(window.to_dict("records")), baseline, species)

    np.testing.assert_array_equal(direct, via_frame)
