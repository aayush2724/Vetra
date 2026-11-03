import { useEffect, useState } from 'react';
import { diagnoseSymptoms, getSymptomCatalog } from '../api.js';
import { pct, severityClass } from '../format.js';

const READINGS = [
  { key: 'heart_rate', label: 'Heart rate', unit: 'bpm', digits: 0 },
  { key: 'body_temperature', label: 'Temperature', unit: '°C', digits: 1 },
  { key: 'respiratory_rate', label: 'Respiratory', unit: '/min', digits: 0 },
  { key: 'spo2', label: 'SpO₂', unit: '%', digits: 0 },
];

const blankForm = (catalog) => ({
  species: catalog.species[0],
  weather: catalog.defaults.weather,
  onset: catalog.defaults.onset,
  symptoms: Object.fromEntries(catalog.questions.map((q) => [q.key, 'normal'])),
});

/**
 * Pick an animal's symptoms from drop-downs and get a diagnosis.
 *
 * The answer comes from the same model the gateway runs on collar data: the
 * gateway turns each selection into the vital-sign change it describes and
 * diagnoses the result, so this screen holds no medical logic of its own.
 */
export default function SymptomCheck() {
  const [catalog, setCatalog] = useState(null);
  const [form, setForm] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getSymptomCatalog()
      .then((data) => {
        if (cancelled) return;
        setCatalog(data);
        setForm(blankForm(data));
      })
      .catch((err) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, []);

  const update = (patch) => {
    setForm((previous) => ({ ...previous, ...patch }));
    setResult(null);
  };
  const setSymptom = (key, value) => update({ symptoms: { ...form.symptoms, [key]: value } });

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await diagnoseSymptoms(form));
    } catch (err) {
      setError(err.message);
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setForm(blankForm(catalog));
    setResult(null);
    setError(null);
  };

  if (!catalog) {
    return (
      <div className="columns">
        <section className="card">
          <div className="card-body">
            {error
              ? <div className="banner">Symptom checks are unavailable: {error}</div>
              : <div className="skeleton" style={{ height: 320 }} />}
          </div>
        </section>
      </div>
    );
  }

  const chosen = Object.values(form.symptoms).filter((v) => v !== 'normal').length;

  return (
    <div className="columns">
      <section className="card">
        <div className="card-head">
          <h2>Symptom check</h2>
          <span className="hint">{chosen ? `${chosen} symptom${chosen > 1 ? 's' : ''} selected` : 'all normal'}</span>
        </div>
        <form className="card-body symptom-form" onSubmit={submit}>
          <div className="field-row">
            <Field label="Species">
              <select value={form.species} onChange={(e) => update({ species: e.target.value })}>
                {catalog.species.map((s) => <option key={s} value={s}>{capitalise(s)}</option>)}
              </select>
            </Field>
            <Field label="Weather">
              <select value={form.weather} onChange={(e) => update({ weather: e.target.value })}>
                {catalog.weather.map((w) => <option key={w.key} value={w.key}>{w.label}</option>)}
              </select>
            </Field>
            <Field label="Signs started">
              <select value={form.onset} onChange={(e) => update({ onset: e.target.value })}>
                {catalog.onset.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
              </select>
            </Field>
          </div>

          <div className="field-grid">
            {catalog.questions.map((q) => (
              <Field key={q.key} label={q.label} changed={form.symptoms[q.key] !== 'normal'}>
                <select value={form.symptoms[q.key]} onChange={(e) => setSymptom(q.key, e.target.value)}>
                  {q.options.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
                </select>
              </Field>
            ))}
          </div>

          <div className="form-actions">
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? 'Diagnosing…' : 'Diagnose'}
            </button>
            <button type="button" className="btn" onClick={reset} disabled={busy}>Reset</button>
          </div>
        </form>
      </section>

      <section className="card">
        <div className="card-head"><h2>Result</h2></div>
        <div className="card-body">
          {error && <div className="banner">{error}</div>}
          {busy && <div className="skeleton" style={{ height: 260 }} />}
          {!busy && !result && !error && (
            <div className="empty">
              <strong>No diagnosis yet</strong>
              Select the signs you see and press Diagnose.
            </div>
          )}
          {!busy && result && <Result result={result} />}
        </div>
      </section>
    </div>
  );
}

function Result({ result }) {
  const urgency = result.urgency === 'none' ? 'sev-ok' : severityClass(result.urgency);
  const uncertain = result.confidence < 0.6 || result.agreement < 0.6;

  return (
    <div className={`dx ${urgency}`}>
      <div className="dx-head">
        <div>
          <div className="dx-label">{result.label}</div>
          <div className="dx-sub">
            {pct(result.confidence)} confidence · {Math.round(result.agreement * result.simulated_animals)}
            {' '}of {result.simulated_animals} simulated {result.species} agree
          </div>
        </div>
        <span className="pill">{result.urgency === 'none' ? 'no action' : `${result.urgency} urgency`}</span>
      </div>

      {uncertain && (
        <p className="dx-note">
          The signs do not point clearly to one condition. Treat this as a possibility, not an answer.
        </p>
      )}

      <h3 className="dx-h">What to do</h3>
      <p className="dx-advice">{result.advice}</p>

      {result.findings.length > 0 && (
        <>
          <h3 className="dx-h">Clinical findings</h3>
          <ul className="dx-findings">
            {result.findings.map((f) => (
              <li key={f.code}><span className={`flag ${f.severity}`}>{f.code}</span> {f.message}</li>
            ))}
          </ul>
        </>
      )}

      <h3 className="dx-h">Likelihood by condition</h3>
      <div className="dx-bars">
        {result.probabilities.slice(0, 4).map((p) => (
          <div key={p.condition} className="dx-bar">
            <span className="name">{p.label}</span>
            <span className="track"><span className="fill" style={{ width: pct(p.probability) }} /></span>
            <span className="val">{pct(p.probability)}</span>
          </div>
        ))}
      </div>

      <h3 className="dx-h">Readings the model was given</h3>
      <div className="vitals">
        {READINGS.map((r) => (
          <div key={r.key} className="vital">
            <div className="k">{r.label}</div>
            <div className="v">
              {Number(result.readings[r.key]).toFixed(r.digits)}<span className="u">{r.unit}</span>
            </div>
          </div>
        ))}
      </div>

      <p className="dx-disclaimer">{result.disclaimer}</p>
    </div>
  );
}

function Field({ label, changed, children }) {
  return (
    <label className={`field ${changed ? 'changed' : ''}`}>
      <span>{label}</span>
      {children}
    </label>
  );
}

const capitalise = (s) => s.charAt(0).toUpperCase() + s.slice(1);
