"""The alert policy is the difference between a useful system and one the
farmer mutes in week two, so each suppression rule is pinned by a test."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alerts import CRITICAL_VITALS, AlertEngine, AlertPolicy, build_message, combine_severity

START = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)


def diagnosis(condition="infectious_disease", confidence=0.95, minute=0,
              rule_severity="info", flags=None, animal_id="CA-0001"):
    return {
        "animal_id": animal_id,
        "species": "cattle",
        "timestamp": (START + timedelta(minutes=minute)).isoformat(),
        "condition": condition,
        "confidence": confidence,
        "rule_severity": rule_severity,
        "rule_flags": flags or [],
    }


def test_a_single_abnormal_window_does_not_alert():
    engine = AlertEngine(AlertPolicy(confirm_count=2))
    assert engine.observe(diagnosis(minute=0)) == []


def test_confirmation_opens_the_alert():
    engine = AlertEngine(AlertPolicy(confirm_count=2))
    engine.observe(diagnosis(minute=0))
    events = engine.observe(diagnosis(minute=30))
    assert [e.action for e in events] == ["opened"]
    assert events[0].alert["condition"] == "infectious_disease"


def test_a_critical_vital_sign_bypasses_confirmation():
    """An animal whose oxygen is collapsing cannot wait for a second window."""
    engine = AlertEngine(AlertPolicy(confirm_count=3))
    events = engine.observe(diagnosis(
        condition="respiratory_disease", rule_severity="critical",
        flags=[{"code": "SEVERE_HYPOXAEMIA", "severity": "critical",
                "message": "Oxygen saturation fell to 84%"}],
    ))
    assert [e.action for e in events] == ["opened"]
    assert events[0].alert["severity"] == "critical"


HYPOTHERMIA = {"code": "HYPOTHERMIA", "severity": "critical",
               "message": "Body temperature 36.5C below normal range"}
HYPOXAEMIA = {"code": "SEVERE_HYPOXAEMIA", "severity": "critical",
              "message": "Oxygen saturation fell to 84% — urgent"}


def test_a_critical_vital_sign_alerts_even_when_the_model_says_healthy():
    """The rules are the authority on an emergency. Hypothermia is not one of
    the model's classes, so the model confidently says healthy; the alert must
    still open, immediately, carrying the finding."""
    engine = AlertEngine(AlertPolicy(confirm_count=2))
    events = engine.observe(diagnosis("healthy", confidence=1.0,
                                      rule_severity="critical", flags=[HYPOTHERMIA]))
    assert [e.action for e in events] == ["opened"]
    alert = events[0].alert
    assert alert["condition"] == CRITICAL_VITALS
    assert alert["severity"] == "critical"
    assert "veterinarian" in alert["notify"]
    assert "36.5C" in alert["message"]


def test_the_weather_alone_never_opens_an_alert_on_a_healthy_animal():
    """Severe heat load is critical by severity but is true of the whole herd at
    once; on a healthy animal it must stay silent, or every hot afternoon raises
    one alert per animal and the farmer mutes the system."""
    heat = {"code": "SEVERE_HEAT_LOAD", "severity": "critical",
            "message": "Temperature-humidity index 89 — severe heat load, act now"}
    engine = AlertEngine(AlertPolicy(confirm_count=2))
    for minute in (0, 30, 60):
        assert engine.observe(diagnosis("healthy", confidence=1.0, minute=minute,
                                        rule_severity="critical", flags=[heat])) == []
    assert engine.open_alerts() == []

    # On a confident heat-stress diagnosis the same flag still makes it critical.
    events = engine.observe(diagnosis("heat_stress", minute=90, rule_severity="critical", flags=[heat]))
    assert [e.action for e in events] == ["opened"]
    assert events[0].alert["severity"] == "critical"


def test_critical_windows_never_count_as_normal_readings():
    """Three low-confidence windows that each carry a critical finding once
    resolved an open alert as 'no longer detected'."""
    engine = AlertEngine(AlertPolicy(confirm_count=2, clear_count=3))
    for minute in (0, 30):
        engine.observe(diagnosis("respiratory_disease", minute=minute))
    for minute in (60, 90, 120):
        events = engine.observe(diagnosis("respiratory_disease", confidence=0.55, minute=minute,
                                          rule_severity="critical", flags=[HYPOXAEMIA]))
        assert all(e.action != "resolved" for e in events)
    assert [a["condition"] for a in engine.open_alerts()] == ["respiratory_disease"]


def test_a_later_diagnosis_supersedes_the_critical_vitals_alert():
    engine = AlertEngine(AlertPolicy(confirm_count=1))
    engine.observe(diagnosis("healthy", confidence=1.0, minute=0,
                             rule_severity="critical", flags=[HYPOXAEMIA]))
    events = engine.observe(diagnosis("respiratory_disease", minute=30,
                                      rule_severity="critical", flags=[HYPOXAEMIA]))
    assert [e.action for e in events] == ["opened", "resolved"]
    assert events[1].alert["condition"] == CRITICAL_VITALS
    assert [a["condition"] for a in engine.open_alerts()] == ["respiratory_disease"]


def test_a_critical_vitals_alert_resolves_after_a_healthy_run():
    engine = AlertEngine(AlertPolicy(clear_count=2))
    engine.observe(diagnosis("healthy", minute=0, rule_severity="critical", flags=[HYPOTHERMIA]))
    assert engine.observe(diagnosis("healthy", minute=30)) == []
    events = engine.observe(diagnosis("healthy", minute=60))
    assert [e.action for e in events] == ["resolved"]
    assert engine.open_alerts() == []


def test_a_steady_alert_does_not_re_notify():
    engine = AlertEngine(AlertPolicy(confirm_count=2))
    for minute in (0, 30):
        engine.observe(diagnosis(minute=minute))
    # Further identical windows must stay silent; only escalation speaks up.
    for minute in (60, 90, 120):
        assert engine.observe(diagnosis(minute=minute)) == []
    assert len(engine.open_alerts()) == 1


def test_worsening_escalates_once():
    engine = AlertEngine(AlertPolicy(confirm_count=2))
    for minute in (0, 30):
        engine.observe(diagnosis(condition="mobility_disorder", minute=minute))
    events = engine.observe(diagnosis(
        condition="mobility_disorder", minute=60, rule_severity="critical"))
    assert [e.action for e in events] == ["escalated"]
    assert events[0].alert["severity"] == "critical"


def test_alert_resolves_only_after_a_sustained_healthy_run():
    engine = AlertEngine(AlertPolicy(confirm_count=2, clear_count=3))
    for minute in (0, 30):
        engine.observe(diagnosis(minute=minute))

    assert engine.observe(diagnosis("healthy", minute=60)) == []
    assert engine.observe(diagnosis("healthy", minute=90)) == []
    events = engine.observe(diagnosis("healthy", minute=120))
    assert [e.action for e in events] == ["resolved"]
    assert engine.open_alerts() == []


def test_cooldown_blocks_immediate_reopening():
    engine = AlertEngine(AlertPolicy(confirm_count=2, clear_count=2, cooldown_hours=6))
    for minute in (0, 30):
        engine.observe(diagnosis(minute=minute))
    for minute in (60, 90):
        engine.observe(diagnosis("healthy", minute=minute))

    # Same condition, well inside the cooldown: must stay quiet.
    for minute in (120, 150):
        assert engine.observe(diagnosis(minute=minute)) == []

    # Past the cooldown it is allowed to reopen.
    actions = [e.action for minute in (600, 630)
               for e in engine.observe(diagnosis(minute=minute))]
    assert actions == ["opened"]


def test_low_confidence_windows_are_not_evidence():
    engine = AlertEngine(AlertPolicy(confirm_count=2, min_confidence=0.6))
    engine.observe(diagnosis(confidence=0.4, minute=0))
    assert engine.observe(diagnosis(confidence=0.45, minute=30)) == []


def test_severity_takes_the_worse_of_prediction_and_vitals():
    # Obesity is low urgency, but a critical measured vital outranks it.
    assert combine_severity("obesity", "info") == "low"
    assert combine_severity("obesity", "critical") == "critical"
    assert combine_severity("cardiac_disorder", "info") == "critical"


def test_headline_prefers_a_finding_that_explains_the_diagnosis():
    """Ranking on severity alone once put 'provide shade and water' on a
    cardiac alert, which is confidently wrong advice."""
    message = build_message(diagnosis(
        condition="cardiac_disorder",
        flags=[
            {"code": "HEAT_LOAD", "severity": "warning",
             "message": "Temperature-humidity index 86 - provide shade and water"},
            {"code": "RESTING_TACHYCARDIA", "severity": "warning",
             "message": "Resting heart rate 118 bpm well above normal"},
        ],
    ), "critical")
    assert "Resting heart rate" in message
    assert "shade" not in message


def test_unrelated_flags_are_omitted_from_the_headline():
    message = build_message(diagnosis(
        condition="cardiac_disorder",
        flags=[{"code": "HEAT_ADVISORY", "severity": "info",
                "message": "Temperature-humidity index 79"}],
    ), "critical")
    assert "Temperature-humidity" not in message
    assert "cardiac disorder" in message


def test_routing_escalates_with_severity():
    engine = AlertEngine(AlertPolicy(confirm_count=1))
    critical = engine.observe(diagnosis(condition="cardiac_disorder"))[0].alert
    assert "veterinarian" in critical["notify"]

    engine2 = AlertEngine(AlertPolicy(confirm_count=1))
    low = engine2.observe(diagnosis(condition="obesity", animal_id="CA-0002"))[0].alert
    assert low["notify"] == ["farmer_digest"]


def test_two_conditions_on_one_animal_are_tracked_separately():
    engine = AlertEngine(AlertPolicy(confirm_count=1))
    engine.observe(diagnosis(condition="cardiac_disorder", minute=0))
    engine.observe(diagnosis(condition="mobility_disorder", minute=30))
    assert {a["condition"] for a in engine.open_alerts()} == {
        "cardiac_disorder", "mobility_disorder"}


def test_restored_alerts_are_not_re_notified():
    engine = AlertEngine(AlertPolicy(confirm_count=1))
    engine.load_open_alerts([{
        "alert_id": "existing", "animal_id": "CA-0001", "condition": "infectious_disease",
        "condition_label": "Infectious disease", "severity": "critical", "status": "open",
        "opened_at": START.isoformat(),
    }])
    assert engine.observe(diagnosis(minute=0)) == []
