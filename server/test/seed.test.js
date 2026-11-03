/**
 * The seed must produce records the dashboard renders exactly like gateway
 * output, and must be safe to run twice.
 */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, test } from 'node:test';

import { HealthRecordRepository } from '../src/db.js';
import { HERD, seedRepository } from '../src/seed.js';

const NOW = new Date('2026-05-02T10:00:00Z');

describe('seed', () => {
  let dir; let repo;

  before(() => {
    dir = mkdtempSync(join(tmpdir(), 'vetra-seed-'));
    repo = new HealthRecordRepository(join(dir, 'seed.db'));
  });

  after(() => {
    repo.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test('writes a herd the dashboard can render', () => {
    const written = seedRepository(repo, { now: NOW });
    assert.equal(written.animals, HERD.length);
    assert.ok(written.telemetry > 0 && written.diagnoses > 0);

    const sick = HERD.filter((a) => a.condition !== 'healthy');
    const summary = repo.herdSummary();
    assert.equal(summary.animals, HERD.length);
    assert.equal(summary.open_alerts, sick.length, 'one open alert per sick animal');

    // Every animal carries a latest diagnosis, and the sick ones carry theirs.
    const animals = repo.listAnimals();
    for (const animal of animals) assert.ok(animal.latest_condition, animal.animal_id);
    for (const { animal_id, condition } of sick) {
      assert.equal(animals.find((a) => a.animal_id === animal_id).latest_condition, condition);
    }

    // Alerts have the fields the feed shows: a message and the clinical flags.
    for (const alert of repo.listAlerts({ status: 'open' })) {
      assert.match(alert.message, /possible/);
      assert.ok(Array.isArray(alert.rule_flags));
      assert.ok(['low', 'medium', 'high', 'critical'].includes(alert.severity));
    }

    // Telemetry ends at `now` and reads back oldest first.
    const telemetry = repo.getTelemetry('CA-0001', 5000);
    assert.equal(telemetry.at(-1).timestamp, NOW.toISOString());
    const times = telemetry.map((t) => t.timestamp);
    assert.deepEqual(times, [...times].sort());
  });

  test('re-seeding does not duplicate records', () => {
    const before = repo.herdSummary();
    seedRepository(repo, { now: NOW });
    const after = repo.herdSummary();
    assert.equal(after.readings, before.readings);
    assert.equal(after.diagnoses, before.diagnoses);
    assert.equal(after.open_alerts, before.open_alerts);
  });
});
