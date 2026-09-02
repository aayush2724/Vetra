"""Stand-in for the sensor collars.

Streams generated telemetry at the gateway over MQTT or REST. `--speed` scales
simulated time against wall-clock time, so a five-day herd history can be
replayed through the live pipeline in a couple of minutes for a demonstration,
or at 1x to watch the system behave in real time.

Run:  python edge/simulator.py --transport rest --animals 12 --speed 600
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ml"))

from vetra_ml.synth.generator import build_herd_plans, simulate_animal  # noqa: E402

log = logging.getLogger("vetra.simulator")


def build_stream(n_animals: int, days: int, seed: int, healthy_fraction: float):
    """Generate the herd, then interleave every animal into one time-ordered stream."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    plans = build_herd_plans(n_animals, rng, days, healthy_fraction)

    frames = [simulate_animal(plan, start, days, rng) for plan in plans]
    stream = pd.concat(frames, ignore_index=True).sort_values("timestamp")

    truth = {p.animal_id: p.condition for p in plans}
    return stream, truth


class RestPublisher:
    def __init__(self, url: str, batch_size: int = 1) -> None:
        self.url = url
        self.batch_size = batch_size
        self._buffer: list[dict] = []
        self.sent = 0
        self.failed = 0

    def publish(self, record: dict) -> None:
        self._buffer.append(record)
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        body = json.dumps({"records": self._buffer}, default=str).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
            self.sent += len(self._buffer)
        except (urllib.error.URLError, OSError) as exc:
            self.failed += len(self._buffer)
            log.warning("POST failed (%s); is the gateway running?", exc)
        finally:
            self._buffer.clear()

    def close(self) -> None:
        self.flush()


class MqttPublisher:
    def __init__(self, host: str, port: int, topic_prefix: str = "vetra/telemetry") -> None:
        import paho.mqtt.client as mqtt

        self.topic_prefix = topic_prefix
        self.sent = 0
        self.failed = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="vetra-simulator")
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()
        log.info("MQTT publisher connected to %s:%s", host, port)

    def publish(self, record: dict) -> None:
        topic = f"{self.topic_prefix}/{record['animal_id']}"
        # QoS 1: a lost reading is a gap in an animal's record, worth a retry.
        result = self.client.publish(topic, json.dumps(record, default=str), qos=1)
        if result.rc == 0:
            self.sent += 1
        else:
            self.failed += 1

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate sensor collars streaming telemetry")
    parser.add_argument("--transport", choices=("rest", "mqtt"), default="rest")
    parser.add_argument("--url", default="http://localhost:5001/ingest")
    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--animals", type=int, default=12)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--healthy-fraction", type=float, default=0.4)
    parser.add_argument("--speed", type=float, default=600.0,
                        help="simulated minutes per real second (600 = 10 simulated hours/second)")
    parser.add_argument("--batch-size", type=int, default=32, help="REST records per request")
    parser.add_argument("--limit", type=int, default=0, help="stop after N readings (0 = all)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    log.info("generating %d animals x %d days ...", args.animals, args.days)
    stream, truth = build_stream(args.animals, args.days, args.seed, args.healthy_fraction)

    log.info("ground truth for this run:")
    for animal_id, condition in sorted(truth.items()):
        log.info("    %-10s %s", animal_id, condition)

    publisher = (
        RestPublisher(args.url, args.batch_size)
        if args.transport == "rest"
        else MqttPublisher(args.mqtt_host, args.mqtt_port)
    )

    total = len(stream) if not args.limit else min(args.limit, len(stream))
    log.info("streaming %d readings at %.0fx ...", total, args.speed)

    # `severity` and `label` are ground truth the simulator knows but a real
    # collar would not, so they are stripped before transmission.
    drop = {"severity", "label"}
    delay_per_reading = 60.0 / max(args.speed, 1e-6) / max(args.animals, 1)

    t0 = time.perf_counter()
    try:
        for i, row in enumerate(stream.to_dict("records")):
            if args.limit and i >= args.limit:
                break
            record = {k: v for k, v in row.items() if k not in drop}
            record["timestamp"] = str(record["timestamp"])
            publisher.publish(record)

            if delay_per_reading > 0.0005:
                time.sleep(delay_per_reading)
            if (i + 1) % 5000 == 0:
                log.info("  %d/%d sent", i + 1, total)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        publisher.close()

    elapsed = time.perf_counter() - t0
    log.info("done: %d sent, %d failed in %.1fs (%.0f readings/s)",
             publisher.sent, publisher.failed, elapsed, publisher.sent / max(elapsed, 1e-6))


if __name__ == "__main__":
    main()
