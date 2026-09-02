import { useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid, Line, LineChart, ReferenceArea, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts';
import { getAnimal } from '../api.js';
import {
  clockTime, conditionClass, conditionLabel, isOutOfRange, NORMAL_RANGE, pct, timeAgo,
} from '../format.js';

const CHARTS = [
  { key: 'heart_rate', title: 'Heart rate', unit: 'bpm', colour: '#c0392b' },
  { key: 'body_temperature', title: 'Body temperature', unit: '°C', colour: '#d9822b' },
  { key: 'respiratory_rate', title: 'Respiratory rate', unit: '/min', colour: '#1f7a4d' },
  { key: 'spo2', title: 'Oxygen saturation', unit: '%', colour: '#4a6fa5' },
];

const VITALS = [
  { key: 'heart_rate', label: 'Heart rate', unit: 'bpm', digits: 0 },
  { key: 'body_temperature', label: 'Temperature', unit: '°C', digits: 1 },
  { key: 'respiratory_rate', label: 'Respiratory', unit: '/min', digits: 0 },
  { key: 'spo2', label: 'SpO₂', unit: '%', digits: 0 },
  { key: 'activity_index', label: 'Activity', unit: '', digits: 0 },
  { key: 'step_regularity', label: 'Gait rhythm', unit: '', digits: 2 },
];

/**
 * One animal's record: current vitals, trends against the species normal band,
 * and the diagnosis history the alerts were drawn from.
 *
 * The shaded band on each chart is the species reference range. Showing it is
 * what makes a trace readable to someone who does not know that 84 bpm is fine
 * for cattle but high for a buffalo.
 */
export default function AnimalDetail({ animalId, refreshKey }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!animalId) return undefined;
    let cancelled = false;

    setLoading(true);
    getAnimal(animalId, 720)
      .then((payload) => {
        if (!cancelled) { setData(payload); setError(null); }
      })
      .catch((err) => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });

    return () => { cancelled = true; };
  }, [animalId, refreshKey]);

  const series = useMemo(() => {
    if (!data?.telemetry) return [];
    // A full window is ~720 points; thinning keeps the chart honest in shape
    // while staying responsive on a phone.
    const rows = data.telemetry;
    const step = Math.max(1, Math.ceil(rows.length / 360));
    return rows
      .filter((_, i) => i % step === 0)
      .map((row) => ({ ...row, t: new Date(row.timestamp).getTime() }));
  }, [data]);

  if (!animalId) {
    return <div className="empty"><strong>Select an animal</strong>Pick one from the herd to see its record.</div>;
  }
  if (loading && !data) {
    return <div className="card-body"><div className="skeleton" style={{ height: 260 }} /></div>;
  }
  if (error) return <div className="banner">Could not load {animalId}: {error}</div>;
  if (!data) return null;

  const { animal, diagnoses } = data;
  const latest = data.telemetry.at(-1);
  const ranges = NORMAL_RANGE[animal.species] ?? {};

  return (
    <div className="card-body">
      <div className="card-head" style={{ padding: '0 0 12px', border: 0 }}>
        <div>
          <h2>{animal.animal_id} · <span style={{ textTransform: 'capitalize', fontWeight: 500 }}>{animal.species}</span></h2>
          <div className="hint">
            {animal.name ? `${animal.name} · ` : ''}
            {latest ? `last reading ${timeAgo(latest.timestamp)}` : 'no readings yet'}
          </div>
        </div>
      </div>

      {latest ? (
        <div className="vitals">
          {VITALS.map((vital) => {
            const value = latest[vital.key];
            const out = isOutOfRange(animal.species, vital.key, value);
            return (
              <div className={`vital ${out ? 'out' : ''}`} key={vital.key}>
                <div className="k">{vital.label}</div>
                <div className="v">
                  {value == null ? '--' : Number(value).toFixed(vital.digits)}
                  {vital.unit && <span className="u">{vital.unit}</span>}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty">No telemetry synced for this animal yet.</div>
      )}

      {series.length > 1 && CHARTS.map((chart) => {
        const band = ranges[chart.key];
        return (
          <div className="chart-wrap" key={chart.key}>
            <h3>{chart.title} <span style={{ fontWeight: 400 }}>({chart.unit})</span>
              {band && <span style={{ color: 'var(--text-faint)', fontWeight: 400 }}>
                {' '}· normal {band[0]}–{band[1]}</span>}
            </h3>
            <ResponsiveContainer width="100%" height={140}>
              <LineChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
                <CartesianGrid strokeDasharray="2 4" stroke="var(--border)" vertical={false} />
                {band && (
                  <ReferenceArea
                    y1={band[0]} y2={band[1]}
                    fill={chart.colour} fillOpacity={0.07} stroke="none"
                  />
                )}
                <XAxis
                  dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']}
                  tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  tick={{ fontSize: 10, fill: 'var(--text-faint)' }}
                  stroke="var(--border)" minTickGap={40}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: 'var(--text-faint)' }}
                  stroke="var(--border)" domain={['auto', 'auto']} width={42}
                />
                <Tooltip
                  contentStyle={{
                    background: 'var(--surface)', border: '1px solid var(--border)',
                    borderRadius: 8, fontSize: 12, color: 'var(--text)',
                  }}
                  labelFormatter={(t) => clockTime(new Date(t).toISOString())}
                  formatter={(value) => [Number(value).toFixed(1), chart.title]}
                />
                <Line
                  type="monotone" dataKey={chart.key} stroke={chart.colour}
                  strokeWidth={1.6} dot={false} isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        );
      })}

      <div className="chart-wrap">
        <h3>Diagnosis history</h3>
        {diagnoses?.length ? (
          <div className="timeline">
            {diagnoses.slice(0, 14).map((d) => (
              <div className={`tl-row ${conditionClass(d.condition)}`} key={d.timestamp}>
                <span className="t">{clockTime(d.timestamp)}</span>
                <span className="c">{conditionLabel(d.condition)}</span>
                <span className="p">{pct(d.confidence)}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="empty">No diagnoses recorded yet.</div>
        )}
      </div>
    </div>
  );
}
