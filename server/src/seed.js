/**
 * Seed the cloud database with a small demonstration herd.
 *
 * The real path into this database is the edge gateway syncing records it
 * diagnosed itself. This script exists for the case where only the cloud
 * service is at hand: a dashboard walkthrough on a laptop without Python, or
 * a UI change that needs data behind it. It writes records in exactly the
 * shape the gateway sends, so the dashboard cannot tell the difference.
 *
 *   npm run seed                                   # into the default database
 *   VETRA_CLOUD_DB=/data/vetra_cloud.db npm run seed
 *
 * Re-running is safe: readings are keyed on animal and timestamp, and the demo
 * alerts are reopened rather than duplicated.
 */
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { HealthRecordRepository } from './db.js';
import { DEMO_FARMERS } from './lib/demo-farmers.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.VETRA_CLOUD_DB ?? join(HERE, '..', 'vetra_cloud.db');

// Mirrors ml/vetra_ml/schema.py. The order is the model's class order.
const CONDITIONS = [
  'healthy', 'cardiac_disorder', 'respiratory_disease', 'obesity',
  'diabetes', 'heat_stress', 'infectious_disease', 'mobility_disorder',
];
const LABELS = {
  healthy: 'Healthy',
  cardiac_disorder: 'Cardiac disorder',
  respiratory_disease: 'Respiratory disease',
  obesity: 'Obesity / over-conditioning',
  diabetes: 'Diabetes / metabolic disorder',
  heat_stress: 'Heat stress',
  infectious_disease: 'Infectious disease',
  mobility_disorder: 'Mobility disorder / lameness',
};
const URGENCY = {
  healthy: 'none', cardiac_disorder: 'critical', respiratory_disease: 'high',
  obesity: 'low', diabetes: 'medium', heat_stress: 'high',
  infectious_disease: 'critical', mobility_disorder: 'medium',
};
const SEVERITY_RANK = { none: 0, low: 1, medium: 2, high: 3, critical: 4 };
const RULE_TO_ALERT = { info: 'low', warning: 'medium', critical: 'critical' };
const ROUTING = {
  low: ['farmer_digest'],
  medium: ['farmer'],
  high: ['farmer', 'veterinarian'],
  critical: ['farmer', 'veterinarian', 'emergency'],
};
// Which findings explain which diagnosis, as in edge/alerts.py.
const RELEVANT_FLAGS = {
  cardiac_disorder: ['RESTING_TACHYCARDIA', 'HYPOXAEMIA', 'SEVERE_HYPOXAEMIA'],
  respiratory_disease: ['HYPOXAEMIA', 'SEVERE_HYPOXAEMIA', 'TACHYPNOEA', 'SEVERE_TACHYPNOEA', 'FEVER'],
  heat_stress: ['HEAT_ADVISORY', 'HEAT_LOAD', 'SEVERE_HEAT_LOAD', 'TACHYPNOEA', 'SEVERE_TACHYPNOEA', 'FEVER'],
  infectious_disease: ['FEVER', 'HIGH_FEVER', 'HYPOTHERMIA'],
  mobility_disorder: ['IRREGULAR_GAIT'],
};

// Species reference ranges, mirrored from ml/vetra_ml/synth/species.py.
const SPECIES = {
  cattle:  { heart_rate: [48, 84],  body_temperature: [38.0, 39.3], respiratory_rate: [26, 50], rumination: 0.36, activity_peak: 58, stride: 15 },
  buffalo: { heart_rate: [40, 60],  body_temperature: [37.2, 38.6], respiratory_rate: [12, 36], rumination: 0.38, activity_peak: 48, stride: 12 },
  goat:    { heart_rate: [70, 110], body_temperature: [38.6, 40.0], respiratory_rate: [15, 30], rumination: 0.30, activity_peak: 70, stride: 19 },
  sheep:   { heart_rate: [70, 90],  body_temperature: [38.3, 39.9], respiratory_rate: [16, 34], rumination: 0.32, activity_peak: 64, stride: 16 },
};

// Shift per channel at full severity, the magnitudes of synth/conditions.py.
const SIGNATURES = {
  healthy: {},
  cardiac_disorder: { heart_rate: 28, respiratory_rate: 8, spo2: -3, activity_index: -18, rumination_min: -0.05, distance: 0.65 },
  respiratory_disease: { respiratory_rate: 22, spo2: -7, body_temperature: 0.8, heart_rate: 12, activity_index: -20, rumination_min: -0.12, distance: 0.6 },
  heat_stress: { respiratory_rate: 30, body_temperature: 1.2, heart_rate: 16, rumination_min: -0.18, activity_index: -15, spo2: -1.5, distance: 0.55 },
  infectious_disease: { body_temperature: 1.8, heart_rate: 22, respiratory_rate: 12, activity_index: -30, rumination_min: -0.2, spo2: -2.5, step_regularity: -0.04, distance: 0.4 },
  mobility_disorder: { step_regularity: -0.35, activity_index: -25, heart_rate: 6, respiratory_rate: 3, body_temperature: 0.15, distance: 0.35 },
};

/** The demonstration herd. Sick animals fall ill half-way through the history. */
export const HERD = [
  { animal_id: 'CA-0001', species: 'cattle',  name: 'Ganga',   owner: 'ramesh', condition: 'healthy' },
  { animal_id: 'CA-0002', species: 'cattle',  name: 'Lakshmi', owner: 'ramesh', condition: 'infectious_disease' },
  { animal_id: 'BU-0003', species: 'buffalo', name: 'Kali',    owner: 'sunita', condition: 'healthy' },
  { animal_id: 'BU-0004', species: 'buffalo', name: null,      owner: 'sunita', condition: 'respiratory_disease' },
  { animal_id: 'GO-0005', species: 'goat',    name: 'Moti',    owner: 'imran',  condition: 'heat_stress' },
  { animal_id: 'GO-0006', species: 'goat',    name: null,      owner: 'imran',  condition: 'healthy' },
  { animal_id: 'SH-0007', species: 'sheep',   name: 'Bholi',   owner: 'devi',   condition: 'mobility_disorder' },
  { animal_id: 'SH-0008', species: 'sheep',   name: null,      owner: 'devi',   condition: 'healthy' },
];

const HOURS = 12;              // of minute-resolution history per animal
const MINUTES = HOURS * 60;
const WINDOW = 60;             // the engine diagnoses on a one-hour window ...
const STRIDE = 30;             // ... every thirty minutes

// --- deterministic randomness, so the seed is the same every time ------------
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const gaussian = (rng) => Math.sqrt(-2 * Math.log(1 - rng())) * Math.cos(2 * Math.PI * rng());
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const mid = ([lo, hi]) => (lo + hi) / 2;
const round = (v, digits) => Number(Number(v).toFixed(digits));
const mean = (rows, key) => rows.reduce((sum, r) => sum + r[key], 0) / rows.length;

/** Grazing rhythm in [0, 1]: peaks after dawn and before dusk, quiet at night. */
function circadian(hour) {
  const bump = (centre, width) => Math.exp(-0.5 * ((hour - centre) / width) ** 2);
  return clamp(bump(7.5, 1.8) + bump(17, 2) + 0.35 * bump(12.5, 2.5), 0, 1);
}

/** Disease severity at minute `i`: zero until half-way, then a three-hour ramp. */
function severityAt(animal, i) {
  if (animal.condition === 'healthy') return 0;
  return clamp((i - MINUTES / 2) / 180, 0, 1) * 0.9;
}

function telemetryFor(animal, now, rng) {
  const ref = SPECIES[animal.species];
  const sig = SIGNATURES[animal.condition];
  const base = {
    heart_rate: mid(ref.heart_rate) + gaussian(rng) * 3,
    body_temperature: mid(ref.body_temperature) + gaussian(rng) * 0.15,
    respiratory_rate: mid(ref.respiratory_rate) + gaussian(rng) * 2,
    spo2: 97 + gaussian(rng) * 0.4,
  };

  const rows = [];
  for (let i = 0; i < MINUTES; i += 1) {
    const at = new Date(now.getTime() - (MINUTES - 1 - i) * 60_000);
    const hour = at.getUTCHours() + at.getUTCMinutes() / 60;
    const severity = severityAt(animal, i);
    const shift = (channel) => (sig[channel] ?? 0) * severity;

    const diurnal = -Math.cos(((hour - 5) / 24) * 2 * Math.PI);
    const heatShift = animal.condition === 'heat_stress' ? severity : 0;
    const ambient = 30 + 7 * diurnal + 6 * heatShift + gaussian(rng) * 0.4;
    const humidity = clamp(62 - 18 * diurnal + 12 * heatShift + gaussian(rng) * 2.5, 5, 99);
    const thi = 0.8 * ambient + (humidity / 100) * (ambient - 14.4) + 46.4;
    const heat = clamp((thi - 72) / 12, 0, 1.5);

    const drive = circadian(hour) * (1 - 0.25 * Math.min(heat, 1));
    const activity = clamp(ref.activity_peak * drive + gaussian(rng) * 6 + shift('activity_index'), 0, 100);
    const effort = activity / 100;
    const distanceFactor = 1 - (1 - (sig.distance ?? 1)) * severity;

    rows.push({
      animal_id: animal.animal_id,
      species: animal.species,
      timestamp: at.toISOString(),
      heart_rate: round(clamp(base.heart_rate * (1 + 0.55 * effort) + 4 * heat + gaussian(rng) * 3 + shift('heart_rate'), 10, 250), 2),
      body_temperature: round(clamp(
        base.body_temperature + 0.35 * Math.sin((hour / 24) * 2 * Math.PI - Math.PI / 2)
          + 0.3 * effort + 0.45 * heat + gaussian(rng) * 0.1 + shift('body_temperature'), 30, 45), 3),
      respiratory_rate: round(clamp(base.respiratory_rate * (1 + 0.45 * effort) + 9 * heat + gaussian(rng) * 2.2 + shift('respiratory_rate'), 4, 120), 2),
      spo2: round(clamp(base.spo2 - 0.8 * effort + gaussian(rng) * 0.5 + shift('spo2'), 50, 100), 2),
      activity_index: round(activity, 2),
      rumination_min: round(clamp(ref.rumination * (1 - 0.85 * drive) * 2 + gaussian(rng) * 0.1 + shift('rumination_min'), 0, 1), 4),
      ambient_temp_c: round(ambient, 2),
      ambient_humidity: round(humidity, 2),
      latitude: round(26.8467 + gaussian(rng) * 0.0012, 6),
      longitude: round(80.9462 + gaussian(rng) * 0.0012, 6),
      step_regularity: round(clamp(0.9 - 0.04 * effort + gaussian(rng) * 0.03 + shift('step_regularity'), 0, 1), 4),
      distance_m: round(clamp(ref.stride * effort * distanceFactor + gaussian(rng), 0, 2000), 2),
      battery_pct: round(100 - (i / MINUTES) * 12, 1),
      // Known to the seed, never to a collar: stripped before saving.
      thi: round(thi, 2),
      severity,
    });
  }
  return rows;
}

/** The clinical findings the gateway's rules would raise for this window. */
function findings(species, window) {
  const ref = SPECIES[species];
  const out = [];
  const flag = (code, severity, message, value, threshold) =>
    out.push({ code, severity, message, value: round(value, 2), threshold: round(threshold, 2) });

  const temp = mean(window, 'body_temperature');
  const feverLimit = ref.body_temperature[1] + 1.0;
  if (temp >= feverLimit + 0.7) {
    flag('HIGH_FEVER', 'critical', `Body temperature ${temp.toFixed(1)}C — marked fever`, temp, feverLimit + 0.7);
  } else if (temp >= feverLimit) {
    flag('FEVER', 'warning', `Body temperature ${temp.toFixed(1)}C above normal range`, temp, feverLimit);
  }

  const spo2 = Math.min(...window.map((r) => r.spo2));
  if (spo2 < 88) {
    flag('SEVERE_HYPOXAEMIA', 'critical', `Oxygen saturation fell to ${spo2.toFixed(0)}% — urgent`, spo2, 88);
  } else if (spo2 < 92) {
    flag('HYPOXAEMIA', 'warning', `Oxygen saturation fell to ${spo2.toFixed(0)}%`, spo2, 92);
  }

  const rr = mean(window, 'respiratory_rate');
  const rrLimit = ref.respiratory_rate[1] * 1.5;
  if (rr >= rrLimit * 1.25) {
    flag('SEVERE_TACHYPNOEA', 'critical', `Respiratory rate ${rr.toFixed(0)}/min — severe respiratory distress`, rr, rrLimit * 1.25);
  } else if (rr >= rrLimit) {
    flag('TACHYPNOEA', 'warning', `Respiratory rate ${rr.toFixed(0)}/min above normal`, rr, rrLimit);
  }

  const thi = mean(window, 'thi');
  if (thi >= 88) {
    flag('SEVERE_HEAT_LOAD', 'critical', `Temperature-humidity index ${thi.toFixed(0)} — severe heat load, act now`, thi, 88);
  } else if (thi >= 84) {
    flag('HEAT_LOAD', 'warning', `Temperature-humidity index ${thi.toFixed(0)} — provide shade and water`, thi, 84);
  } else if (thi >= 78) {
    flag('HEAT_ADVISORY', 'info', `Temperature-humidity index ${thi.toFixed(0)} — mild heat load across the herd`, thi, 78);
  }

  const gait = mean(window, 'step_regularity');
  if (gait < 0.68) {
    flag('IRREGULAR_GAIT', 'warning', `Gait rhythm irregular (${gait.toFixed(2)}) — check for lameness`, gait, 0.68);
  }
  return out;
}

const worstSeverity = (flags) => flags.reduce(
  (worst, f) => (SEVERITY_RANK[RULE_TO_ALERT[f.severity]] > SEVERITY_RANK[RULE_TO_ALERT[worst]] ? f.severity : worst),
  'info',
);

/** Class probabilities that sum to one, with `confidence` on the predicted class. */
function probabilities(condition, confidence, rng) {
  const others = CONDITIONS.filter((c) => c !== condition);
  const weights = others.map((c) => (c === 'healthy' ? 3 : 1) * (0.5 + rng()));
  const total = weights.reduce((s, w) => s + w, 0);
  const out = { [condition]: round(confidence, 4) };
  for (const [i, c] of others.entries()) out[c] = round(((1 - confidence) * weights[i]) / total, 4);
  return Object.fromEntries(CONDITIONS.map((c) => [c, out[c]]));
}

function diagnosesFor(animal, rows, rng) {
  const out = [];
  for (let end = WINDOW; end <= rows.length; end += STRIDE) {
    const window = rows.slice(end - WINDOW, end);
    const severity = mean(window, 'severity');
    const sick = severity >= 0.15;
    const condition = sick ? animal.condition : 'healthy';
    const confidence = sick
      ? clamp(0.55 + 0.5 * severity + gaussian(rng) * 0.02, 0.5, 0.99)
      : clamp(0.9 + rng() * 0.09, 0, 0.99);
    const flags = findings(animal.species, window);
    out.push({
      animal_id: animal.animal_id,
      species: animal.species,
      timestamp: window.at(-1).timestamp,
      condition,
      confidence: round(confidence, 4),
      probabilities: probabilities(condition, confidence, rng),
      rule_flags: flags,
      rule_severity: worstSeverity(flags),
      baseline_ready: end > WINDOW * 6,
      model_backend: 'seed',
    });
  }
  return out;
}

/** One open alert per sick animal, built the way edge/alerts.py builds them. */
function alertFor(animal, diagnoses) {
  if (animal.condition === 'healthy') return null;
  const supporting = diagnoses.filter((d) => d.condition === animal.condition && d.confidence >= 0.6);
  if (supporting.length < 2) return null;  // the policy needs two confirming windows

  const latest = supporting.at(-1);
  const rank = Math.max(
    SEVERITY_RANK[URGENCY[animal.condition]],
    SEVERITY_RANK[RULE_TO_ALERT[latest.rule_severity] ?? 'low'],
  );
  const severity = Object.keys(SEVERITY_RANK).find((k) => SEVERITY_RANK[k] === rank);

  const relevant = RELEVANT_FLAGS[animal.condition] ?? [];
  const headline = latest.rule_flags
    .filter((f) => relevant.includes(f.code))
    .sort((a, b) => SEVERITY_RANK[RULE_TO_ALERT[b.severity]] - SEVERITY_RANK[RULE_TO_ALERT[a.severity]])[0];

  const label = LABELS[animal.condition].toLowerCase();
  const pct = Math.round(latest.confidence * 100);
  const message = headline
    ? `${animal.animal_id}: possible ${label} (${pct}% confidence). ${headline.message}`
    : `${animal.animal_id}: possible ${label} (${pct}% confidence).`;

  return {
    alert_id: `seed-${animal.animal_id}-${animal.condition}`,
    animal_id: animal.animal_id,
    species: animal.species,
    condition: animal.condition,
    condition_label: LABELS[animal.condition],
    severity,
    status: 'open',
    opened_at: supporting[1].timestamp,
    updated_at: latest.timestamp,
    resolved_at: null,
    acknowledged_by: null,
    confidence: latest.confidence,
    supporting_windows: Math.min(supporting.length, 4),
    rule_flags: latest.rule_flags,
    notify: ROUTING[severity],
    message,
    latest_diagnosis: latest,
  };
}

/**
 * Write the demonstration herd into `repo`. The history ends at `now`, so the
 * dashboard reads "last reading just now" straight after seeding.
 */
export function seedRepository(repo, { now = new Date(), seed = 7 } = {}) {
  const rng = mulberry32(seed);
  const end = new Date(Math.floor(now.getTime() / 60_000) * 60_000);
  const enrolledAt = new Date(end.getTime() - 30 * 24 * 3_600_000).toISOString();
  const written = { farmers: 0, animals: 0, telemetry: 0, diagnoses: 0, alerts: 0 };

  for (const farmer of DEMO_FARMERS) {
    repo.saveFarmer(farmer);
    written.farmers += 1;
  }
  written.animals += repo.saveAnimals(HERD.map((a) => ({
    animal_id: a.animal_id, species: a.species, name: a.name, owner: a.owner, enrolled_at: enrolledAt,
  })));

  for (const animal of HERD) {
    const rows = telemetryFor(animal, end, rng);
    const diagnoses = diagnosesFor(animal, rows, rng);
    written.telemetry += repo.saveTelemetry(rows.map(({ thi, severity, ...reading }) => reading));
    written.diagnoses += repo.saveDiagnoses(diagnoses);
    const alert = alertFor(animal, diagnoses);
    if (alert) written.alerts += repo.saveAlerts([alert]);
  }
  return written;
}

// Only run when invoked directly, so tests can import seedRepository. URL to
// URL, so a checkout path with a space still counts as "invoked directly".
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const repo = new HealthRecordRepository(DB_PATH);
  try {
    const written = seedRepository(repo);
    console.log(`Seeded ${DB_PATH}`);
    for (const [table, n] of Object.entries(written)) {
      console.log(`  ${table.padEnd(10)} ${n.toLocaleString()} rows written`);
    }
  } finally {
    repo.close();
  }
}
