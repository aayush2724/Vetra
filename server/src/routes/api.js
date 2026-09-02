/**
 * Read and action API for the farmer/veterinarian dashboard.
 */
import { Router } from 'express';

export function apiRouter(repo, bus) {
  const router = Router();

  const wrap = (fn) => (req, res) => {
    try {
      fn(req, res);
    } catch (err) {
      console.error(`${req.method} ${req.path} failed:`, err.message);
      res.status(500).json({ error: 'internal error' });
    }
  };

  const positiveInt = (value, fallback, max) => {
    const n = Number.parseInt(value, 10);
    return Number.isFinite(n) && n > 0 ? Math.min(n, max) : fallback;
  };

  router.get('/summary', wrap((req, res) => {
    res.json({ ...repo.herdSummary(), live_clients: bus.clientCount });
  }));

  router.get('/animals', wrap((req, res) => {
    res.json({ animals: repo.listAnimals() });
  }));

  router.get('/animals/:id', wrap((req, res) => {
    const animal = repo.getAnimal(req.params.id);
    if (!animal) return res.status(404).json({ error: 'animal not found' });
    res.json({
      animal,
      telemetry: repo.getTelemetry(req.params.id, positiveInt(req.query.limit, 720, 5000)),
      diagnoses: repo.getDiagnoses(req.params.id, 60),
    });
  }));

  router.get('/alerts', wrap((req, res) => {
    res.json({
      alerts: repo.listAlerts({
        status: req.query.status,
        severity: req.query.severity,
        limit: positiveInt(req.query.limit, 100, 500),
      }),
    });
  }));

  router.post('/alerts/:id/acknowledge', wrap((req, res) => {
    const who = req.body?.acknowledged_by;
    if (!who || typeof who !== 'string') {
      return res.status(400).json({ error: '`acknowledged_by` is required' });
    }
    // Only an open alert can be acknowledged, so a second click by another user
    // reports the conflict rather than silently overwriting the first.
    if (!repo.acknowledgeAlert(req.params.id, who)) {
      return res.status(409).json({ error: 'alert not found or not open' });
    }
    bus.broadcast('alert_updated', { alert_id: req.params.id, status: 'acknowledged', by: who });
    res.json({ ok: true });
  }));

  router.post('/alerts/:id/resolve', wrap((req, res) => {
    if (!repo.resolveAlert(req.params.id)) {
      return res.status(409).json({ error: 'alert not found or already resolved' });
    }
    bus.broadcast('alert_updated', { alert_id: req.params.id, status: 'resolved' });
    res.json({ ok: true });
  }));

  router.get('/events', (req, res) => {
    bus.addClient(res);
    req.on('close', () => res.end());
  });

  return router;
}
