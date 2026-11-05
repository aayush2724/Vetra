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

  // A printable record for one animal; the dashboard renders and prints it.
  router.get('/animals/:id/report', wrap((req, res) => {
    const report = repo.animalReport(req.params.id);
    if (!report) return res.status(404).json({ error: 'animal not found' });
    res.json(report);
  }));

  const optionalString = (value) => value === undefined || value === null || typeof value === 'string';

  // Ownership and the display name are set here, at the farm office, never
  // by the gateway: a sync from the device leaves both untouched.
  router.patch('/animals/:id', wrap((req, res) => {
    const body = req.body ?? {};
    const hasOwner = Object.hasOwn(body, 'owner');
    const hasName = Object.hasOwn(body, 'name');
    if (!hasOwner && !hasName) {
      return res.status(400).json({ error: 'send `owner` and/or `name`' });
    }
    if (!optionalString(body.owner) || !optionalString(body.name)) {
      return res.status(400).json({ error: '`owner` and `name` must be strings or null' });
    }
    if (hasOwner && body.owner && !repo.getFarmer(body.owner)) {
      return res.status(404).json({ error: 'farmer not found' });
    }
    const changed = repo.assignAnimal(req.params.id, {
      owner: hasOwner ? (body.owner || null) : undefined,
      name: hasName ? (body.name?.trim() || null) : undefined,
    });
    if (!changed) return res.status(404).json({ error: 'animal not found' });
    bus.broadcast('animal_updated', { animal_id: req.params.id });
    res.json({ animal: repo.getAnimal(req.params.id) });
  }));

  router.get('/farmers', wrap((req, res) => {
    res.json({ farmers: repo.listFarmers() });
  }));

  router.post('/farmers', wrap((req, res) => {
    const body = req.body ?? {};
    const name = typeof body.name === 'string' ? body.name.trim() : '';
    if (!name) return res.status(400).json({ error: '`name` is required' });
    for (const field of ['farmer_id', 'phone', 'village', 'notes']) {
      if (!optionalString(body[field])) {
        return res.status(400).json({ error: `\`${field}\` must be a string` });
      }
    }
    const farmer = repo.saveFarmer({
      farmer_id: body.farmer_id?.trim() || undefined,
      name,
      phone: body.phone?.trim() || null,
      village: body.village?.trim() || null,
      notes: body.notes?.trim() || null,
    });
    bus.broadcast('animal_updated', { farmer_id: farmer.farmer_id });
    res.status(201).json({ farmer });
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

  // `?status=a&status=b` arrives as an array; binding an array into SQL throws.
  const single = (value) => (Array.isArray(value) ? value[0] : value);

  router.get('/alerts', wrap((req, res) => {
    const status = single(req.query.status);
    const severity = single(req.query.severity);
    if ((status !== undefined && typeof status !== 'string')
        || (severity !== undefined && typeof severity !== 'string')) {
      return res.status(400).json({ error: 'status and severity must be plain strings' });
    }
    res.json({
      alerts: repo.listAlerts({
        status,
        severity,
        limit: positiveInt(single(req.query.limit), 100, 500),
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
