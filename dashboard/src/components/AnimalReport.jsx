import { useEffect, useState } from 'react';
import { getAnimalReport } from '../api.js';
import { clockTime, conditionLabel, pct, severityClass } from '../format.js';

/**
 * The printable record for one animal: identity and owner, the span of data
 * held, each vital against its species band, the diagnosis history as runs of
 * one condition, and every alert ever raised. Print it from the browser for a
 * PDF, or download the JSON the page was rendered from.
 */
export default function AnimalReport({ animalId, onClose }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setReport(null);
    setError(null);
    getAnimalReport(animalId)
      .then((r) => { if (!cancelled) setReport(r); })
      .catch((err) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [animalId]);

  const download = () => {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${animalId}-health-report.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (error) return <div className="banner">Could not build the report for {animalId}: {error}</div>;
  if (!report) return <div className="card-body"><div className="skeleton" style={{ height: 320 }} /></div>;

  const { animal, period, vitals, latest_diagnosis: latest, condition_history: history, alerts, alert_counts: counts } = report;
  const num = (v, digits) => (v == null ? '--' : Number(v).toFixed(digits));
  const when = (iso) => (iso ? clockTime(iso) : '--');

  return (
    <section className="card report">
      <div className="card-head no-print">
        <h2>Animal health report</h2>
        <div className="report-actions">
          <button type="button" className="btn" onClick={onClose}>Back</button>
          <button type="button" className="btn" onClick={download}>Download JSON</button>
          <button type="button" className="btn primary" onClick={() => window.print()}>Print / save as PDF</button>
        </div>
      </div>

      <div className="card-body">
        <div className="report-title">
          <div>
            <h1>{animal.animal_id}{animal.name ? ` · ${animal.name}` : ''}</h1>
            <div className="hint">
              <span style={{ textTransform: 'capitalize' }}>{animal.species}</span>
              {' · '}farm {animal.farm_id}
              {' · '}enrolled {when(animal.enrolled_at)}
            </div>
          </div>
          <div className="report-meta">
            <div><b>Generated</b> {when(report.generated_at)}</div>
            <div><b>Farmer</b> {animal.farmer_name ?? 'not assigned'}</div>
            {animal.farmer_phone && <div><b>Phone</b> {animal.farmer_phone}</div>}
            {animal.farmer_village && <div><b>Village</b> {animal.farmer_village}</div>}
          </div>
        </div>

        <div className="tiles report-tiles">
          <div className="tile"><div className="label">Readings held</div><div className="value">{Number(period.readings).toLocaleString()}</div>
            <div className="sub">{period.first_reading ? `${when(period.first_reading)} to ${when(period.last_reading)}` : 'none'}</div></div>
          <div className="tile"><div className="label">Days of history</div><div className="value">{num(period.days, 1)}</div></div>
          <div className="tile"><div className="label">Alerts ever raised</div><div className="value">{counts.total}</div>
            <div className="sub">{counts.open} open · {counts.acknowledged} seen · {counts.resolved} resolved</div></div>
          <div className={`tile ${latest && latest.condition !== 'healthy' ? 'alarm' : ''}`}>
            <div className="label">Latest diagnosis</div>
            <div className="value" style={{ fontSize: 20 }}>{latest ? conditionLabel(latest.condition) : '--'}</div>
            {latest && <div className="sub">{pct(latest.confidence)} confidence · {when(latest.timestamp)}</div>}
          </div>
        </div>

        {latest?.rule_flags?.length > 0 && (
          <div className="report-section">
            <h3>Current clinical findings</h3>
            <ul className="dx-findings">
              {latest.rule_flags.map((f) => (
                <li key={f.code}><span className={`flag ${f.severity}`}>{f.code.replace(/_/g, ' ').toLowerCase()}</span> {f.message}</li>
              ))}
            </ul>
          </div>
        )}

        <div className="report-section">
          <h3>Vital signs over the whole record</h3>
          <table className="rtable">
            <thead><tr><th>Measure</th><th>Mean</th><th>Min</th><th>Max</th><th>Species normal</th><th>Time outside normal</th></tr></thead>
            <tbody>
              {vitals.map((v) => (
                <tr key={v.channel} className={v.outside_normal > 0.25 ? 'warn' : ''}>
                  <td>{v.label}{v.unit ? ` (${v.unit})` : ''}</td>
                  <td>{num(v.mean, v.digits)}</td>
                  <td>{num(v.min, v.digits)}</td>
                  <td>{num(v.max, v.digits)}</td>
                  <td>{v.normal ? `${v.normal[0]} to ${v.normal[1]}` : '--'}</td>
                  <td>{v.outside_normal == null ? '--' : pct(v.outside_normal)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="report-section">
          <h3>Diagnosis history</h3>
          {history.length ? (
            <table className="rtable">
              <thead><tr><th>Condition</th><th>From</th><th>To</th><th>Windows</th><th>Peak confidence</th></tr></thead>
              <tbody>
                {history.map((run) => (
                  <tr key={run.from} className={run.condition === 'healthy' ? '' : 'warn'}>
                    <td>{conditionLabel(run.condition)}</td>
                    <td>{when(run.from)}</td>
                    <td>{when(run.to)}</td>
                    <td>{run.windows}</td>
                    <td>{pct(run.peak_confidence)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="empty">No diagnoses recorded yet.</div>}
        </div>

        <div className="report-section">
          <h3>Alert history</h3>
          {alerts.length ? (
            <table className="rtable">
              <thead><tr><th>Opened</th><th>Condition</th><th>Severity</th><th>Status</th><th>Findings</th><th>Seen by</th><th>Resolved</th></tr></thead>
              <tbody>
                {alerts.map((a) => (
                  <tr key={a.alert_id} className={severityClass(a.severity)}>
                    <td>{when(a.opened_at)}</td>
                    <td>{a.condition_label ?? conditionLabel(a.condition)}</td>
                    <td><span className="pill">{a.severity}</span></td>
                    <td>{a.status}</td>
                    <td>{a.rule_flags.map((f) => f.code.replace(/_/g, ' ').toLowerCase()).join(', ') || '--'}</td>
                    <td>{a.acknowledged_by ?? '--'}</td>
                    <td>{when(a.resolved_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="empty">No alerts have been raised for this animal.</div>}
        </div>

        <p className="dx-disclaimer">
          Diagnoses are produced on the farm gateway by a model trained on simulated data and by
          textbook clinical thresholds. This report is a monitoring record, not a veterinary diagnosis.
        </p>
      </div>
    </section>
  );
}
