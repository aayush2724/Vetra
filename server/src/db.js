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
import { randomBytes } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import { NORMAL_RANGE, REPORT_CHANNELS } from './lib/species.js';

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

-- Who an animal belongs to. Kept on the cloud: collars and the gateway know
-- nothing about people, and ownership changes at the farm office, not on the
-- device. animals.owner holds a farmer_id; older rows may hold a bare name.
CREATE TABLE IF NOT EXISTS farmers (
  farmer_id  TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  phone      TEXT,
  village    TEXT,
  notes      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
`;

/** A readable id for a new farmer: the name as a slug, made unique if taken. */
const slugOf = (name) => name.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'farmer';

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
    // When a gateway last delivered a batch. In memory: it is a liveness
    // signal for the dashboard, not part of the health record.
    this.lastSyncAt = null;
  }

  noteSync() { this.lastSyncAt = now(); }

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
          -- Two writers share this row. The gateway owns the clinical state
          -- (severity, message, findings) and may resolve; the operator owns
          -- acknowledgement and may also resolve. A re-send from the gateway
          -- (escalation, or a retry of a batch whose reply was lost) must not
          -- undo what a vet did on the dashboard.
          severity        = excluded.severity,
          message         = excluded.message,
          payload         = excluded.payload,
          status          = CASE
            WHEN excluded.status = 'resolved' OR alerts.status = 'resolved' THEN 'resolved'
            WHEN alerts.status = 'acknowledged' THEN 'acknowledged'
            ELSE excluded.status END,
          resolved_at     = COALESCE(alerts.resolved_at, excluded.resolved_at),
          updated_at      = CASE
            WHEN alerts.status = 'resolved' AND excluded.status != 'resolved' THEN alerts.updated_at
            ELSE excluded.updated_at END`),

      acknowledgeAlert: this.db.prepare(`
        UPDATE alerts SET status = 'acknowledged', acknowledged_by = ?,
                          acknowledged_at = ?, updated_at = ?
        WHERE alert_id = ? AND status = 'open'`),

      resolveAlert: this.db.prepare(`
        UPDATE alerts SET status = 'resolved', resolved_at = ?, updated_at = ?
        WHERE alert_id = ? AND status != 'resolved'`),

      upsertFarmer: this.db.prepare(`
        INSERT INTO farmers (farmer_id, name, phone, village, notes, created_at, updated_at)
        VALUES (@farmer_id, @name, @phone, @village, @notes, @created_at, @updated_at)
        ON CONFLICT(farmer_id) DO UPDATE SET
          name       = excluded.name,
          phone      = COALESCE(excluded.phone, farmers.phone),
          village    = COALESCE(excluded.village, farmers.village),
          notes      = COALESCE(excluded.notes, farmers.notes),
          updated_at = excluded.updated_at`),

      // Each field is written only when the caller sent it, so assigning an
      // owner never blanks a name and vice versa.
      setOwner: this.db.prepare(`
        UPDATE animals SET
          owner      = CASE WHEN @owner_set THEN @owner ELSE owner END,
          name       = CASE WHEN @name_set THEN @name ELSE name END,
          updated_at = @updated_at
        WHERE animal_id = @animal_id`),
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
             COALESCE(f.name, a.owner) AS farmer_name,
             f.phone         AS farmer_phone,
             f.village       AS farmer_village,
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
      LEFT JOIN farmers f ON f.farmer_id = a.owner
      LEFT JOIN diagnoses d ON d.animal_id = a.animal_id
        AND d.timestamp = (SELECT MAX(timestamp) FROM diagnoses WHERE animal_id = a.animal_id)
      ORDER BY a.animal_id`).all();
  }

  getAnimal(animalId) {
    return this.db.prepare(`
      SELECT a.*, COALESCE(f.name, a.owner) AS farmer_name,
             f.phone AS farmer_phone, f.village AS farmer_village
      FROM animals a LEFT JOIN farmers f ON f.farmer_id = a.owner
      WHERE a.animal_id = ?`).get(animalId) ?? null;
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
    if (status) {
      if (status === 'active') {
        where.push("al.status IN ('open', 'acknowledged')");
      } else {
        where.push('al.status = ?');
        params.push(status);
      }
    }
    if (severity) { where.push('al.severity = ?'); params.push(severity); }
    const clause = where.length ? `WHERE ${where.join(' AND ')}` : '';
    return this.db.prepare(`
      SELECT al.alert_id, al.animal_id, al.condition, al.condition_label, al.severity, al.status,
             al.message, al.opened_at, al.updated_at, al.resolved_at, al.acknowledged_by, al.payload,
             an.name AS animal_name, an.owner AS owner,
             COALESCE(f.name, an.owner) AS farmer_name, f.phone AS farmer_phone
      FROM alerts al
      LEFT JOIN animals an ON an.animal_id = al.animal_id
      LEFT JOIN farmers f ON f.farmer_id = an.owner
      ${clause}
      ORDER BY CASE al.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                WHEN 'medium' THEN 2 ELSE 1 END DESC, al.opened_at DESC
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

  // --- farmers ----------------------------------------------------------
  listFarmers() {
    // Each farmer with how many animals they own and how many are under an
    // active alert, so the dashboard can say who needs a call first.
    return this.db.prepare(`
      SELECT f.*,
             (SELECT COUNT(*) FROM animals a WHERE a.owner = f.farmer_id) AS animals,
             (SELECT COUNT(DISTINCT al.animal_id) FROM alerts al
                JOIN animals a ON a.animal_id = al.animal_id
               WHERE a.owner = f.farmer_id AND al.status IN ('open', 'acknowledged')) AS animals_under_alert,
             (SELECT al.severity FROM alerts al
                JOIN animals a ON a.animal_id = al.animal_id
               WHERE a.owner = f.farmer_id AND al.status IN ('open', 'acknowledged')
               ORDER BY CASE al.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                         WHEN 'medium' THEN 2 ELSE 1 END DESC
               LIMIT 1) AS worst_severity
      FROM farmers f
      ORDER BY f.name`).all();
  }

  getFarmer(farmerId) {
    return this.db.prepare('SELECT * FROM farmers WHERE farmer_id = ?').get(farmerId) ?? null;
  }

  saveFarmer({ farmer_id, name, phone = null, village = null, notes = null }) {
    const stamp = now();
    let id = farmer_id;
    if (!id) {
      id = slugOf(name);
      if (this.getFarmer(id)) id = `${id}-${randomBytes(2).toString('hex')}`;
    }
    this.stmt.upsertFarmer.run({
      farmer_id: id, name, phone, village, notes, created_at: stamp, updated_at: stamp,
    });
    return this.getFarmer(id);
  }

  /** Set an animal's owner and/or display name. Fields left undefined are untouched. */
  assignAnimal(animalId, { owner, name } = {}) {
    return this.stmt.setOwner.run({
      animal_id: animalId,
      owner_set: owner === undefined ? 0 : 1,
      owner: owner ?? null,
      name_set: name === undefined ? 0 : 1,
      name: name ?? null,
      updated_at: now(),
    }).changes > 0;
  }

  // --- report -----------------------------------------------------------
  /**
   * Everything known about one animal, assembled for a printable record:
   * identity and owner, the span of readings held, each vital's mean, range
   * and share of time outside the species band, the diagnosis history as runs
   * of one condition, and every alert ever raised, resolved ones included.
   */
  animalReport(animalId) {
    const animal = this.getAnimal(animalId);
    if (!animal) return null;
    const ranges = NORMAL_RANGE[animal.species] ?? {};

    const span = this.db.prepare(`
      SELECT MIN(timestamp) AS first_reading, MAX(timestamp) AS last_reading, COUNT(*) AS readings
      FROM telemetry WHERE animal_id = ?`).get(animalId);
    const first = span.first_reading ? new Date(span.first_reading).getTime() : null;
    const last = span.last_reading ? new Date(span.last_reading).getTime() : null;
    const days = first != null && last != null ? (last - first) / 86_400_000 : 0;

    // Column names come from REPORT_CHANNELS, never from the request.
    const vitals = REPORT_CHANNELS.map(({ key, label, unit, digits }) => {
      const range = ranges[key] ?? null;
      const outside = range
        ? `AVG(CASE WHEN ${key} < ? OR ${key} > ? THEN 1.0 ELSE 0.0 END)`
        : 'NULL';
      const row = this.db.prepare(`
        SELECT AVG(${key}) AS mean, MIN(${key}) AS min, MAX(${key}) AS max, ${outside} AS outside
        FROM telemetry WHERE animal_id = ? AND ${key} IS NOT NULL`)
        .get(...(range ? [range[0], range[1]] : []), animalId);
      return {
        channel: key, label, unit, digits, normal: range,
        mean: row.mean, min: row.min, max: row.max, outside_normal: row.outside,
      };
    });

    const diagnoses = this.db.prepare(`
      SELECT timestamp, condition, confidence, rule_severity FROM diagnoses
      WHERE animal_id = ? ORDER BY timestamp`).all(animalId);
    const history = [];
    const counts = new Map();
    for (const d of diagnoses) {
      counts.set(d.condition, (counts.get(d.condition) ?? 0) + 1);
      const run = history.at(-1);
      if (run && run.condition === d.condition) {
        run.to = d.timestamp;
        run.windows += 1;
        run.peak_confidence = Math.max(run.peak_confidence, d.confidence);
      } else {
        history.push({
          condition: d.condition, from: d.timestamp, to: d.timestamp,
          windows: 1, peak_confidence: d.confidence,
        });
      }
    }

    const alerts = this.db.prepare(`
      SELECT alert_id, condition, condition_label, severity, status, message, opened_at,
             updated_at, resolved_at, acknowledged_by, acknowledged_at, payload
      FROM alerts WHERE animal_id = ? ORDER BY opened_at DESC`).all(animalId)
      .map(({ payload, ...row }) => {
        const record = JSON.parse(payload);
        return {
          ...row,
          confidence: record.confidence ?? null,
          rule_flags: record.rule_flags ?? [],
          notify: record.notify ?? [],
        };
      });
    const tally = (status) => alerts.filter((a) => a.status === status).length;

    return {
      generated_at: now(),
      animal,
      period: { ...span, days },
      vitals,
      latest_diagnosis: this.getDiagnoses(animalId, 1)[0] ?? null,
      condition_history: history.reverse(),
      diagnosis_counts: [...counts.entries()]
        .map(([condition, n]) => ({ condition, n })).sort((a, b) => b.n - a.n),
      alerts,
      alert_counts: {
        total: alerts.length, open: tally('open'),
        acknowledged: tally('acknowledged'), resolved: tally('resolved'),
      },
    };
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

    // After a restart nothing has synced yet; fall back to the newest record
    // received so the dashboard still shows when the gateway was last heard.
    const lastSync = this.lastSyncAt ?? this.db.prepare(`
      SELECT MAX(at) AS at FROM (
        SELECT MAX(received_at) AS at FROM diagnoses
        UNION ALL SELECT MAX(received_at) FROM alerts
        UNION ALL SELECT MAX(received_at) FROM telemetry)`).get()?.at ?? null;

    return { ...counts, by_condition: byCondition, last_sync_at: lastSync };
  }

  close() { this.db.close(); }
}
