/**
 * Thin client for the cloud API, plus the live event stream.
 *
 * Paths are relative so the same build works behind the Vite dev proxy and when
 * the cloud service serves the built assets itself.
 */
const json = async (path, options) => {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error ?? `${response.status} ${response.statusText}`);
  }
  return response.json();
};

export const getSummary = () => json('/api/summary');
export const getAnimals = () => json('/api/animals');
export const getAnimal = (id, limit = 720) => json(`/api/animals/${encodeURIComponent(id)}?limit=${limit}`);
export const getAlerts = (status = 'open') =>
  json(`/api/alerts${status ? `?status=${status}` : ''}`);

export const acknowledgeAlert = (id, who) =>
  json(`/api/alerts/${encodeURIComponent(id)}/acknowledge`, {
    method: 'POST',
    body: JSON.stringify({ acknowledged_by: who }),
  });

export const resolveAlert = (id) =>
  json(`/api/alerts/${encodeURIComponent(id)}/resolve`, { method: 'POST' });

/**
 * Subscribe to server-sent events. Returns an unsubscribe function.
 * EventSource reconnects on its own, so there is no retry logic here.
 */
export function subscribe(handlers = {}) {
  const source = new EventSource('/api/events');
  const bound = [];

  const on = (name, fn) => {
    if (!fn) return;
    const wrapped = (event) => {
      try {
        fn(JSON.parse(event.data));
      } catch {
        // A malformed frame should not tear down a dashboard left open all day.
      }
    };
    source.addEventListener(name, wrapped);
    bound.push([name, wrapped]);
  };

  on('alert', handlers.onAlert);
  on('alert_updated', handlers.onAlertUpdated);
  on('diagnosis', handlers.onDiagnosis);

  source.onopen = () => handlers.onStatus?.(true);
  source.onerror = () => handlers.onStatus?.(false);

  return () => {
    for (const [name, fn] of bound) source.removeEventListener(name, fn);
    source.close();
  };
}
