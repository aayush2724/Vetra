/**
 * Cloud API tests.
 *
 * Each test gets its own temporary database and an ephemeral port, so they can
 * run in any order and alongside a real server on 4000.
 */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, test } from 'node:test';

import { createApp } from '../src/index.js';

let server; let base; let repo; let bus; let dir;

before(async () => {
  dir = mkdtempSync(join(tmpdir(), 'vetra-test-'));
  const created = createApp({ dbPath: join(dir, 'test.db') });
  repo = created.repo;
  bus = created.bus;
  server = created.app.listen(0);
  await new Promise((resolve) => server.once('listening', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => {
  bus.close();
  server.close();
  repo.close();
  rmSync(dir, { recursive: true, force: true });
});

const post = (path, body) =>
  fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

const get = (path) => fetch(`${base}${path}`);

const READING = {
  animal_id: 'CA-0001', species: 'cattle', timestamp: '2026-05-01T10:00:00+00:00',
  heart_rate: 68, body_temperature: 38.6, respiratory_rate: 30, spo2: 97,
  activity_index: 40, rumination_min: 0.5, latitude: 26.85, longitude: 80.94,
  ambient_temp_c: 30, ambient_humidity: 60, step_regularity: 0.9,
  distance_m: 12, battery_pct: 88,
};

const ALERT = {
  alert_id: 'a-1', animal_id: 'CA-0001', condition: 'heat_stress',
  condition_label: 'Heat stress', severity: 'high', status: 'open',
  message: 'CA-0001: possible heat stress', opened_at: '2026-05-01T11:00:00+00:00',
  rule_flags: [{ code: 'HEAT_LOAD', severity: 'warning', message: 'THI 86' }],
};

describe('health and sync', () => {
  test('health endpoint responds', async () => {
    const res = await get('/health');
    assert.equal(res.status, 200);
    assert.equal((await res.json()).status, 'ok');
  });

  test('animals sync accepts a batch', async () => {
    const res = await post('/api/sync/animals', {
      records: [{ animal_id: 'CA-0001', species: 'cattle', name: 'Ganga' }],
    });
    assert.equal(res.status, 200);
    assert.equal((await res.json()).written, 1);
  });

  test('telemetry sync is idempotent on animal + timestamp', async () => {
    const first = await (await post('/api/sync/telemetry', { records: [READING] })).json();
    assert.equal(first.written, 1);

    // A gateway that never saw the first response retries the same batch.
    const retry = await (await post('/api/sync/telemetry', { records: [READING] })).json();
    assert.equal(retry.written, 0, 'a retried batch must not duplicate rows');
    assert.equal(retry.received, 1);
  });

  test('diagnoses sync stores the full payload', async () => {
    const res = await post('/api/sync/diagnoses', {
      records: [{
        animal_id: 'CA-0001', timestamp: '2026-05-01T11:00:00+00:00',
        condition: 'heat_stress', confidence: 0.94, rule_severity: 'warning',
        probabilities: { heat_stress: 0.94, healthy: 0.03 },
      }],
    });
    assert.equal((await res.json()).written, 1);

    const detail = await (await get('/api/animals/CA-0001')).json();
    assert.equal(detail.diagnoses[0].condition, 'heat_stress');
    assert.ok(detail.diagnoses[0].probabilities, 'nested payload survived the round trip');
  });

  test('rejects a body without a records array', async () => {
    assert.equal((await post('/api/sync/telemetry', { rows: [] })).status, 400);
  });

  test('rejects an oversized batch', async () => {
    const records = Array.from({ length: 1001 }, (_, i) => ({
      ...READING, timestamp: `2026-05-02T00:${String(i % 60).padStart(2, '0')}:00+00:00`,
    }));
    assert.equal((await post('/api/sync/telemetry', { records })).status, 413);
  });

  test('an empty batch is accepted as a no-op', async () => {
    const res = await post('/api/sync/telemetry', { records: [] });
    assert.equal(res.status, 200);
    assert.equal((await res.json()).written, 0);
  });
});

describe('read API', () => {
  test('summary reflects stored records', async () => {
    const summary = await (await get('/api/summary')).json();
    assert.equal(summary.animals, 1);
    assert.ok(summary.readings >= 1);
    assert.ok(Array.isArray(summary.by_condition));
  });

  test('animal list carries the latest condition', async () => {
    const { animals } = await (await get('/api/animals')).json();
    assert.equal(animals.length, 1);
    assert.equal(animals[0].latest_condition, 'heat_stress');
  });

  test('unknown animal is a 404, not an empty 200', async () => {
    assert.equal((await get('/api/animals/NOPE-9999')).status, 404);
  });

  test('telemetry comes back in ascending time order', async () => {
    await post('/api/sync/telemetry', {
      records: [
        { ...READING, timestamp: '2026-05-01T10:05:00+00:00' },
        { ...READING, timestamp: '2026-05-01T10:02:00+00:00' },
      ],
    });
    const { telemetry } = await (await get('/api/animals/CA-0001')).json();
    const times = telemetry.map((t) => t.timestamp);
    assert.deepEqual(times, [...times].sort());
  });
});

describe('alert actions', () => {
  test('alerts sync then appear in the open list', async () => {
    assert.equal((await (await post('/api/sync/alerts', { records: [ALERT] })).json()).written, 1);
    const { alerts } = await (await get('/api/alerts?status=open')).json();
    assert.equal(alerts.length, 1);
    assert.equal(alerts[0].rule_flags[0].code, 'HEAT_LOAD');
  });

  test('acknowledge requires a name', async () => {
    assert.equal((await post('/api/alerts/a-1/acknowledge', {})).status, 400);
  });

  test('acknowledge succeeds once, then conflicts', async () => {
    assert.equal((await post('/api/alerts/a-1/acknowledge', { acknowledged_by: 'vet-1' })).status, 200);
    // A second vet clicking the same button must be told, not silently ignored.
    assert.equal((await post('/api/alerts/a-1/acknowledge', { acknowledged_by: 'vet-2' })).status, 409);
  });

  test('resolve succeeds once, then conflicts', async () => {
    assert.equal((await post('/api/alerts/a-1/resolve', {})).status, 200);
    assert.equal((await post('/api/alerts/a-1/resolve', {})).status, 409);
  });

  test('acting on an unknown alert conflicts rather than 500s', async () => {
    assert.equal((await post('/api/alerts/does-not-exist/resolve', {})).status, 409);
  });
});

describe('live events', () => {
  test('a synced alert is pushed to a connected dashboard', async () => {
    const controller = new AbortController();
    const res = await fetch(`${base}/api/events`, { signal: controller.signal });
    const reader = res.body.getReader();
    await reader.read();  // the ": connected" preamble

    await post('/api/sync/alerts', {
      records: [{ ...ALERT, alert_id: 'a-2', severity: 'critical' }],
    });

    const { value } = await reader.read();
    const frame = new TextDecoder().decode(value);
    assert.match(frame, /event: alert/);
    assert.match(frame, /"alert_id":"a-2"/);

    controller.abort();
  });
});

describe('sync authentication', () => {
  test('a configured API key is enforced', async () => {
    const keyDir = mkdtempSync(join(tmpdir(), 'vetra-auth-'));
    const created = createApp({ dbPath: join(keyDir, 'auth.db'), apiKey: 'secret-key' });
    const authServer = created.app.listen(0);
    await new Promise((resolve) => authServer.once('listening', resolve));
    const authBase = `http://127.0.0.1:${authServer.address().port}`;

    const send = (headers) => fetch(`${authBase}/api/sync/telemetry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify({ records: [READING] }),
    });

    assert.equal((await send({})).status, 401, 'no key must be rejected');
    assert.equal((await send({ Authorization: 'Bearer wrong' })).status, 401);
    assert.equal((await send({ Authorization: 'Bearer secret-key' })).status, 200);

    // Reads stay open so the dashboard does not need the gateway's key.
    assert.equal((await fetch(`${authBase}/api/summary`)).status, 200);

    created.bus.close();
    authServer.close();
    created.repo.close();
    rmSync(keyDir, { recursive: true, force: true });
  });
});
