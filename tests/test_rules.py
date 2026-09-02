"""Clinical guardrails: these must fire on genuine danger and stay quiet on
normal animal behaviour, because every false flag costs the farmer's trust."""
from __future__ import annotations

from vetra_ml.inference.rules import evaluate_rules, worst_severity

NORMAL_CATTLE = {
    "heart_rate": 65.0, "hr_at_rest": 60.0, "body_temperature": 38.6,
    "respiratory_rate": 32.0, "spo2_min": 96.0, "activity_index": 40.0,
    "rumination_min": 0.45, "step_regularity": 0.90, "thi_mean": 70.0,
    "hour_of_day": 10.0,
}


def summary(**overrides):
    return {**NORMAL_CATTLE, **overrides}


def codes(flags):
    return {f.code for f in flags}


def test_a_normal_animal_raises_nothing():
    assert evaluate_rules(summary(), "cattle") == []


def test_fever_grades_by_magnitude():
    assert "FEVER" in codes(evaluate_rules(summary(body_temperature=40.4), "cattle"))
    assert "HIGH_FEVER" in codes(evaluate_rules(summary(body_temperature=41.2), "cattle"))


def test_hypothermia_is_critical():
    flags = evaluate_rules(summary(body_temperature=36.5), "cattle")
    assert "HYPOTHERMIA" in codes(flags)
    assert worst_severity(flags) == "critical"


def test_hypoxaemia_grades_by_magnitude():
    assert "HYPOXAEMIA" in codes(evaluate_rules(summary(spo2_min=90.0), "cattle"))
    assert "SEVERE_HYPOXAEMIA" in codes(evaluate_rules(summary(spo2_min=85.0), "cattle"))


def test_resting_tachycardia_uses_resting_rate_not_mean():
    """A grazing animal's mean heart rate is high and entirely normal."""
    grazing = summary(heart_rate=120.0, hr_at_rest=62.0)
    assert "RESTING_TACHYCARDIA" not in codes(evaluate_rules(grazing, "cattle"))

    resting_high = summary(heart_rate=115.0, hr_at_rest=115.0)
    assert "RESTING_TACHYCARDIA" in codes(evaluate_rules(resting_high, "cattle"))


def test_thresholds_are_species_relative():
    """85 bpm at rest is unremarkable for a goat (normal 70-110) and clearly
    abnormal for a buffalo (normal 40-60) — the same number, two verdicts."""
    at_rest = summary(hr_at_rest=85.0)
    assert "RESTING_TACHYCARDIA" not in codes(evaluate_rules(at_rest, "goat"))
    assert "RESTING_TACHYCARDIA" in codes(evaluate_rules(at_rest, "buffalo"))


def test_heat_load_is_advisory_until_it_is_dangerous():
    """Mild heat is true of the whole herd at once; raising it as a per-animal
    warning would bury the real alerts."""
    mild = evaluate_rules(summary(thi_mean=80.0), "cattle")
    assert worst_severity(mild) == "info"

    hot = evaluate_rules(summary(thi_mean=85.0), "cattle")
    assert worst_severity(hot) == "warning"

    dangerous = evaluate_rules(summary(thi_mean=90.0), "cattle")
    assert worst_severity(dangerous) == "critical"


def test_stillness_at_night_is_not_recumbency():
    night = summary(activity_index=1.0, hour_of_day=2.0, activity_index_baseline=30.0)
    assert "RECUMBENCY" not in codes(evaluate_rules(night, "cattle"))


def test_stillness_during_grazing_hours_is_recumbency():
    day = summary(activity_index=1.0, hour_of_day=9.0, activity_index_baseline=30.0)
    assert "RECUMBENCY" in codes(evaluate_rules(day, "cattle"))


def test_a_habitually_inactive_animal_does_not_trip_recumbency():
    """Judged against its own habit, not a herd-wide constant."""
    quiet = summary(activity_index=2.0, hour_of_day=9.0, activity_index_baseline=3.0)
    assert "RECUMBENCY" not in codes(evaluate_rules(quiet, "cattle"))


def test_rumination_collapse_is_relative_to_the_animals_own_baseline():
    dropped = summary(rumination_min=0.20, rumination_min_baseline=0.50)
    assert "RUMINATION_COLLAPSE" in codes(evaluate_rules(dropped, "cattle"))

    steady = summary(rumination_min=0.45, rumination_min_baseline=0.50)
    assert "RUMINATION_COLLAPSE" not in codes(evaluate_rules(steady, "cattle"))

    # With no baseline yet there is nothing to compare against.
    assert "RUMINATION_COLLAPSE" not in codes(evaluate_rules(summary(rumination_min=0.05), "cattle"))


def test_irregular_gait_flags_lameness():
    assert "IRREGULAR_GAIT" in codes(evaluate_rules(summary(step_regularity=0.55), "cattle"))
    assert "IRREGULAR_GAIT" not in codes(evaluate_rules(summary(step_regularity=0.88), "cattle"))


def test_worst_severity_of_nothing_is_info():
    assert worst_severity([]) == "info"
