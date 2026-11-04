/**
 * Ingestion endpoints for edge gateways.
 *
 * One shape for all four record types: POST { records: [...] }, respond with
 * how many were written. The gateway marks rows synced only on a 2xx, and every
 * write is idempotent on its natural key, so a gateway that retries a batch
 * whose response it never saw is harmless.
 */
import { timingSafeEqual } from 'node:crypto';
import { Router } from 'express';

const MAX_BATCH = 1000;

// Which fields every record of a table must carry, and as what type. A record
// that fails this can never be stored, so the batch is refused with a 400 and
// the gateway drops it from its queue instead of retrying it for ever.
const REQUIRED = {
  animals: { animal_id: 'string' },
  telemetry: { animal_id: 'string', timestamp: 'string' },
  diagnoses: { animal_id: 'string', timestamp: 'string', condition: 'string' },
  alerts: { alert_id: 'string', animal_id: 'string', condition: 'string', opened_at: 'string' },
};

const firstProblem = (name, records) => {
  for (const [index, record] of records.entries()) {
    if (!record || typeof record !== 'object' || Array.isArray(record)) {
      return `record ${index}: must be an object`;
    }
    for (const [field, type] of Object.entries(REQUIRED[name])) {
      if (typeof record[field] !== type || record[field] === '') {
        return `record ${index}: ${field} must be a non-empty ${type}`;
      }
    }
  }
  return null;
};

const sameSecret = (given, expected) => {
  const a = Buffer.from(String(given));
  const b = Buffer.from(String(expected));
  return a.length === b.length && timingSafeEqual(a, b);
};

export function syncRouter(repo, bus, { apiKey = '' } = {}) {
  const router = Router();

  // A shared key is thin authentication, but it does stop an open port on a
  // farm network from accepting anyone's readings. Real deployments should put
  // per-gateway credentials here; see docs/architecture.md.
  router.use((req, res, next) => {
    if (!apiKey) return next();
    const header = req.get('authorization') ?? '';
    const token = header.startsWith('Bearer ') ? header.slice(7) : '';
    if (!sameSecret(token, apiKey)) {
      return res.status(401).json({ error: 'invalid or missing API key' });
    }
    next();
  });

  const handler = (name, save, onWrite) => (req, res) => {
    const records = req.body?.records;
    if (!Array.isArray(records)) {
      return res.status(400).json({ error: '`records` must be an array' });
    }
    if (records.length > MAX_BATCH) {
      return res.status(413).json({ error: `batch too large (max ${MAX_BATCH})` });
    }
    if (records.length === 0) return res.json({ written: 0, received: 0 });

    const problem = firstProblem(name, records);
    if (problem) return res.status(400).json({ error: problem });

    try {
      const written = save(records);
      repo.noteSync?.();
      onWrite?.(records);
      res.json({ written, received: records.length });
    } catch (err) {
      // Returning 500 (not 200) matters: the gateway must keep these rows
      // unsynced and try again rather than dropping them.
      req.log?.error?.(err);
      console.error(`sync/${name} failed:`, err.message);
      res.status(500).json({ error: 'failed to persist batch' });
    }
  };

  router.post('/animals', handler('animals', (r) => repo.saveAnimals(r)));
  router.post('/telemetry', handler('telemetry', (r) => repo.saveTelemetry(r)));

  router.post('/diagnoses', handler('diagnoses', (r) => repo.saveDiagnoses(r), (records) => {
    const latest = records[records.length - 1];
    bus.broadcast('diagnosis', { count: records.length, latest });
  }));

  router.post('/alerts', handler('alerts', (r) => repo.saveAlerts(r), (records) => {
    // Push each alert individually: the dashboard shows them one by one, and a
    // critical alert should not wait behind a batch of low-priority ones.
    for (const alert of records) bus.broadcast('alert', alert);
  }));

  return router;
}
