"""The edge gateway: ingest telemetry, diagnose offline, alert, sync when able.

This is the process that runs on the farm's Raspberry Pi. It accepts readings
over MQTT and over REST — the objectives call for both, and in practice collars
speak MQTT while a phone or a lab rig posts JSON — normalises them through the
one telemetry schema, and drives the diagnosis engine, the alert engine, the
local record store and the cloud sync.

Everything after ingestion is offline-capable by construction: diagnosis and
alerting depend on nothing but the local model file, and the cloud is treated
purely as somewhere to copy records to when a link happens to exist.

Run:  python edge/agent.py --http-port 5001
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ml"))
sys.path.insert(0, str(ROOT / "edge"))

from alerts import AlertEngine, AlertEvent, AlertPolicy   # noqa: E402
from store import EdgeStore                               # noqa: E402
from sync import CloudSync, SyncConfig                    # noqa: E402
from vetra_ml.inference.engine import DiagnosisEngine     # noqa: E402
from vetra_ml.schema import ValidationError, parse_payload  # noqa: E402

log = logging.getLogger("vetra.agent")

MQTT_TOPIC = "vetra/telemetry/+"


class EdgeAgent:
    """Owns the device's state and the pipeline every reading passes through."""

    def __init__(
        self,
        db_path: Path,
        sync_config: SyncConfig | None = None,
        policy: AlertPolicy | None = None,
        enable_sync: bool = True,
        retention_days: int = 7,
    ) -> None:
        self.store = EdgeStore(db_path)
        self.engine = DiagnosisEngine()
        self.alerts = AlertEngine(policy or AlertPolicy(), on_event=self._on_alert_event)
        # Restore open alerts so a restart does not re-notify about animals the
        # farmer has already been told about.
        self.alerts.load_open_alerts(self.store.list_alerts(status="open"))

        self.sync = CloudSync(self.store, sync_config or SyncConfig()) if enable_sync else None
        self._lock = threading.Lock()
        self.counters = {"received": 0, "rejected": 0, "diagnoses": 0, "alerts": 0, "pruned": 0}
        self.started_at = datetime.now(timezone.utc)
        self.retention_days = retention_days
        self._prune_stop = threading.Event()
        self._prune_thread: threading.Thread | None = None

        log.info("model backend: %s", self.engine.backend)

    # --- pipeline --------------------------------------------------------
    def handle_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate, store, diagnose and alert on one reading."""
        try:
            record = parse_payload(payload)
        except ValidationError as exc:
            # A malformed or implausible reading is dropped loudly rather than
            # fed to the model, where it would produce a confident wrong answer.
            with self._lock:
                self.counters["rejected"] += 1
            log.warning("rejected reading: %s", exc)
            return {"accepted": False, "error": str(exc)}

        with self._lock:
            self.counters["received"] += 1

        self.store.register_animal(record.animal_id, record.species)
        self.store.record_telemetry(record.animal_id, record.timestamp, record.to_dict())

        diagnosis = self.engine.ingest(record)
        if diagnosis is None:
            return {"accepted": True, "diagnosis": None}

        payload_out = diagnosis.to_dict()
        self.store.record_diagnosis(payload_out)
        with self._lock:
            self.counters["diagnoses"] += 1

        events = self.alerts.observe(payload_out)
        return {
            "accepted": True,
            "diagnosis": payload_out,
            "alert_events": [{"action": e.action, "alert_id": e.alert["alert_id"]} for e in events],
        }

    def _on_alert_event(self, event: AlertEvent) -> None:
        self.store.upsert_alert(event.alert)
        with self._lock:
            self.counters["alerts"] += 1
        alert = event.alert
        log.warning(
            "[%s] %s severity=%s notify=%s :: %s",
            event.action.upper(), alert["animal_id"], alert["severity"],
            ",".join(alert["notify"]), alert["message"],
        )

    # --- lifecycle -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        uptime = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return {
            "uptime_seconds": round(uptime, 1),
            "model_backend": self.engine.backend,
            "tracked_animals": self.engine.tracked_animals,
            "counters": dict(self.counters),
            "store": self.store.stats(),
            "sync": self.sync.status() if self.sync else {"enabled": False},
        }

    def _prune_loop(self) -> None:
        """Reclaim disk from raw samples that are already safely in the cloud.

        Minute-resolution telemetry dominates the database — roughly 0.6 KB a
        row, so a 240-animal herd writes about 200 MB a day. Diagnoses and
        alerts are the permanent health record and are never touched here.
        """
        while not self._prune_stop.wait(3600.0):
            try:
                removed = self.store.prune_telemetry(keep_days=self.retention_days)
                if removed:
                    with self._lock:
                        self.counters["pruned"] += removed
                    log.info("pruned %d uploaded telemetry rows older than %d days",
                             removed, self.retention_days)
            except Exception:
                log.exception("telemetry prune failed")

    def start(self) -> None:
        if self.sync:
            self.sync.start()
        self._prune_thread = threading.Thread(target=self._prune_loop, name="vetra-prune", daemon=True)
        self._prune_thread.start()

    def stop(self) -> None:
        self._prune_stop.set()
        if self.sync:
            self.sync.stop()
        self.store.close()


# --- REST ingestion ------------------------------------------------------
def make_http_handler(agent: EdgeAgent):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieten the default access log
            log.debug("http %s", fmt % args)

        def _send(self, status: int, body: dict) -> None:
            blob = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            # The dashboard may be served from a different origin during development.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(blob)

        def do_OPTIONS(self):  # noqa: N802 - BaseHTTPRequestHandler naming
            self._send(204, {})

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._send(200, {"status": "ok"})
            if self.path == "/status":
                return self._send(200, agent.status())
            if self.path.startswith("/alerts"):
                status = "open" if self.path.endswith("/open") else None
                return self._send(200, {"alerts": agent.store.list_alerts(status=status)})
            if self.path == "/animals":
                return self._send(200, {"animals": agent.store.list_animals()})
            if self.path.startswith("/diagnoses"):
                return self._send(200, {"diagnoses": agent.store.recent_diagnoses(limit=100)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path not in ("/ingest", "/api/telemetry"):
                return self._send(404, {"error": "not found"})

            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 5_000_000:
                return self._send(400, {"error": "missing or oversized body"})

            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                return self._send(400, {"error": f"invalid JSON: {exc}"})

            # Accept a single reading or a batch; collars buffer while offline
            # and then flush everything at once.
            records = body.get("records") if isinstance(body, dict) and "records" in body else [body]
            if not isinstance(records, list):
                return self._send(400, {"error": "`records` must be a list"})

            results = [agent.handle_payload(r) for r in records]
            accepted = sum(1 for r in results if r.get("accepted"))
            return self._send(200, {
                "accepted": accepted,
                "rejected": len(results) - accepted,
                "results": results,
            })

    return Handler


# --- MQTT ingestion ------------------------------------------------------
def start_mqtt(agent: EdgeAgent, host: str, port: int, topic: str = MQTT_TOPIC):
    """Subscribe to the collar topic. Returns None if MQTT is unavailable."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.warning("paho-mqtt not installed; MQTT ingestion disabled (REST still active)")
        return None

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe(topic, qos=1)
            log.info("MQTT connected to %s:%s, subscribed to %s", host, port, topic)
        else:
            log.error("MQTT connection refused: %s", reason_code)

    def on_message(client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("dropping unparseable MQTT message on %s: %s", message.topic, exc)
            return
        agent.handle_payload(payload)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="vetra-edge-agent")
    client.on_connect = on_connect
    client.on_message = on_message
    # QoS 1 plus a persistent session: readings queue at the broker rather than
    # being lost if the gateway restarts.
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    try:
        client.connect(host, port, keepalive=60)
    except OSError as exc:
        log.warning("no MQTT broker at %s:%s (%s); REST ingestion still active", host, port, exc)
        return None

    client.loop_start()
    return client


def main() -> None:
    parser = argparse.ArgumentParser(description="Vetra edge gateway")
    parser.add_argument("--db", type=Path, default=ROOT / "edge" / "vetra_edge.db")
    parser.add_argument("--http-port", type=int, default=5001)
    parser.add_argument("--http-host", default="0.0.0.0")
    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--no-mqtt", action="store_true")
    parser.add_argument("--cloud-url", default="http://localhost:4000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--sync-interval", type=float, default=30.0)
    parser.add_argument("--retention-days", type=int, default=7,
                        help="days of uploaded raw telemetry to keep on the device")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    agent = EdgeAgent(
        db_path=args.db,
        sync_config=SyncConfig(
            base_url=args.cloud_url,
            api_key=args.api_key,
            interval_seconds=args.sync_interval,
        ),
        enable_sync=not args.no_sync,
        retention_days=args.retention_days,
    )
    agent.start()

    mqtt_client = None
    if not args.no_mqtt:
        mqtt_client = start_mqtt(agent, args.mqtt_host, args.mqtt_port)

    try:
        server = ThreadingHTTPServer((args.http_host, args.http_port), make_http_handler(agent))
    except OSError as exc:
        # Almost always a gateway already running. Say so, rather than dumping a
        # socket traceback on someone setting the system up for the first time.
        agent.stop()
        raise SystemExit(
            f"cannot bind {args.http_host}:{args.http_port} ({exc}). "
            f"Another gateway is probably running - stop it, or pass a different --http-port."
        ) from exc
    server.daemon_threads = True
    log.info("REST ingestion on http://%s:%d/ingest", args.http_host, args.http_port)
    log.info("status at http://%s:%d/status", args.http_host, args.http_port)

    stopping = threading.Event()

    def shutdown(signum, frame):
        if stopping.is_set():
            return
        stopping.set()
        log.info("shutting down ...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    finally:
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        agent.stop()
        log.info("final status: %s", json.dumps(agent.status()["counters"]))


if __name__ == "__main__":
    main()
