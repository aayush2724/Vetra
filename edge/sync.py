"""Upload local records to the cloud whenever a connection happens to exist.

Written against the standard library only. A field gateway should not need a
package index to be reachable in order to be installed, and `urllib` is enough
for batched JSON POSTs.

The contract with the store is what makes this safe to interrupt: rows are
marked synced only after the server has acknowledged them, so a connection that
drops mid-batch costs a retry and never a record. The server side is expected
to be idempotent on the natural keys (animal + timestamp, alert id), which lets
a retry after a lost response be harmless rather than a duplicate.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("vetra.sync")

# Tables in dependency order: an alert referring to an unknown animal is
# awkward for the server to file, so animals go up first.
SYNC_ORDER = ("animals", "telemetry", "diagnoses", "alerts")

ENDPOINTS = {
    "animals": "/api/sync/animals",
    "telemetry": "/api/sync/telemetry",
    "diagnoses": "/api/sync/diagnoses",
    "alerts": "/api/sync/alerts",
}


@dataclass(slots=True)
class SyncConfig:
    base_url: str = "http://localhost:4000"
    api_key: str = ""
    batch_size: int = 200
    interval_seconds: float = 30.0
    timeout_seconds: float = 10.0
    max_backoff_seconds: float = 300.0
    # Raw telemetry is bulky and rarely needed once a diagnosis exists; skipping
    # it keeps a metered rural link usable for the records that matter.
    upload_telemetry: bool = True


class CloudSync:
    """Pushes unsynced rows upstream, backing off while the link is down."""

    def __init__(self, store, config: SyncConfig | None = None) -> None:
        self.store = store
        self.config = config or SyncConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = 0.0
        self.online = False
        self.last_error: str | None = None
        self.last_success_at: float | None = None
        self.uploaded_total = 0

    # --- transport -------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.config.base_url.rstrip("/") + path
        body = json.dumps(payload, default=str).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.config.api_key:
            request.add_header("Authorization", f"Bearer {self.config.api_key}")

        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read().decode("utf-8") or "{}"
            return json.loads(raw)

    # --- one pass --------------------------------------------------------
    def sync_once(self) -> dict[str, int]:
        """Push one batch per table. Returns rows accepted, keyed by table."""
        uploaded: dict[str, int] = {}

        for table in SYNC_ORDER:
            if table == "telemetry" and not self.config.upload_telemetry:
                continue

            rows = self.store.unsynced(table, limit=self.config.batch_size)
            if not rows:
                continue

            try:
                self._post(ENDPOINTS[table], {"records": rows})
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                self.online = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("sync failed for %s: %s", table, self.last_error)
                # Stop the pass here rather than hammering a link that is down.
                return uploaded

            keys = [r["row_key"] if "row_key" in r else r["animal_id"] for r in rows]
            self.store.mark_synced(table, keys)
            uploaded[table] = len(rows)
            self.uploaded_total += len(rows)
            log.info("synced %d %s rows", len(rows), table)

        self.online = True
        self.last_error = None
        self.last_success_at = time.time()
        return uploaded

    # --- background loop -------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                uploaded = self.sync_once()
            except Exception:  # a sync bug must never take the gateway down
                log.exception("unexpected error during sync")
                uploaded = {}

            if self.online:
                self._backoff = 0.0
                # Drain a large backlog quickly instead of waiting a full
                # interval between batches once the link is back.
                delay = 1.0 if uploaded else self.config.interval_seconds
            else:
                self._backoff = min(
                    max(self._backoff * 2, self.config.interval_seconds),
                    self.config.max_backoff_seconds,
                )
                delay = self._backoff

            self._stop.wait(delay)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vetra-sync", daemon=True)
        self._thread.start()
        log.info("cloud sync started -> %s", self.config.base_url)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def status(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "base_url": self.config.base_url,
            "uploaded_total": self.uploaded_total,
            "pending": self.store.pending_counts(),
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "backoff_seconds": round(self._backoff, 1),
        }
