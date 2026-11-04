"""Cloud sync against a stand-in server: what goes up in which order, what is
stamped as uploaded, and what happens when the cloud says no."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sync import CloudSync, SyncConfig

ALERT = {
    "alert_id": "alert-1", "animal_id": "CA-0001", "condition": "heat_stress",
    "severity": "high", "status": "open", "opened_at": "2026-05-01T10:00:00+00:00",
    "updated_at": "2026-05-01T10:00:00+00:00",
}
DIAGNOSIS = {
    "animal_id": "CA-0001", "timestamp": "2026-05-01T10:00:00+00:00",
    "condition": "heat_stress", "confidence": 0.91, "rule_severity": "warning",
}


class FakeCloud:
    """Records every POST; answers with the status configured per path."""

    def __init__(self, status_for=None, on_request=None):
        self.requests: list[tuple[str, list]] = []
        self.status_for = status_for or {}
        self.on_request = on_request
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                records = json.loads(self.rfile.read(length))["records"]
                cloud.requests.append((self.path, records))
                if cloud.on_request:
                    cloud.on_request(self.path, records)
                code = cloud.status_for.get(self.path, 200)
                body = {"error": "records malformed"} if code >= 400 else {"written": len(records)}
                blob = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def paths(self):
        return [path for path, _ in self.requests]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def cloud():
    servers = []

    def make(**kwargs):
        server = FakeCloud(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def seed(store):
    store.register_animal("CA-0001", "cattle")
    for minute in range(3):
        store.record_telemetry("CA-0001", f"2026-05-01T10:0{minute}:00+00:00", {"heart_rate": 70})
    store.record_diagnosis(DIAGNOSIS)
    store.upsert_alert(ALERT)


def test_the_health_record_uploads_before_bulk_telemetry(store, cloud):
    seed(store)
    server = cloud()
    sync = CloudSync(store, SyncConfig(base_url=server.url))
    uploaded = sync.sync_once()
    assert server.paths() == [
        "/api/sync/animals", "/api/sync/diagnoses", "/api/sync/alerts", "/api/sync/telemetry",
    ]
    assert uploaded == {"animals": 1, "diagnoses": 1, "alerts": 1, "telemetry": 3}
    assert sync.online
    assert all(n == 0 for n in store.pending_counts().values())


def test_an_alert_that_changes_during_upload_goes_up_again(store, cloud):
    """Stamping the batch blindly left the cloud with a stale alert for ever."""
    seed(store)

    def escalate_mid_flight(path, records):
        if path == "/api/sync/alerts":
            store.upsert_alert({**ALERT, "severity": "critical",
                                "updated_at": "2026-05-01T10:30:00+00:00"})

    server = cloud(on_request=escalate_mid_flight)
    sync = CloudSync(store, SyncConfig(base_url=server.url))
    sync.sync_once()
    assert store.pending_counts()["alerts"] == 1

    sync.sync_once()
    assert store.pending_counts()["alerts"] == 0
    sent = [records for path, records in server.requests if path == "/api/sync/alerts"]
    assert [batch[0]["severity"] for batch in sent] == ["high", "critical"]


def test_a_batch_the_cloud_rejects_as_malformed_is_dropped_not_retried(store, cloud):
    seed(store)
    server = cloud(status_for={"/api/sync/diagnoses": 400})
    sync = CloudSync(store, SyncConfig(base_url=server.url))
    sync.sync_once()
    # The rejected rows leave the queue, everything behind them still uploads.
    assert store.pending_counts() == {"animals": 0, "telemetry": 0, "diagnoses": 0, "alerts": 0}
    assert sync.dropped_total == 1
    assert sync.online
    assert "/api/sync/alerts" in server.paths() and "/api/sync/telemetry" in server.paths()


def test_an_oversized_batch_shrinks_the_batch_size(store, cloud):
    seed(store)
    server = cloud(status_for={"/api/sync/telemetry": 413})
    sync = CloudSync(store, SyncConfig(base_url=server.url, batch_size=4))
    sync.sync_once()
    assert sync.config.batch_size == 2
    assert store.pending_counts()["telemetry"] == 3
    assert sync.online


def test_a_dead_link_stops_the_pass_and_keeps_every_row(store, cloud):
    seed(store)
    server = cloud()
    server.close()
    sync = CloudSync(store, SyncConfig(base_url=server.url, timeout_seconds=1.0))
    assert sync.sync_once() == {}
    assert not sync.online
    assert sync.last_error
    assert store.pending_counts()["alerts"] == 1
