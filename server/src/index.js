/**
 * Vetra cloud service: receives synced records from farm gateways and serves
 * the farmer/veterinarian dashboard.
 *
 * Deliberately not on the critical path for diagnosis. Every model decision and
 * alert is made on the edge device; if this server is unreachable the farm keeps
 * working and simply has a longer upload queue. That is the property that makes
 * the system usable where connectivity is intermittent.
 */
import cors from 'cors';
import express from 'express';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { HealthRecordRepository } from './db.js';
import { EventBus } from './lib/events.js';
import { apiRouter } from './routes/api.js';
import { syncRouter } from './routes/sync.js';

const HERE = dirname(fileURLToPath(import.meta.url));

const PORT = Number(process.env.PORT ?? 4000);
const DB_PATH = process.env.VETRA_CLOUD_DB ?? join(HERE, '..', 'vetra_cloud.db');
const API_KEY = process.env.VETRA_API_KEY ?? '';

export function createApp({ dbPath = DB_PATH, apiKey = API_KEY } = {}) {
  const repo = new HealthRecordRepository(dbPath);
  const bus = new EventBus();
  const app = express();

  app.use(cors());
  // Sync batches of raw telemetry are large; the default 100 KB limit would
  // reject them and the gateway would retry the same batch forever.
  app.use(express.json({ limit: '10mb' }));

  app.get('/health', (req, res) => res.json({ status: 'ok', uptime: process.uptime() }));
  app.use('/api/sync', syncRouter(repo, bus, { apiKey }));
  app.use('/api', apiRouter(repo, bus));

  // Serve the built dashboard when one exists, so a deployment is a single
  // process. In development Vite serves it instead and proxies /api here.
  const dashboard = join(HERE, '..', '..', 'dashboard', 'dist');
  if (existsSync(dashboard)) {
    app.use(express.static(dashboard));
    // Client-side routing: any non-API path falls through to the app shell.
    app.get(/^(?!\/api).*/, (req, res, next) => {
      if (req.method !== 'GET') return next();
      res.sendFile(join(dashboard, 'index.html'));
    });
  }

  app.use((req, res) => res.status(404).json({ error: 'not found' }));
  app.use((err, req, res, next) => {
    if (err?.type === 'entity.too.large') {
      return res.status(413).json({ error: 'payload too large' });
    }
    console.error('unhandled error:', err?.message ?? err);
    res.status(500).json({ error: 'internal error' });
  });

  return { app, repo, bus };
}

// Only listen when run directly, so tests can import createApp without a port.
if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  const { app, repo, bus } = createApp();
  const server = app.listen(PORT, () => {
    console.log(`Vetra cloud listening on http://localhost:${PORT}`);
    console.log(`  database   ${DB_PATH}`);
    console.log(`  sync auth  ${API_KEY ? 'API key required' : 'open (set VETRA_API_KEY to require one)'}`);
  });

  const shutdown = () => {
    console.log('\nshutting down ...');
    server.close(() => { bus.close(); repo.close(); process.exit(0); });
    setTimeout(() => process.exit(1), 5000).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}
