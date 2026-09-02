"""Local health-record store for the edge device.

This is the system's durability guarantee. Rural connectivity is intermittent
by assumption, so every reading, diagnosis and alert lands in SQLite first and
is uploaded later; nothing is ever held only in memory waiting for a network
that may not come back.

Sync state lives as a nullable `synced_at` column on each table rather than in
a separate outbox. Re-uploading a row is then idempotent, an interrupted sync
resumes by simply asking for unsynced rows again, and there is no second
structure that can drift out of step with the data.

Only the standard library is used: `paho-mqtt` and friends are optional extras,
but a device must always be able to record what it saw.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_DB = Path(__file__).resolve().parent / "vetra_edge.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS animals (
    animal_id   TEXT PRIMARY KEY,
    species     TEXT NOT NULL,
    name        TEXT,
    owner       TEXT,
    enrolled_at TEXT NOT NULL,
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id   TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    synced_at   TEXT,
    UNIQUE (animal_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_unsynced ON telemetry (synced_at) WHERE synced_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_telemetry_animal_time ON telemetry (animal_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS diagnoses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id     TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    condition     TEXT NOT NULL,
    confidence    REAL NOT NULL,
    rule_severity TEXT NOT NULL,
    payload       TEXT NOT NULL,
    synced_at     TEXT,
    UNIQUE (animal_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_diagnoses_unsynced ON diagnoses (synced_at) WHERE synced_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_diagnoses_animal_time ON diagnoses (animal_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id      TEXT PRIMARY KEY,
    animal_id     TEXT NOT NULL,
    condition     TEXT NOT NULL,
    severity      TEXT NOT NULL,
    status        TEXT NOT NULL,          -- open | resolved | acknowledged
    opened_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    resolved_at   TEXT,
    acknowledged_by TEXT,
    payload       TEXT NOT NULL,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_unsynced ON alerts (synced_at) WHERE synced_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts (status, opened_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EdgeStore:
    """Thread-safe SQLite wrapper. Safe to share across the MQTT callback thread."""

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus an explicit lock: the MQTT client calls
        # back on its own network thread while the sync loop runs on another.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

        with self._cursor() as cur:
            # WAL lets the sync thread read while the ingest thread writes.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.executescript(SCHEMA)

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- animals ---------------------------------------------------------
    def register_animal(self, animal_id: str, species: str, name: str | None = None,
                        owner: str | None = None) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO animals (animal_id, species, name, owner, enrolled_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(animal_id) DO UPDATE SET
                       species = excluded.species,
                       name    = COALESCE(excluded.name, animals.name),
                       owner   = COALESCE(excluded.owner, animals.owner)""",
                (animal_id, species, name, owner, _now()),
            )

    def list_animals(self) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM animals ORDER BY animal_id")
            return [dict(r) for r in cur.fetchall()]

    # --- telemetry -------------------------------------------------------
    def record_telemetry(self, animal_id: str, timestamp: str, payload: dict) -> None:
        with self._cursor() as cur:
            # A collar that retransmits after a dropout replays timestamps it
            # already sent; ignoring the duplicate is the correct response.
            cur.execute(
                """INSERT INTO telemetry (animal_id, timestamp, payload) VALUES (?, ?, ?)
                   ON CONFLICT(animal_id, timestamp) DO NOTHING""",
                (animal_id, timestamp, json.dumps(payload, default=str)),
            )

    def recent_telemetry(self, animal_id: str, limit: int = 120) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT payload FROM telemetry WHERE animal_id = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (animal_id, limit),
            )
            rows = [json.loads(r["payload"]) for r in cur.fetchall()]
        return list(reversed(rows))

    # --- diagnoses -------------------------------------------------------
    def record_diagnosis(self, diagnosis: dict[str, Any]) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO diagnoses
                       (animal_id, timestamp, condition, confidence, rule_severity, payload)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(animal_id, timestamp) DO UPDATE SET
                       condition = excluded.condition,
                       confidence = excluded.confidence,
                       rule_severity = excluded.rule_severity,
                       payload = excluded.payload,
                       synced_at = NULL""",
                (
                    diagnosis["animal_id"], diagnosis["timestamp"], diagnosis["condition"],
                    float(diagnosis["confidence"]), diagnosis.get("rule_severity", "info"),
                    json.dumps(diagnosis, default=str),
                ),
            )

    def recent_diagnoses(self, animal_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT payload FROM diagnoses"
        params: tuple = ()
        if animal_id:
            query += " WHERE animal_id = ?"
            params = (animal_id,)
        query += " ORDER BY timestamp DESC LIMIT ?"
        with self._cursor() as cur:
            cur.execute(query, params + (limit,))
            return [json.loads(r["payload"]) for r in cur.fetchall()]

    # --- alerts ----------------------------------------------------------
    def upsert_alert(self, alert: dict[str, Any]) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO alerts (alert_id, animal_id, condition, severity, status,
                                       opened_at, updated_at, resolved_at, acknowledged_by, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(alert_id) DO UPDATE SET
                       severity        = excluded.severity,
                       status          = excluded.status,
                       updated_at      = excluded.updated_at,
                       resolved_at     = excluded.resolved_at,
                       acknowledged_by = excluded.acknowledged_by,
                       payload         = excluded.payload,
                       synced_at       = NULL""",
                (
                    alert["alert_id"], alert["animal_id"], alert["condition"], alert["severity"],
                    alert["status"], alert["opened_at"], alert.get("updated_at", _now()),
                    alert.get("resolved_at"), alert.get("acknowledged_by"),
                    json.dumps(alert, default=str),
                ),
            )

    def open_alert_for(self, animal_id: str, condition: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute(
                """SELECT payload FROM alerts
                   WHERE animal_id = ? AND condition = ? AND status = 'open'
                   ORDER BY opened_at DESC LIMIT 1""",
                (animal_id, condition),
            )
            row = cur.fetchone()
        return json.loads(row["payload"]) if row else None

    def list_alerts(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT payload FROM alerts"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY opened_at DESC LIMIT ?"
        with self._cursor() as cur:
            cur.execute(query, params + (limit,))
            return [json.loads(r["payload"]) for r in cur.fetchall()]

    # --- sync ------------------------------------------------------------
    def unsynced(self, table: str, limit: int = 500) -> list[dict[str, Any]]:
        """Rows still awaiting upload. `table` is validated against a fixed set."""
        key = {"telemetry": "id", "diagnoses": "id", "alerts": "alert_id", "animals": "animal_id"}
        if table not in key:
            raise ValueError(f"unknown table {table!r}")

        if table == "animals":
            columns = "animal_id, species, name, owner, enrolled_at"
        else:
            columns = f"{key[table]} AS row_key, payload"

        with self._cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM {table} WHERE synced_at IS NULL "  # noqa: S608 - table is allow-listed above
                f"ORDER BY rowid LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()

        if table == "animals":
            return [dict(r) for r in rows]
        return [{"row_key": r["row_key"], **json.loads(r["payload"])} for r in rows]

    def mark_synced(self, table: str, keys: Iterable[Any]) -> int:
        key_column = {"telemetry": "id", "diagnoses": "id",
                      "alerts": "alert_id", "animals": "animal_id"}
        if table not in key_column:
            raise ValueError(f"unknown table {table!r}")
        keys = list(keys)
        if not keys:
            return 0

        stamp = _now()
        placeholders = ",".join("?" for _ in keys)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET synced_at = ? "  # noqa: S608 - table is allow-listed above
                f"WHERE {key_column[table]} IN ({placeholders})",
                (stamp, *keys),
            )
            return cur.rowcount

    def pending_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._cursor() as cur:
            for table in ("animals", "telemetry", "diagnoses", "alerts"):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE synced_at IS NULL")  # noqa: S608
                counts[table] = int(cur.fetchone()["n"])
        return counts

    # --- housekeeping ----------------------------------------------------
    def prune_telemetry(self, keep_days: int = 7) -> int:
        """Drop old raw samples that have already been uploaded.

        Diagnoses and alerts are never pruned — they are the health record.
        Raw minute-resolution telemetry is the bulky part and, once it is safely
        in the cloud, the device has no reason to keep carrying it.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM telemetry WHERE timestamp < ? AND synced_at IS NOT NULL",
                (cutoff,),
            )
            removed = cur.rowcount
        return removed

    def stats(self) -> dict[str, Any]:
        with self._cursor() as cur:
            out: dict[str, Any] = {}
            for table in ("animals", "telemetry", "diagnoses", "alerts"):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
                out[table] = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM alerts WHERE status = 'open'")
            out["open_alerts"] = int(cur.fetchone()["n"])
        out["pending_sync"] = self.pending_counts()
        out["db_size_kb"] = round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0
        return out
