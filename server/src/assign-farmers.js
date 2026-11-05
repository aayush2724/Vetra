/**
 * Register the demonstration farmers and hand every unowned animal to one of
 * them, through the cloud API, so the dashboard shows who to call.
 *
 * Ownership is a cloud-side record: the collars and the gateway know nothing
 * about people, and a real deployment assigns animals from the dashboard when
 * a collar is fitted. The demo streams a herd first and then runs this, which
 * is the same order of events.
 *
 *   node server/src/assign-farmers.js [http://localhost:4000]
 */
import { DEMO_FARMERS } from './lib/demo-farmers.js';

const BASE = (process.argv[2] ?? process.env.VETRA_CLOUD_URL ?? 'http://localhost:4000').replace(/\/$/, '');

const call = async (method, path, body) => {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${data.error ?? ''}`.trim());
  return data;
};

export async function assignDemoFarmers(base = BASE) {
  for (const farmer of DEMO_FARMERS) await call('POST', '/api/farmers', farmer);
  const { animals } = await call('GET', '/api/animals');
  let assigned = 0;
  // Round-robin by position, so each farmer gets a mix of species and of
  // healthy and sick animals rather than one farmer holding every goat.
  for (const [i, animal] of animals.entries()) {
    if (animal.owner) continue;
    await call('PATCH', `/api/animals/${encodeURIComponent(animal.animal_id)}`, {
      owner: DEMO_FARMERS[i % DEMO_FARMERS.length].farmer_id,
    });
    assigned += 1;
  }
  return { farmers: DEMO_FARMERS.length, animals: animals.length, assigned };
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  assignDemoFarmers()
    .then((r) => console.log(`Registered ${r.farmers} farmers; assigned ${r.assigned} of ${r.animals} animals at ${BASE}`))
    .catch((err) => { console.error(err.message); process.exit(1); });
}
