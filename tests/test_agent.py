"""The gateway's network surface: readings in over REST, diagnoses and
counters out, and an MQTT client that keeps trying rather than giving up."""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from vetra_ml.config import MODEL_DIR, SAMPLES_PER_WINDOW
from vetra_ml.schema import CONDITIONS

needs_model = pytest.mark.skipif(
    not (MODEL_DIR / "vetra_dx_edge.tflite").exists()
    and not (MODEL_DIR / "sklearn_model.joblib").exists(),
    reason="no trained model; run the training scripts first",
)

START = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)


def reading(minute: int, animal_id: str = "CA-0001", **overrides) -> dict:
    base = dict(
        animal_id=animal_id, species="cattle",
        timestamp=(START + timedelta(minutes=minute)).isoformat(),
        heart_rate=68.0, body_temperature=38.6, respiratory_rate=30.0, spo2=97.0,
        activity_index=40.0, rumination_min=0.5, ambient_temp_c=30.0,
        ambient_humidity=60.0, latitude=26.85, longitude=80.94,
        step_regularity=0.9, distance_m=12.0,
    )
    base.update(overrides)
    return base


def _call(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    from agent import EdgeAgent, make_http_handler

    agent = EdgeAgent(tmp_path_factory.mktemp("gw") / "edge.db", enable_sync=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(agent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    agent.stop()


@needs_model
def test_ingest_accepts_a_batch_and_rejects_implausible_readings(gateway):
    records = [reading(m) for m in range(5)] + [reading(5, spo2=20.0)]
    status, data = _call(f"{gateway}/ingest", {"records": records})
    assert status == 200
    assert (data["accepted"], data["rejected"]) == (5, 1)
    assert "spo2" in data["results"][-1]["error"]

    _, status_body = _call(f"{gateway}/status")
    assert status_body["counters"]["received"] == 5
    assert status_body["counters"]["rejected"] == 1
    assert status_body["model_backend"] != "none"

    _, animals = _call(f"{gateway}/animals")
    assert [a["animal_id"] for a in animals["animals"]] == ["CA-0001"]


@needs_model
def test_a_full_window_produces_exactly_one_diagnosis(gateway):
    records = [reading(m, animal_id="CA-0002") for m in range(SAMPLES_PER_WINDOW)]
    _, data = _call(f"{gateway}/ingest", {"records": records})
    diagnosed = [r["diagnosis"] for r in data["results"] if r.get("diagnosis")]
    assert len(diagnosed) == 1
    assert diagnosed[0]["condition"] in CONDITIONS

    _, body = _call(f"{gateway}/diagnoses")
    assert body["diagnoses"][0]["animal_id"] == "CA-0002"


@needs_model
def test_unknown_paths_and_malformed_bodies_are_client_errors(gateway):
    assert _call(f"{gateway}/nope")[0] == 404
    status, data = _call(f"{gateway}/ingest", {"records": "not-a-list"})
    assert status == 400
    assert "records" in data["error"]


def test_mqtt_keeps_retrying_when_no_broker_is_listening():
    """Containers and brokers come up in any order; the gateway must not give
    up on MQTT because the broker was a few seconds late."""
    from agent import start_mqtt

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The port is closed again here, so nothing accepts the connection.

    agent = SimpleNamespace(handle_payload=lambda payload: None)
    client = start_mqtt(agent, "127.0.0.1", port)
    try:
        assert client is not None
        assert not client.is_connected()
    finally:
        client.loop_stop()
        client.disconnect()
