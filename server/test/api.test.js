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
const patch = (path, body) =>
  fetch(`${base}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

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

  test('active filter keeps acknowledged alerts, open filter drops them', async () => {
    // The dashboard lists `active` so a seen-but-unresolved alert stays on screen.
    const { alerts: open } = await (await get('/api/alerts?status=open')).json();
    assert.equal(open.length, 0);
    const { alerts: active } = await (await get('/api/alerts?status=active')).json();
    assert.deepEqual(active.map((a) => a.alert_id), ['a-1']);
    assert.equal(active[0].status, 'acknowledged');
  });

  test('resolve succeeds once, then conflicts', async () => {
    assert.equal((await post('/api/alerts/a-1/resolve', {})).status, 200);
    assert.equal((await post('/api/alerts/a-1/resolve', {})).status, 409);
  });

  test('resolved alerts drop out of the active list', async () => {
    const { alerts } = await (await get('/api/alerts?status=active')).json();
    assert.equal(alerts.length, 0);
  });

  test('acting on an unknown alert conflicts rather than 500s', async () => {
    assert.equal((await post('/api/alerts/does-not-exist/resolve', {})).status, 409);
  });
});

describe('gateway re-sends and client mistakes', () => {
  test('an acknowledged alert keeps its acknowledgement when the gateway escalates it', async () => {
    const alert = { ...ALERT, alert_id: 'a-3', updated_at: '2026-05-01T11:00:00+00:00' };
    await post('/api/sync/alerts', { records: [alert] });
    assert.equal((await post('/api/alerts/a-3/acknowledge', { acknowledged_by: 'vet-1' })).status, 200);

    await post('/api/sync/alerts', {
      records: [{ ...alert, severity: 'critical', status: 'open', updated_at: '2026-05-01T11:30:00+00:00' }],
    });
    const { alerts } = await (await get('/api/alerts?status=active')).json();
    const a3 = alerts.find((a) => a.alert_id === 'a-3');
    assert.equal(a3.status, 'acknowledged', 'the vet already saw it');
    assert.equal(a3.severity, 'critical', 'but the escalation must show');
    assert.equal(a3.acknowledged_by, 'vet-1');
  });

  test('a resolved alert stays resolved when the gateway retries an older version', async () => {
    const alert = { ...ALERT, alert_id: 'a-4' };
    await post('/api/sync/alerts', { records: [alert] });
    assert.equal((await post('/api/alerts/a-4/resolve', {})).status, 200);

    await post('/api/sync/alerts', { records: [alert] });
    const { alerts: resolved } = await (await get('/api/alerts?status=resolved')).json();
    assert.ok(resolved.some((a) => a.alert_id === 'a-4'));
    const { alerts: active } = await (await get('/api/alerts?status=active')).json();
    assert.ok(!active.some((a) => a.alert_id === 'a-4'));
  });

  test('the gateway resolving an acknowledged alert resolves it', async () => {
    const alert = { ...ALERT, alert_id: 'a-5' };
    await post('/api/sync/alerts', { records: [alert] });
    await post('/api/alerts/a-5/acknowledge', { acknowledged_by: 'vet-1' });
    await post('/api/sync/alerts', {
      records: [{ ...alert, status: 'resolved', resolved_at: '2026-05-01T13:00:00+00:00' }],
    });
    const { alerts } = await (await get('/api/alerts?status=resolved')).json();
    const a5 = alerts.find((a) => a.alert_id === 'a-5');
    assert.equal(a5.resolved_at, '2026-05-01T13:00:00+00:00');
    assert.equal(a5.acknowledged_by, 'vet-1');
  });

  test('malformed JSON is a 400, not a 500', async () => {
    const res = await fetch(`${base}/api/sync/telemetry`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"records": [}',
    });
    assert.equal(res.status, 400);
  });

  test('a malformed record rejects the batch with 400 and writes nothing', async () => {
    const before = (await (await get('/api/summary')).json()).readings;
    const res = await post('/api/sync/telemetry', {
      records: [{ ...READING, timestamp: '2026-05-03T00:00:00+00:00' }, null],
    });
    assert.equal(res.status, 400);
    assert.match((await res.json()).error, /record 1/);
    assert.equal((await (await get('/api/summary')).json()).readings, before);
  });

  test('a repeated query parameter does not 500', async () => {
    assert.equal((await get('/api/alerts?status=open&status=resolved')).status, 200);
  });

  test('the summary records when a gateway last synced', async () => {
    const before = Date.now() - 1000;
    await post('/api/sync/animals', { records: [{ animal_id: 'CA-0002', species: 'goat' }] });
    const summary = await (await get('/api/summary')).json();
    assert.ok(new Date(summary.last_sync_at).getTime() >= before);
  });
});

describe('farmers and reports', () => {
  test('a farmer can be registered and listed with counts', async () => {
    const res = await post('/api/farmers', {
      name: 'Ramesh Yadav', phone: '+91 98765 10001', village: 'Malihabad',
    });
    assert.equal(res.status, 201);
    const { farmer } = await res.json();
    assert.equal(farmer.farmer_id, 'ramesh-yadav');
    const { farmers } = await (await get('/api/farmers')).json();
    assert.equal(farmers.length, 1);
    assert.equal(farmers[0].animals, 0);
  });

  test('registering a farmer needs a name', async () => {
    assert.equal((await post('/api/farmers', { phone: '1' })).status, 400);
  });

  test('two farmers with the same name get distinct ids', async () => {
    const { farmer } = await (await post('/api/farmers', { name: 'Ramesh Yadav' })).json();
    assert.notEqual(farmer.farmer_id, 'ramesh-yadav');
    assert.ok(farmer.farmer_id.startsWith('ramesh-yadav-'));
  });

  test('an animal can be assigned to a farmer and given a name', async () => {
    const res = await patch('/api/animals/CA-0001', { owner: 'ramesh-yadav', name: 'Ganga' });
    assert.equal(res.status, 200);
    const { animal } = await res.json();
    assert.equal(animal.farmer_name, 'Ramesh Yadav');
    assert.equal(animal.name, 'Ganga');

    const { animals } = await (await get('/api/animals')).json();
    assert.equal(animals[0].farmer_phone, '+91 98765 10001');
    const { farmers } = await (await get('/api/farmers')).json();
    assert.equal(farmers.find((f) => f.farmer_id === 'ramesh-yadav').animals, 1);
  });

  test('assigning an unknown farmer or animal is a 404, an empty patch a 400', async () => {
    assert.equal((await patch('/api/animals/CA-0001', { owner: 'nobody' })).status, 404);
    assert.equal((await patch('/api/animals/NOPE-1', { owner: 'ramesh-yadav' })).status, 404);
    assert.equal((await patch('/api/animals/CA-0001', {})).status, 400);
    assert.equal((await patch('/api/animals/CA-0001', { owner: 42 })).status, 400);
  });

  test('a gateway re-sync of the animal keeps its owner and name', async () => {
    // The gateway sends what it knows, which is species only.
    await post('/api/sync/animals', { records: [{ animal_id: 'CA-0001', species: 'cattle' }] });
    const { animal } = await (await get('/api/animals/CA-0001')).json();
    assert.equal(animal.owner, 'ramesh-yadav');
    assert.equal(animal.name, 'Ganga');
  });

  test('alerts carry the farmer to call', async () => {
    await post('/api/sync/alerts', { records: [{ ...ALERT, alert_id: 'a-farmer' }] });
    const { alerts } = await (await get('/api/alerts?status=open')).json();
    const mine = alerts.find((a) => a.alert_id === 'a-farmer');
    assert.equal(mine.farmer_name, 'Ramesh Yadav');
    assert.equal(mine.farmer_phone, '+91 98765 10001');
    assert.equal(mine.animal_name, 'Ganga');
  });

  test('the report assembles owner, vitals, history and every alert', async () => {
    const res = await get('/api/animals/CA-0001/report');
    assert.equal(res.status, 200);
    const report = await res.json();
    assert.equal(report.animal.farmer_name, 'Ramesh Yadav');
    assert.ok(report.period.readings >= 3);
    const hr = report.vitals.find((v) => v.channel === 'heart_rate');
    assert.deepEqual(hr.normal, [48, 84]);
    assert.equal(hr.outside_normal, 0, 'a 68 bpm cow is inside its band');
    assert.ok(report.condition_history.length >= 1);
    assert.ok(report.alerts.some((a) => a.alert_id === 'a-1' && a.status === 'resolved'),
      'resolved alerts stay in the record');
    assert.equal(report.alert_counts.total, report.alerts.length);
    assert.ok(Array.isArray(report.alerts[0].rule_flags));
  });

  test('a report for an unknown animal is a 404', async () => {
    assert.equal((await get('/api/animals/NOPE-1/report')).status, 404);
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
