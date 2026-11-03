/**
 * Symptom-check forwarding. A stand-in gateway records what it receives, so the
 * tests check what the cloud service sends and returns without a Python process.
 */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, test } from 'node:test';

import { createApp } from '../src/index.js';

const listen = async (server) => {
  server.listen(0);
  await new Promise((resolve) => server.once('listening', resolve));
  return `http://127.0.0.1:${server.address().port}`;
};

const startCloud = async (gatewayUrl, dir) => {
  const created = createApp({ dbPath: join(dir, `cloud-${Date.now()}.db`), gatewayUrl });
  const server = created.app.listen(0);
  await new Promise((resolve) => server.once('listening', resolve));
  return { ...created, server, base: `http://127.0.0.1:${server.address().port}` };
};

const stopCloud = ({ server, bus, repo }) => {
  bus.close();
  server.close();
  repo.close();
};

const post = (base, body) =>
  fetch(`${base}/api/diagnose`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });

describe('symptom checks with a gateway', () => {
  let gateway; let cloud; let dir;
  const received = [];

  before(async () => {
    dir = mkdtempSync(join(tmpdir(), 'vetra-diagnose-'));
    gateway = createServer((req, res) => {
      let raw = '';
      req.on('data', (chunk) => { raw += chunk; });
      req.on('end', () => {
        received.push({ method: req.method, url: req.url, body: raw ? JSON.parse(raw) : null });
        res.setHeader('Content-Type', 'application/json');
        if (req.url === '/symptoms') {
          return res.end(JSON.stringify({ species: ['cattle'], questions: [] }));
        }
        const body = JSON.parse(raw);
        if (body.species !== 'cattle') {
          res.statusCode = 400;
          return res.end(JSON.stringify({ error: `unknown species '${body.species}'` }));
        }
        return res.end(JSON.stringify({ condition: 'heat_stress', confidence: 0.97 }));
      });
    });
    cloud = await startCloud(await listen(gateway), dir);
  });

  after(() => {
    stopCloud(cloud);
    gateway.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test('the symptom catalog comes from the gateway', async () => {
    const res = await fetch(`${cloud.base}/api/symptoms`);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { species: ['cattle'], questions: [] });
  });

  test('a check is forwarded unchanged and the diagnosis returned', async () => {
    const request = { species: 'cattle', symptoms: { breathing: 'panting' }, weather: 'hot' };
    const res = await post(cloud.base, request);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { condition: 'heat_stress', confidence: 0.97 });
    const last = received.at(-1);
    assert.equal(last.method, 'POST');
    assert.equal(last.url, '/diagnose');
    assert.deepEqual(last.body, request);
  });

  test("the gateway's validation error reaches the caller", async () => {
    const res = await post(cloud.base, { species: 'camel', symptoms: {} });
    assert.equal(res.status, 400);
    assert.match((await res.json()).error, /unknown species/);
  });

  test('a body that is not an object is rejected before forwarding', async () => {
    const before = received.length;
    const res = await post(cloud.base, '["cattle"]');
    assert.equal(res.status, 400);
    assert.equal(received.length, before);
  });
});

describe('symptom checks without a gateway', () => {
  test('an unreachable gateway is reported as unavailable, not as a crash', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'vetra-diagnose-'));
    // Bind and release a port so nothing is listening on it.
    const probe = createServer();
    const dead = await listen(probe);
    await new Promise((resolve) => probe.close(resolve));

    const cloud = await startCloud(dead, dir);
    try {
      const res = await post(cloud.base, { species: 'cattle', symptoms: {} });
      assert.equal(res.status, 503);
      assert.match((await res.json()).error, /gateway is not reachable/);
    } finally {
      stopCloud(cloud);
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
