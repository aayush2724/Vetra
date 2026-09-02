"""End-to-end checks over the generator, the split and the live engine.

These are the slow tests. They exist because the individually-correct pieces
can still combine into a system that leaks data between splits or answers
differently on the device than on the bench.
"""
from __future__ import annotations

import numpy as np
import pytest

from vetra_ml.config import MODEL_DIR
from vetra_ml.schema import CONDITIONS, SPECIES, TelemetryRecord, validate


# --- generator ------------------------------------------------------------
def test_every_generated_reading_is_physiologically_plausible(herd):
    """The generator must not emit anything its own validator would reject."""
    sample = herd.sample(400, random_state=0)
    for row in sample.to_dict("records"):
        row["timestamp"] = str(row["timestamp"])
        record = TelemetryRecord.from_dict(row)
        assert validate(record) == [], (record.animal_id, validate(record))


def test_generation_is_deterministic():
    from vetra_ml.synth.generator import generate_dataset

    a = generate_dataset(n_animals=4, days=1, seed=5)
    b = generate_dataset(n_animals=4, days=1, seed=5)
    assert a.equals(b)


def test_herd_covers_every_species_and_condition():
    from vetra_ml.synth.generator import generate_dataset

    big = generate_dataset(n_animals=32, days=1, seed=3)
    assert set(big.species) == set(SPECIES)
    assert set(big.label) == set(CONDITIONS)


def test_severity_ramps_from_zero_for_acute_disease(herd):
    acute = herd[herd.label == "infectious_disease"]
    if acute.empty:
        pytest.skip("no acute case in this herd")
    animal = acute[acute.animal_id == acute.animal_id.iloc[0]].sort_values("timestamp")
    assert animal.severity.iloc[0] == 0.0
    assert animal.severity.max() > 0.3
    # Severity is monotonic non-decreasing: disease does not un-happen here.
    assert (animal.severity.diff().dropna() >= -1e-9).all()


def test_chronic_disease_is_present_from_the_start(herd):
    chronic = herd[herd.label == "obesity"]
    if chronic.empty:
        pytest.skip("no chronic case in this herd")
    animal = chronic[chronic.animal_id == chronic.animal_id.iloc[0]]
    assert animal.severity.iloc[0] > 0.0


def test_sick_animals_differ_from_healthy_on_their_hallmark_channel(herd):
    """A signature that does not move its hallmark channel is a bug in the
    simulator, and would train the model on nothing."""
    healthy = herd[herd.label == "healthy"]
    hallmarks = {
        "infectious_disease": ("body_temperature", "greater"),
        "respiratory_disease": ("spo2", "less"),
        "mobility_disorder": ("step_regularity", "less"),
        "heat_stress": ("respiratory_rate", "greater"),
    }
    for condition, (channel, direction) in hallmarks.items():
        sick = herd[(herd.label == condition) & (herd.severity > 0.6)]
        if sick.empty or healthy.empty:
            continue
        if direction == "greater":
            assert sick[channel].mean() > healthy[channel].mean(), condition
        else:
            assert sick[channel].mean() < healthy[channel].mean(), condition


# --- split ----------------------------------------------------------------
def test_no_animal_appears_in_two_splits():
    """The headline methodological claim of the project, pinned by a test."""
    from vetra_ml.data import make_split

    split = make_split()
    train = set(split.meta_train.animal_id)
    val = set(split.meta_val.animal_id)
    test = set(split.meta_test.animal_id)
    assert train & val == set()
    assert train & test == set()
    assert val & test == set()


def test_split_is_reproducible_for_a_given_seed():
    from vetra_ml.data import make_split

    a = make_split(seed=11).meta_test.animal_id.unique()
    b = make_split(seed=11).meta_test.animal_id.unique()
    assert set(a) == set(b)


# --- engine ---------------------------------------------------------------
@pytest.mark.skipif(
    not (MODEL_DIR / "vetra_dx_edge.tflite").exists()
    and not (MODEL_DIR / "sklearn_model.joblib").exists(),
    reason="no trained model; run the training scripts first",
)
def test_engine_diagnoses_a_replayed_animal(herd):
    from vetra_ml.inference.engine import DiagnosisEngine

    engine = DiagnosisEngine()
    animal_id = herd.animal_id.iloc[0]
    animal = herd[herd.animal_id == animal_id].sort_values("timestamp")

    diagnoses = []
    for row in animal.to_dict("records"):
        row["timestamp"] = str(row["timestamp"])
        result = engine.ingest(TelemetryRecord.from_dict(row))
        if result:
            diagnoses.append(result)

    assert diagnoses, "a full replay produced no diagnosis at all"
    for d in diagnoses:
        assert d.condition in CONDITIONS
        assert 0.0 <= d.confidence <= 1.0
        assert abs(sum(d.probabilities.values()) - 1.0) < 0.05


@pytest.mark.skipif(
    not (MODEL_DIR / "vetra_dx_edge.tflite").exists(),
    reason="no exported TFLite model",
)
def test_engine_emits_nothing_until_a_full_window_exists(herd):
    from vetra_ml.config import SAMPLES_PER_WINDOW
    from vetra_ml.inference.engine import DiagnosisEngine

    engine = DiagnosisEngine()
    animal = herd[herd.animal_id == herd.animal_id.iloc[0]].sort_values("timestamp")

    for row in animal.head(SAMPLES_PER_WINDOW - 1).to_dict("records"):
        row["timestamp"] = str(row["timestamp"])
        assert engine.ingest(TelemetryRecord.from_dict(row)) is None


@pytest.mark.skipif(
    not (MODEL_DIR / "vetra_dx_edge.tflite").exists(),
    reason="no exported TFLite model",
)
def test_engine_detects_the_right_condition_at_high_severity(herd):
    """The system-level claim: replay a sick animal, get the right answer."""
    from vetra_ml.inference.engine import DiagnosisEngine

    engine = DiagnosisEngine()
    sick = herd[(herd.label != "healthy")]
    animal_id = sick.animal_id.iloc[0]
    truth = sick[sick.animal_id == animal_id].label.iloc[0]
    animal = herd[herd.animal_id == animal_id].sort_values("timestamp")

    severity_at = dict(zip(animal.timestamp.astype(str), animal.severity))
    hits = total = 0
    for row in animal.to_dict("records"):
        row["timestamp"] = str(row["timestamp"])
        result = engine.ingest(TelemetryRecord.from_dict(row))
        if result and severity_at.get(result.timestamp, 0.0) >= 0.5:
            total += 1
            hits += result.condition == truth

    assert total > 0, "replay never reached high severity"
    assert hits / total >= 0.8, f"only {hits}/{total} windows identified {truth}"
