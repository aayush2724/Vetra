/**
 * Symptom checks, forwarded to the farm gateway.
 *
 * The model lives on the gateway and every diagnosis is made there, so this
 * service holds no model of its own: it passes the request through and returns
 * the gateway's answer, or says plainly that the gateway cannot be reached.
 */
import { Router } from 'express';

const TIMEOUT_MS = 15000;

export function diagnoseRouter({ gatewayUrl }) {
  const router = Router();
  const base = gatewayUrl.replace(/\/+$/, '');

  const forward = async (res, path, init) => {
    let response;
    try {
      response = await fetch(`${base}${path}`, { ...init, signal: AbortSignal.timeout(TIMEOUT_MS) });
    } catch {
      return res.status(503).json({
        error: 'The farm gateway is not reachable, so symptom checks are unavailable. '
          + 'Start it with `make gateway`.',
      });
    }
    const body = await response.json().catch(() => ({ error: 'invalid response from gateway' }));
    return res.status(response.ok ? 200 : response.status).json(body);
  };

  router.get('/symptoms', (req, res) => forward(res, '/symptoms'));

  router.post('/diagnose', (req, res) => {
    if (!req.body || typeof req.body !== 'object' || Array.isArray(req.body)) {
      return res.status(400).json({ error: 'body must be a JSON object' });
    }
    return forward(res, '/diagnose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body),
    });
  });

  return router;
}
