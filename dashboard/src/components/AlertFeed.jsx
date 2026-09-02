import { useState } from 'react';
import { conditionLabel, severityClass, timeAgo } from '../format.js';

const SEVERITY_ORDER = { critical: 4, high: 3, medium: 2, low: 1 };

/**
 * Alerts grouped by animal.
 *
 * An animal can legitimately carry more than one open alert — the engine tracks
 * one per condition, and comorbidity is real. Listing them flat makes a single
 * troubled animal look like a herd-wide outbreak, so each animal appears once,
 * ordered by its worst alert, with its concerns nested underneath.
 */
export default function AlertFeed({ alerts, freshIds, onAcknowledge, onResolve, onSelectAnimal }) {
  const [busy, setBusy] = useState(null);

  const act = async (id, fn) => {
    setBusy(id);
    try {
      await fn(id);
    } finally {
      setBusy(null);
    }
  };

  if (!alerts.length) {
    return (
      <div className="empty">
        <strong>No open alerts</strong>
        Every monitored animal is reading within its normal range.
      </div>
    );
  }

  const byAnimal = new Map();
  for (const alert of alerts) {
    if (!byAnimal.has(alert.animal_id)) byAnimal.set(alert.animal_id, []);
    byAnimal.get(alert.animal_id).push(alert);
  }

  const worst = (list) => Math.max(...list.map((a) => SEVERITY_ORDER[a.severity] ?? 0));
  const groups = [...byAnimal.entries()].sort((a, b) => worst(b[1]) - worst(a[1]));

  return (
    <div>
      {groups.map(([animalId, list]) => {
        const sorted = [...list].sort(
          (a, b) => (SEVERITY_ORDER[b.severity] ?? 0) - (SEVERITY_ORDER[a.severity] ?? 0),
        );
        return (
          <div className="alert-group" key={animalId}>
            <div className="alert-group-head">
              <button
                className="btn"
                onClick={() => onSelectAnimal?.(animalId)}
                title="Open this animal's record"
              >
                {animalId}
              </button>
              <span className="count">
                {sorted.length} {sorted.length === 1 ? 'concern' : 'concerns'}
              </span>
            </div>

            {sorted.map((alert) => (
              <div
                key={alert.alert_id}
                className={`alert ${severityClass(alert.severity)} ${
                  freshIds?.has(alert.alert_id) ? 'new' : ''
                }`}
              >
                <div className="alert-main">
                  <div className="alert-title">
                    <span className="pill">{alert.severity}</span>
                    <strong>{alert.condition_label ?? conditionLabel(alert.condition)}</strong>
                  </div>

                  <div className="alert-msg">{alert.message}</div>

                  {alert.rule_flags?.length > 0 && (
                    <div className="flags">
                      {alert.rule_flags.map((flag) => (
                        <span
                          key={flag.code}
                          className={`flag ${flag.severity}`}
                          title={flag.message}
                        >
                          {flag.code.replace(/_/g, ' ').toLowerCase()}
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="alert-meta">
                    <span>opened {timeAgo(alert.opened_at)}</span>
                    {alert.acknowledged_by && <span>· seen by {alert.acknowledged_by}</span>}
                  </div>
                </div>

                <div className="alert-actions">
                  <button
                    className="btn"
                    disabled={busy === alert.alert_id || alert.status !== 'open'}
                    onClick={() => act(alert.alert_id, onAcknowledge)}
                  >
                    {alert.status === 'acknowledged' ? 'Seen' : 'Acknowledge'}
                  </button>
                  <button
                    className="btn primary"
                    disabled={busy === alert.alert_id}
                    onClick={() => act(alert.alert_id, onResolve)}
                  >
                    Resolve
                  </button>
                </div>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
