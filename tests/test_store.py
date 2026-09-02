"""The store is the durability guarantee: nothing may be lost, and a retried
upload must not duplicate a record."""
from __future__ import annotations

import pytest

DIAGNOSIS = {
    "animal_id": "CA-0001", "timestamp": "2026-05-01T10:00:00+00:00",
    "condition": "heat_stress", "confidence": 0.91, "rule_severity": "warning",
}
ALERT = {
    "alert_id": "alert-1", "animal_id": "CA-0001", "condition": "heat_stress",
    "severity": "high", "status": "open", "opened_at": "2026-05-01T10:00:00+00:00",
}


def test_duplicate_readings_are_ignored(store):
    for _ in range(3):
        store.record_telemetry("CA-0001", "2026-05-01T10:00:00+00:00", {"heart_rate": 70})
    assert store.stats()["telemetry"] == 1


def test_registering_an_animal_twice_updates_rather_than_duplicates(store):
    store.register_animal("CA-0001", "cattle")
    store.register_animal("CA-0001", "cattle", name="Ganga", owner="Ramesh")
    animals = store.list_animals()
    assert len(animals) == 1
    assert animals[0]["name"] == "Ganga"


def test_rewriting_a_diagnosis_reopens_it_for_sync(store):
    store.record_diagnosis(DIAGNOSIS)
    rows = store.unsynced("diagnoses")
    store.mark_synced("diagnoses", [r["row_key"] for r in rows])
    assert store.pending_counts()["diagnoses"] == 0

    # A corrected diagnosis for the same window must be re-uploaded.
    store.record_diagnosis({**DIAGNOSIS, "condition": "infectious_disease"})
    assert store.pending_counts()["diagnoses"] == 1


def test_alerts_upsert_by_id(store):
    store.upsert_alert(ALERT)
    store.upsert_alert({**ALERT, "severity": "critical"})
    alerts = store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "critical"


def test_open_alert_lookup_ignores_resolved(store):
    store.upsert_alert(ALERT)
    assert store.open_alert_for("CA-0001", "heat_stress") is not None
    store.upsert_alert({**ALERT, "status": "resolved", "resolved_at": "2026-05-01T12:00:00+00:00"})
    assert store.open_alert_for("CA-0001", "heat_stress") is None


def test_marking_synced_is_scoped_to_the_given_keys(store):
    for minute in range(3):
        store.record_telemetry("CA-0001", f"2026-05-01T10:0{minute}:00+00:00", {"heart_rate": 70})
    rows = store.unsynced("telemetry")
    store.mark_synced("telemetry", [rows[0]["row_key"]])
    assert store.pending_counts()["telemetry"] == 2


def test_unknown_table_is_rejected(store):
    # The table name is interpolated into SQL, so the allow-list is load-bearing.
    with pytest.raises(ValueError):
        store.unsynced("telemetry; DROP TABLE alerts")
    with pytest.raises(ValueError):
        store.mark_synced("animals'--", ["x"])


def test_pruning_keeps_anything_not_yet_uploaded(store):
    store.record_telemetry("CA-0001", "2020-01-01T00:00:00+00:00", {"heart_rate": 70})
    assert store.prune_telemetry(keep_days=1) == 0   # old, but never synced

    rows = store.unsynced("telemetry")
    store.mark_synced("telemetry", [r["row_key"] for r in rows])
    assert store.prune_telemetry(keep_days=1) == 1


def test_pruning_never_touches_the_health_record(store):
    store.record_diagnosis(DIAGNOSIS)
    store.upsert_alert(ALERT)
    store.prune_telemetry(keep_days=0)
    stats = store.stats()
    assert stats["diagnoses"] == 1 and stats["alerts"] == 1


def test_recent_telemetry_comes_back_oldest_first(store):
    for minute in range(5):
        store.record_telemetry("CA-0001", f"2026-05-01T10:0{minute}:00+00:00",
                               {"heart_rate": 60 + minute})
    rows = store.recent_telemetry("CA-0001", limit=3)
    assert [r["heart_rate"] for r in rows] == [62, 63, 64]
