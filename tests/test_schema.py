"""The telemetry contract is what every layer agrees on, so it is tested first."""
from __future__ import annotations

import pytest

from vetra_ml.schema import (
    CONDITIONS, CONDITION_TO_ID, CONDITION_URGENCY, ID_TO_CONDITION,
    TelemetryRecord, ValidationError, parse_payload, utc_now_iso, validate,
)


def make_record(**overrides) -> TelemetryRecord:
    base = dict(
        animal_id="CA-0001", species="cattle", timestamp=utc_now_iso(),
        heart_rate=68.0, body_temperature=38.6, respiratory_rate=30.0, spo2=97.0,
        activity_index=40.0, rumination_min=0.5, ambient_temp_c=30.0,
        ambient_humidity=60.0, latitude=26.85, longitude=80.94,
        step_regularity=0.9, distance_m=12.0,
    )
    base.update(overrides)
    return TelemetryRecord(**base)


def test_healthy_record_validates():
    assert validate(make_record()) == []


def test_class_id_mapping_is_a_bijection():
    # Model outputs are indexed by these ids; a mismatch silently relabels
    # every prediction, so the mapping is pinned by a test.
    assert len(CONDITIONS) == len(set(CONDITIONS))
    assert all(ID_TO_CONDITION[CONDITION_TO_ID[c]] == c for c in CONDITIONS)
    assert set(CONDITION_URGENCY) == set(CONDITIONS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spo2", 20.0),                 # below any survivable saturation
        ("heart_rate", 400.0),          # sensor artefact, not tachycardia
        ("body_temperature", 55.0),
        ("respiratory_rate", -3.0),
        ("step_regularity", 1.7),
        ("ambient_humidity", 140.0),
    ],
)
def test_implausible_readings_are_rejected(field, value):
    problems = validate(make_record(**{field: value}))
    assert any(field in p for p in problems), problems


def test_out_of_bounds_gps_is_rejected():
    assert any("latitude" in p for p in validate(make_record(latitude=120.0)))
    assert any("longitude" in p for p in validate(make_record(longitude=-500.0)))


def test_unknown_species_is_rejected():
    assert any("species" in p for p in validate(make_record(species="llama")))


def test_bad_timestamp_is_rejected():
    assert any("ISO-8601" in p for p in validate(make_record(timestamp="yesterday")))


def test_round_trip_through_dict():
    record = make_record()
    assert TelemetryRecord.from_dict(record.to_dict()) == record


def test_from_dict_ignores_unknown_fields():
    payload = make_record().to_dict()
    payload["firmware_version"] = "2.1.0"   # a field a future collar might add
    assert TelemetryRecord.from_dict(payload).animal_id == "CA-0001"


def test_parse_payload_raises_on_bad_input():
    with pytest.raises(ValidationError):
        parse_payload({"animal_id": "X"})
    with pytest.raises(ValidationError):
        parse_payload(make_record(spo2=10.0).to_dict())
