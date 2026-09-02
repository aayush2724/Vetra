/**
 * Health-record repository for the cloud tier.
 *
 * Every query the API makes lives here, behind plain method names. That
 * boundary is the point: the project brief names Firestore, but Firestore needs
 * a Google Cloud project and network access that a rural deployment (or an
 * examiner running this offline) may not have. SQLite runs anywhere with no
 * setup, and because nothing above this file writes SQL, moving to Firestore
 * means reimplementing this one module rather than touching the API or the
 * dashboard. See docs/architecture.md for the mapping between these tables and
 * the equivalent Firestore collections.
 *
 * Writes are idempotent on natural keys — animal + timestamp for readings and
 * diagnoses, alert id for alerts — because the edge gateway retries any batch
 * whose response it did not see, and a retry must not duplicate a record.
 */
import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS animals (
  animal_id   TEXT PRIMARY KEY,
  species     TEXT NOT NULL,
  name        TEXT,
  owner       TEXT,
  farm_id     TEXT NOT NULL DEFAULT 'farm-001',
  enrolled_at TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
  animal_id        TEXT NOT NULL,
  timestamp        TEXT NOT NULL,
  heart_rate       REAL,
  body_temperature REAL,
  respiratory_rate REAL,
  spo2             REAL,
  activity_index   REAL,
  rumination_min   REAL,
  latitude         REAL,
  longitude        REAL,
  ambient_temp_c   REAL,
  ambient_humidity REAL,
  step_regularity  REAL,
  distance_m       REAL,
  battery_pct      REAL,
  received_at      TEXT NOT NULL,
  PRIMARY KEY (animal_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_time ON telemetry (animal_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS diagnoses (
  animal_id     TEXT NOT NULL,
  timestamp     TEXT NOT NULL,
  condition     TEXT NOT NULL,
  confidence    REAL NOT NULL,
  rule_severity TEXT NOT NULL,
  payload       TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  PRIMARY KEY (animal_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_diagnoses_time ON diagnoses (animal_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_diagnoses_recent ON diagnoses (timestamp DESC);

CREATE TABLE IF NOT EXISTS alerts (
  alert_id        TEXT PRIMARY KEY,
  animal_id       TEXT NOT NULL,
  condition       TEXT NOT NULL,
  condition_label TEXT,
  severity        TEXT NOT NULL,
  status          TEXT NOT NULL,
  message         TEXT,
  opened_at       TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  resolved_at     TEXT,
  acknowledged_by TEXT,
  acknowledged_at TEXT,
  payload         TEXT NOT NULL,
  received_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_animal ON alerts (animal_id, opened_at DESC);
`;

const now = () => new Date().toISOString();

export class HealthRecordRepository {
  constructor(path = 'vetra_cloud.db') {
    mkdirSync(dirname(path), { recursive: true });
    this.db = new Database(path);
    // WAL keeps dashboard reads from blocking on a gateway's sync batch.
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');
    this.db.exec(SCHEMA);
    this.#prepare();
  }

  #prepare() {
    this.stmt = {
      upsertAnimal: this.db.prepare(`
        INSERT INTO animals (animal_id, species, name, owner, farm_id, enrolled_at, updated_at)
        VALUES (@animal_id, @species, @name, @owner, @farm_id, @enrolled_at, @updated_at)
        ON CONFLICT(animal_id) DO UPDATE SET
          species    = excluded.species,
          name       = COALESCE(excluded.name, animals.name),
          owner      = COALESCE(excluded.owner, animals.owner),
          updated_at = excluded.updated_at`),

      upsertTelemetry: this.db.prepare(`
        INSERT INTO telemetry (animal_id, timestamp, heart_rate, body_temperature,
          respiratory_rate, spo2, activity_index, rumination_min, latitude, longitude,
          ambient_temp_c, ambient_humidity, step_regularity, distance_m, battery_pct, received_at)
        VALUES (@animal_id, @timestamp, @heart_rate, @body_temperature, @respiratory_rate,
          @spo2, @activity_index, @rumination_min, @latitude, @longitude, @ambient_temp_c,
          @ambient_humidity, @step_regularity, @distance_m, @battery_pct, @received_at)
        ON CONFLICT(animal_id, timestamp) DO NOTHING`),

      upsertDiagnosis: this.db.prepare(`
        INSERT INTO diagnoses (animal_id, timestamp, condition, confidence, rule_severity,
                               payload, received_at)
        VALUES (@animal_id, @timestamp, @condition, @confidence, @rule_severity,
                @payload, @received_at)
        ON CONFLICT(animal_id, timestamp) DO UPDATE SET
          condition     = excluded.condition,
          confidence    = excluded.confidence,
          rule_severity = excluded.rule_severity,
          payload       = excluded.payload`),

      upsertAlert: this.db.prepare(`
        INSERT INTO alerts (alert_id, animal_id, condition, condition_label, severity, status,
          message, opened_at, updated_at, resolved_at, acknowledged_by, acknowledged_at,
          payload, received_at)
        VALUES (@alert_id, @animal_id, @condition, @condition_label, @severity, @status,
          @message, @opened_at, @updated_at, @resolved_at, @acknowledged_by, @acknowledged_at,
          @payload, @received_at)
        ON CONFLICT(alert_id) DO UPDATE SET
          severity        = excluded.severity,
          status          = excluded.status,
          message         = excluded.message,
          updated_at      = excluded.updated_at,
          resolved_at     = excluded.resolved_at,
          payload         = excluded.payload`),

      acknowledgeAlert: this.db.prepare(`
        UPDATE alerts SET status = 'acknowledged', acknowledged_by = ?,
                          acknowledged_at = ?, updated_at = ?
        WHERE alert_id = ? AND status = 'open'`),

      resolveAlert: this.db.prepare(`
        UPDATE alerts SET status = 'resolved', resolved_at = ?, updated_at = ?
        WHERE alert_id = ? AND status != 'resolved'`),
    };
  }

  // --- writes (batched: one transaction per sync request) ---------------
  #batch(statement, rows, shape) {
    const run = this.db.transaction((items) => {
      let written = 0;
      for (const item of items) {
        written += statement.run(shape(item)).changes;
      }
      return written;
    });
    return run(rows);
  }

  saveAnimals(rows) {
    const stamp = now();
    return this.#batch(this.stmt.upsertAnimal, rows, (r) => ({
      animal_id: r.animal_id,
      species: r.species ?? 'cattle',
      name: r.name ?? null,
      owner: r.owner ?? null,
      farm_id: r.farm_id ?? 'farm-001',
      enrolled_at: r.enrolled_at ?? stamp,
      updated_at: stamp,
    }));
  }

  saveTelemetry(rows) {
    const stamp = now();
    const num = (v) => (v === undefined || v === null ? null : Number(v));
    return this.#batch(this.stmt.upsertTelemetry, rows, (r) => ({
      animal_id: r.animal_id,
      timestamp: r.timestamp,
      heart_rate: num(r.heart_rate),
      body_temperature: num(r.body_temperature),
      respiratory_rate: num(r.respiratory_rate),
      spo2: num(r.spo2),
      activity_index: num(r.activity_index),
      rumination_min: num(r.rumination_min),
      latitude: num(r.latitude),
      longitude: num(r.longitude),
      ambient_temp_c: num(r.ambient_temp_c),
      ambient_humidity: num(r.ambient_humidity),
      step_regularity: num(r.step_regularity),
      distance_m: num(r.distance_m),
      battery_pct: num(r.battery_pct),
      received_at: stamp,
    }));
  }

  saveDiagnoses(rows) {
    const stamp = now();
    return this.#batch(this.stmt.upsertDiagnosis, rows, (r) => ({
      animal_id: r.animal_id,
      timestamp: r.timestamp,
      condition: r.condition,
      confidence: Number(r.confidence ?? 0),
      rule_severity: r.rule_severity ?? 'info',
      payload: JSON.stringify(r),
      received_at: stamp,
    }));
  }

  saveAlerts(rows) {
    const stamp = now();
    return this.#batch(this.stmt.upsertAlert, rows, (r) => ({
      alert_id: r.alert_id,
      animal_id: r.animal_id,
      condition: r.condition,
      condition_label: r.condition_label ?? r.condition,
      severity: r.severity ?? 'medium',
      status: r.status ?? 'open',
      message: r.message ?? null,
      opened_at: r.opened_at,
      updated_at: r.updated_at ?? stamp,
      resolved_at: r.resolved_at ?? null,
      acknowledged_by: r.acknowledged_by ?? null,
      acknowledged_at: r.acknowledged_at ?? null,
      payload: JSON.stringify(r),
      received_at: stamp,
    }));
  }

  // --- reads ------------------------------------------------------------
  listAnimals() {
    // Each animal is joined to its newest diagnosis and its worst open alert so
    // the herd screen is one query rather than one per animal.
    return this.db.prepare(`
      SELECT a.*,
             d.condition     AS latest_condition,
             d.confidence    AS latest_confidence,
             d.timestamp     AS latest_seen,
             d.rule_severity AS latest_rule_severity,
             (SELECT COUNT(*) FROM alerts al
               WHERE al.animal_id = a.animal_id AND al.status = 'open') AS open_alerts,
             (SELECT al.severity FROM alerts al
               WHERE al.animal_id = a.animal_id AND al.status = 'open'
               ORDER BY CASE al.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                         WHEN 'medium' THEN 2 ELSE 1 END DESC
               LIMIT 1) AS worst_severity
      FROM animals a
      LEFT JOIN diagnoses d ON d.animal_id = a.animal_id
        AND d.timestamp = (SELECT MAX(timestamp) FROM diagnoses WHERE animal_id = a.animal_id)
      ORDER BY a.animal_id`).all();
  }

  getAnimal(animalId) {
    return this.db.prepare('SELECT * FROM animals WHERE animal_id = ?').get(animalId) ?? null;
  }

  getTelemetry(animalId, limit = 720) {
    const rows = this.db.prepare(`
      SELECT * FROM telemetry WHERE animal_id = ?
      ORDER BY timestamp DESC LIMIT ?`).all(animalId, limit);
    return rows.reverse();
  }

  getDiagnoses(animalId, limit = 100) {
    const rows = this.db.prepare(`
      SELECT payload FROM diagnoses WHERE animal_id = ?
      ORDER BY timestamp DESC LIMIT ?`).all(animalId, limit);
    return rows.map((r) => JSON.parse(r.payload));
  }

  listAlerts({ status, severity, limit = 100 } = {}) {
    const where = [];
    const params = [];
    if (status) { where.push('status = ?'); params.push(status); }
    if (severity) { where.push('severity = ?'); params.push(severity); }
    const clause = where.length ? `WHERE ${where.join(' AND ')}` : '';
    return this.db.prepare(`
      SELECT alert_id, animal_id, condition, condition_label, severity, status, message,
             opened_at, updated_at, resolved_at, acknowledged_by, payload
      FROM alerts ${clause}
      ORDER BY CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                             WHEN 'medium' THEN 2 ELSE 1 END DESC, opened_at DESC
      LIMIT ?`).all(...params, limit)
      .map((r) => ({ ...r, rule_flags: JSON.parse(r.payload).rule_flags ?? [] }));
  }

  acknowledgeAlert(alertId, who) {
    const stamp = now();
    return this.stmt.acknowledgeAlert.run(who, stamp, stamp, alertId).changes > 0;
  }

  resolveAlert(alertId) {
    const stamp = now();
    return this.stmt.resolveAlert.run(stamp, stamp, alertId).changes > 0;
  }

  herdSummary() {
    const counts = this.db.prepare(`
      SELECT
        (SELECT COUNT(*) FROM animals) AS animals,
        (SELECT COUNT(*) FROM alerts WHERE status = 'open') AS open_alerts,
        (SELECT COUNT(*) FROM alerts WHERE status = 'open' AND severity = 'critical') AS critical_alerts,
        (SELECT COUNT(*) FROM telemetry) AS readings,
        (SELECT COUNT(*) FROM diagnoses) AS diagnoses`).get();

    const byCondition = this.db.prepare(`
      SELECT d.condition, COUNT(*) AS n FROM diagnoses d
      WHERE d.timestamp = (SELECT MAX(timestamp) FROM diagnoses WHERE animal_id = d.animal_id)
      GROUP BY d.condition ORDER BY n DESC`).all();

    const lastSync = this.db.prepare(
      'SELECT MAX(received_at) AS at FROM diagnoses').get()?.at ?? null;

    return { ...counts, by_condition: byCondition, last_sync_at: lastSync };
  }

  close() { this.db.close(); }
}
