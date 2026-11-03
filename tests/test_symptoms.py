"""Symptom checks: drop-down selections in, a diagnosis from the real model out.

The presentation tests are the important ones. Each describes a condition the
way a farmer would and checks that the unchanged edge model names it, which is
what ties the symptom form to the model the accuracy figures were measured on.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from vetra_ml.config import MODEL_DIR
from vetra_ml.schema import CONDITIONS, SPECIES
from vetra_ml.symptoms import QUESTIONS, build_signature, catalog

needs_model = pytest.mark.skipif(
    not (MODEL_DIR / "vetra_dx_edge.tflite").exists()
    and not (MODEL_DIR / "sklearn_model.joblib").exists(),
    reason="no trained model; run the training scripts first",
)

# condition -> (symptoms, weather, onset), as a farmer would describe it.
PRESENTATIONS = {
    "healthy": ({}, "warm", "recent"),
    "infectious_disease": (
        {"body_temperature": "fever", "heart_rate": "raised", "activity": "dull",
         "rumination": "stopped"}, "warm", "recent"),
    "respiratory_disease": (
        {"breathing": "laboured", "oxygen": "low", "body_temperature": "raised",
         "activity": "less"}, "warm", "recent"),
    "heat_stress": (
        {"breathing": "panting", "body_temperature": "raised", "rumination": "stopped",
         "heart_rate": "raised"}, "hot", "recent"),
    "cardiac_disorder": (
        {"heart_rate": "very_fast", "oxygen": "slightly_low", "activity": "less"}, "warm", "recent"),
    "mobility_disorder": ({"gait": "limping", "activity": "less"}, "warm", "recent"),
    "obesity": ({"activity": "dull", "gait": "stiff"}, "warm", "long"),
    "diabetes": ({"activity": "erratic", "rumination": "reduced"}, "warm", "long"),
}


@pytest.fixture(scope="module")
def checker():
    from vetra_ml.symptoms import SymptomChecker

    return SymptomChecker()


# --- catalog and signature --------------------------------------------------
def test_catalog_offers_every_species_and_a_normal_choice_for_each_symptom():
    data = catalog()
    assert data["species"] == list(SPECIES)
    for question in data["questions"]:
        keys = [o["key"] for o in question["options"]]
        assert keys[0] == "normal", question["key"]
        assert len(keys) == len(set(keys)), question["key"]
    assert data["defaults"]["weather"] in {w["key"] for w in data["weather"]}
    assert data["defaults"]["onset"] in {o["key"] for o in data["onset"]}


def test_every_presentation_uses_options_the_catalog_offers():
    offered = {q.key: {o.key for o in q.options} for q in QUESTIONS}
    assert set(PRESENTATIONS) == set(CONDITIONS)
    for symptoms, _, _ in PRESENTATIONS.values():
        for question, option in symptoms.items():
            assert option in offered[question], (question, option)


def test_selected_shifts_add_up_and_variability_takes_the_worst():
    signature = build_signature({"heart_rate": "very_fast", "activity": "erratic"}, chronic=False)
    assert signature.deltas["heart_rate"] == pytest.approx(28.0)
    assert signature.deltas["activity_index"] == pytest.approx(-10.0)
    # Both options raise heart-rate variability; the larger one wins.
    assert signature.noise_scale["heart_rate"] == pytest.approx(2.4)
    assert signature.rest_heart_rate == pytest.approx(12.0)


def test_unknown_symptom_or_option_is_rejected():
    with pytest.raises(ValueError, match="unknown symptom"):
        build_signature({"cough": "yes"}, chronic=False)
    with pytest.raises(ValueError, match="unknown option"):
        build_signature({"breathing": "wheezing"}, chronic=False)


# --- diagnosis ----------------------------------------------------------------
@needs_model
@pytest.mark.parametrize("condition", list(PRESENTATIONS))
def test_a_classic_presentation_is_diagnosed_as_that_condition(checker, condition):
    symptoms, weather, onset = PRESENTATIONS[condition]
    result = checker.diagnose("cattle", symptoms, weather=weather, onset=onset)
    assert result["condition"] == condition, result["probabilities"][:3]
    assert result["confidence"] >= 0.6
    assert result["advice"]


@needs_model
def test_result_is_complete_and_repeatable(checker):
    symptoms, weather, onset = PRESENTATIONS["respiratory_disease"]
    first = checker.diagnose("goat", symptoms, weather=weather, onset=onset)
    second = checker.diagnose("goat", symptoms, weather=weather, onset=onset)
    assert first == second

    assert sum(p["probability"] for p in first["probabilities"]) == pytest.approx(1.0, abs=0.01)
    assert [p["condition"] for p in first["probabilities"]][0] == first["condition"]
    assert 0.0 < first["agreement"] <= 1.0
    assert first["simulated_animals"] == checker.animals
    assert any(f["code"] in {"HYPOXAEMIA", "SEVERE_HYPOXAEMIA"} for f in first["findings"])
    assert first["readings"]["spo2"] < 93.0
    assert "simulated" in first["disclaimer"]


@needs_model
def test_bad_request_values_are_rejected(checker):
    with pytest.raises(ValueError, match="species"):
        checker.diagnose("camel", {})
    with pytest.raises(ValueError, match="weather"):
        checker.diagnose("cattle", {}, weather="stormy")
    with pytest.raises(ValueError, match="onset"):
        checker.diagnose("cattle", {}, onset="yesterday")


@needs_model
def test_a_check_leaves_no_state_in_the_engine(checker):
    checker.diagnose("sheep", {"gait": "limping"})
    assert checker.engine.tracked_animals == 0


# --- gateway endpoints ----------------------------------------------------------
@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    from agent import EdgeAgent, make_http_handler

    agent = EdgeAgent(tmp_path_factory.mktemp("gw") / "edge.db", enable_sync=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(agent))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    agent.stop()


def _call(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@needs_model
def test_gateway_serves_the_catalog_and_diagnoses(gateway):
    status, data = _call(f"{gateway}/symptoms")
    assert status == 200
    assert data == catalog()

    symptoms, weather, onset = PRESENTATIONS["heat_stress"]
    status, data = _call(f"{gateway}/diagnose", {
        "species": "buffalo", "symptoms": symptoms, "weather": weather, "onset": onset,
    })
    assert status == 200
    assert data["condition"] == "heat_stress"


@needs_model
def test_gateway_reports_a_bad_check_as_a_client_error(gateway):
    status, data = _call(f"{gateway}/diagnose", {"species": "camel", "symptoms": {}})
    assert status == 400
    assert "species" in data["error"]

    status, data = _call(f"{gateway}/diagnose", ["cattle"])
    assert status == 400
