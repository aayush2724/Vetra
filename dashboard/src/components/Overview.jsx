import { useMemo } from 'react';
import { conditionLabel, severityClass, timeAgo } from '../format.js';
import {
  IconAlert, IconClipboard, IconHerd, IconPulse, IconSignal, IconTag, IconUsers,
} from './icons.jsx';

/**
 * The overview cards: the herd's health at a glance, in the idiom of a
 * commercial farm dashboard — one headline number per card, thin meters for
 * every "x of N", and a donut for the herd split.
 *
 * Everything here is derived from the three lists the app already loads
 * (summary, animals, open alerts); the cards add no requests of their own.
 * Simulated time runs far behind the wall clock, so "recent" is measured
 * against the herd's own latest reading, never against Date.now().
 */

const HOUR = 3600 * 1000;
const SEVERITIES = ['critical', 'high', 'medium', 'low'];
const SEVERITY_LABEL = { critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low' };
const SEVERITY_RANK = { critical: 4, high: 3, medium: 2, low: 1 };

// Weather findings describe the whole herd, not the animal; alerts.py treats
// them the same way when deciding what counts as critical vitals.
const ENVIRONMENTAL = new Set(['HEAT_ADVISORY', 'HEAT_LOAD', 'SEVERE_HEAT_LOAD']);

// Rule codes folded into the findings a farmer recognises.
const FINDINGS = [
  ['Low oxygen', ['HYPOXAEMIA', 'SEVERE_HYPOXAEMIA']],
  ['Fast breathing', ['TACHYPNOEA', 'SEVERE_TACHYPNOEA']],
  ['Fever', ['FEVER', 'HIGH_FEVER']],
  ['Hypothermia', ['HYPOTHERMIA']],
  ['Resting tachycardia', ['RESTING_TACHYCARDIA']],
  ['Heat load', ['HEAT_ADVISORY', 'HEAT_LOAD', 'SEVERE_HEAT_LOAD']],
  ['Rumination collapse', ['RUMINATION_COLLAPSE']],
  ['Recumbency', ['RECUMBENCY']],
  ['Irregular gait', ['IRREGULAR_GAIT']],
];
const FLAG_RANK = { critical: 3, warning: 2, info: 1 };
const FLAG_COLOUR = { critical: 'var(--critical)', warning: 'var(--high)', info: 'var(--low)' };

const SPECIES_ORDER = ['cattle', 'buffalo', 'goat', 'sheep'];

const parseTime = (iso) => {
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? t : null;
};

/** Alerts from the list API carry their gateway record as a JSON string; the live stream sends it whole. */
function flagsOf(alert) {
  if (Array.isArray(alert.rule_flags)) return alert.rule_flags;
  if (typeof alert.payload === 'string') {
    try { return JSON.parse(alert.payload).rule_flags ?? []; } catch { return []; }
  }
  return alert.payload?.rule_flags ?? [];
}

const fmt = (n) => (n == null ? '--' : Number(n).toLocaleString());

export default function Overview({
  summary, animals, alerts, live, farmers = [], ownerFilter = '', onPickFarmer,
}) {
  const model = useMemo(() => {
    const times = [
      ...animals.map((a) => parseTime(a.latest_seen)),
      ...alerts.map((a) => parseTime(a.updated_at)),
    ].filter((t) => t != null);
    const herdNow = times.length ? Math.max(...times) : null;

    const sickAnimals = new Set(alerts.map((a) => a.animal_id)).size;
    const newAlerts = herdNow == null ? 0 : alerts.filter((a) => {
      const opened = parseTime(a.opened_at);
      return opened != null && herdNow - opened <= 24 * HOUR;
    }).length;

    const bySeverity = SEVERITIES.map((s) => ({
      key: s, label: SEVERITY_LABEL[s],
      value: alerts.filter((a) => a.severity === s).length,
      colour: `var(--${s})`,
    }));

    const criticalVitals = alerts.filter((a) =>
      flagsOf(a).some((f) => f.severity === 'critical' && !ENVIRONMENTAL.has(f.code))).length;

    // Herd split: an animal's worst open alert, else what its latest diagnosis says.
    const split = { critical: 0, high: 0, medium: 0, low: 0, watch: 0, healthy: 0, awaiting: 0 };
    for (const animal of animals) {
      if (animal.open_alerts > 0 && SEVERITY_RANK[animal.worst_severity]) split[animal.worst_severity] += 1;
      else if (!animal.latest_condition) split.awaiting += 1;
      else if (animal.latest_condition === 'healthy') split.healthy += 1;
      else split.watch += 1;
    }
    const segments = [
      { key: 'healthy', label: 'Healthy', value: split.healthy, colour: 'var(--ok)' },
      { key: 'critical', label: 'Critical alert', value: split.critical, colour: 'var(--critical)' },
      { key: 'high', label: 'High alert', value: split.high, colour: 'var(--high)' },
      { key: 'medium', label: 'Medium alert', value: split.medium, colour: 'var(--medium)' },
      { key: 'low', label: 'Low alert', value: split.low, colour: 'var(--low)' },
      { key: 'watch', label: 'Flagged, unconfirmed', value: split.watch, colour: 'var(--text-faint)' },
      { key: 'awaiting', label: 'Awaiting data', value: split.awaiting, colour: 'var(--border)' },
    ];

    const notReporting = herdNow == null ? 0 : animals.filter((a) => {
      const seen = parseTime(a.latest_seen);
      return seen != null && herdNow - seen > 2 * HOUR;
    }).length;
    const awaiting = animals.filter((a) => !a.latest_seen).length;

    const findings = FINDINGS.map(([label, codes]) => {
      let value = 0;
      let worst = 'info';
      for (const alert of alerts) {
        const hits = flagsOf(alert).filter((f) => codes.includes(f.code));
        if (!hits.length) continue;
        value += 1;
        for (const f of hits) if ((FLAG_RANK[f.severity] ?? 0) > FLAG_RANK[worst]) worst = f.severity;
      }
      return { key: label, label, value, colour: FLAG_COLOUR[worst] };
    }).filter((f) => f.value > 0).sort((a, b) => b.value - a.value);

    const speciesSeen = [...new Set(animals.map((a) => a.species))]
      .sort((a, b) => (SPECIES_ORDER.indexOf(a) + 99) - (SPECIES_ORDER.indexOf(b) + 99) || a.localeCompare(b));
    const bySpecies = speciesSeen.map((species) => {
      const members = animals.filter((a) => a.species === species);
      const alerted = members.filter((a) => a.open_alerts > 0);
      const worst = alerted.reduce((w, a) =>
        ((SEVERITY_RANK[a.worst_severity] ?? 0) > (SEVERITY_RANK[w] ?? 0) ? a.worst_severity : w), null);
      return {
        key: species, label: species.charAt(0).toUpperCase() + species.slice(1),
        value: alerted.length, total: members.length,
        colour: worst ? `var(--${worst})` : 'var(--ok)',
      };
    });

    const conditionCounts = new Map();
    for (const a of animals) {
      if (a.latest_condition && a.latest_condition !== 'healthy') {
        conditionCounts.set(a.latest_condition, (conditionCounts.get(a.latest_condition) ?? 0) + 1);
      }
    }
    const conditions = [...conditionCounts.entries()]
      .map(([key, value]) => ({ key, label: conditionLabel(key), value, colour: 'var(--neutral-bar)' }))
      .sort((a, b) => b.value - a.value);

    return {
      herdNow, sickAnimals, newAlerts, bySeverity, criticalVitals, segments,
      notReporting, awaiting, findings, bySpecies, conditions,
    };
  }, [animals, alerts]);

  const totalAlerts = alerts.length;
  const totalAnimals = animals.length;

  return (
    <div className="ov-grid">
      <Card icon={<IconAlert />} title="Health alerts">
        <div className="stat-row">
          <Stat label="Animals under alert" value={model.sickAnimals} unit={model.sickAnimals === 1 ? 'animal' : 'animals'} />
          {model.newAlerts > 0 && (
            <span className="new-badge">{model.newAlerts} new<small>last 24 h of data</small></span>
          )}
        </div>
        <div className="split">
          <div className="bars">
            {model.bySeverity.map((row) => (
              <BarRow key={row.key} label={row.label} value={row.value} total={totalAlerts} colour={row.colour} />
            ))}
          </div>
          <Ring value={model.criticalVitals} label="Critical vitals" alarm={model.criticalVitals > 0} />
        </div>
      </Card>

      <Card icon={<IconHerd />} title="Herd status">
        <div className="donut-wrap">
          <Donut segments={model.segments} total={totalAnimals} />
          <div className="legend">
            {model.segments.filter((s) => s.value > 0).map((s) => (
              <div className="item" key={s.key}>
                <span className="k"><span className="dot" style={{ '--fill': s.colour }} />{s.label}</span>
                <span className="n">{s.value}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="ov-foot">Total {totalAnimals} {totalAnimals === 1 ? 'animal' : 'animals'} monitored</div>
      </Card>

      <Card icon={<IconSignal />} title="Gateway and collars">
        <div className="kv-list">
          <KV label="Not reporting for 2 h or more" value={model.notReporting} alarm={model.notReporting > 0} />
          <KV label="Awaiting first readings" value={model.awaiting} />
          <KV label="Readings stored" value={fmt(summary?.readings)} />
          <KV label="Diagnoses recorded" value={fmt(summary?.diagnoses)} />
        </div>
        <div className="ov-foot">
          Last sync {summary?.last_sync_at ? timeAgo(summary.last_sync_at) : 'never'} · live link {live ? 'connected' : 'reconnecting'}
        </div>
      </Card>

      <Card icon={<IconPulse />} title="Clinical findings">
        {model.findings.length ? (
          <div className="bars">
            {model.findings.map((row) => (
              <BarRow key={row.key} label={row.label} value={row.value} total={totalAlerts} colour={row.colour} />
            ))}
          </div>
        ) : (
          <div className="ov-empty">No clinical thresholds crossed on any open alert.</div>
        )}
        <div className="ov-foot">Open alerts carrying each finding, of {totalAlerts}</div>
      </Card>

      <Card icon={<IconTag />} title="Alerts by species">
        {model.bySpecies.length ? (
          <div className="bars">
            {model.bySpecies.map((row) => (
              <BarRow key={row.key} label={row.label} value={row.value} total={row.total} colour={row.colour} />
            ))}
          </div>
        ) : (
          <div className="ov-empty">No animals enrolled yet.</div>
        )}
        <div className="ov-foot">Animals under alert, of those monitored</div>
      </Card>

      <Card icon={<IconClipboard />} title="Conditions diagnosed">
        {model.conditions.length ? (
          <div className="bars">
            {model.conditions.map((row) => (
              <BarRow key={row.key} label={row.label} value={row.value} total={totalAnimals} colour={row.colour} />
            ))}
          </div>
        ) : (
          <div className="ov-empty">Every animal's latest diagnosis is healthy.</div>
        )}
        <div className="ov-foot">Latest diagnosis per animal, of {totalAnimals}</div>
      </Card>

      <Card icon={<IconUsers />} title="Farmers" wide>
        {farmers.length ? (
          <table className="ftable">
            <thead>
              <tr>
                <th>Farmer</th><th>Phone</th><th>Village</th>
                <th>Animals</th><th>Under alert</th><th>Worst alert</th>
              </tr>
            </thead>
            <tbody>
              {farmers.map((f) => (
                <tr key={f.farmer_id} className={f.farmer_id === ownerFilter ? 'active' : ''}>
                  <td>
                    <button
                      type="button" className="linkish"
                      onClick={() => onPickFarmer?.(f.farmer_id)}
                      title="Filter the alerts and the herd to this farmer"
                    >
                      {f.name}
                    </button>
                  </td>
                  <td>{f.phone ?? '--'}</td>
                  <td>{f.village ?? '--'}</td>
                  <td>{f.animals}</td>
                  <td>{f.animals_under_alert}{f.animals ? ` of ${f.animals}` : ''}</td>
                  <td>
                    {f.worst_severity
                      ? <span className={`pill ${severityClass(f.worst_severity)}`}>{f.worst_severity}</span>
                      : <span className="ok-text">none</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="ov-empty">No farmers registered yet. Open an animal's record and assign one.</div>
        )}
        <div className="ov-foot">
          {ownerFilter ? 'Showing one farmer below; click the name again for everyone' : 'Click a farmer to filter the alerts and the herd below'}
        </div>
      </Card>
    </div>
  );
}

function Card({ icon, title, children, wide }) {
  return (
    <section className={`ov-card ${wide ? 'wide' : ''}`}>
      <div className="ov-head"><span className="ic">{icon}</span><h2>{title}</h2></div>
      {children}
    </section>
  );
}

function Stat({ label, value, unit }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}{unit && <span className="unit">{unit}</span>}</div>
    </div>
  );
}

function KV({ label, value, alarm }) {
  return (
    <div className="kv">
      <div className="k">{label}</div>
      <div className={`v ${alarm ? 'alarm' : ''}`}>{value}</div>
    </div>
  );
}

function BarRow({ label, value, total, colour }) {
  const width = total > 0 ? Math.min(100, (value / total) * 100) : 0;
  return (
    <div className="bar-row">
      <div className="bar-top"><span>{label}</span><b>{value}/{total}</b></div>
      <div className="track" role="img" aria-label={`${label}: ${value} of ${total}`}>
        <span className="fill" style={{ width: `${width}%`, '--fill': colour }} />
      </div>
    </div>
  );
}

function Ring({ value, label, alarm }) {
  return (
    <div className="ring">
      <span>{label}</span>
      <div className={`circle ${alarm ? 'alarm' : ''}`}>{value}</div>
    </div>
  );
}

/** A donut drawn with stroked circles: no chart library needed on the alert screen. */
function Donut({ segments, total, size = 148, thickness = 20 }) {
  const radius = (size - thickness) / 2;
  const circumference = 2 * Math.PI * radius;
  const shown = segments.filter((s) => s.value > 0);
  const gap = shown.length > 1 ? 3 : 0;
  let offset = 0;
  const summary = shown.map((s) => `${s.label} ${s.value}`).join(', ') || 'no animals';

  return (
    <svg className="donut" width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
         aria-label={`Herd status: ${summary}`}>
      <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="var(--surface-2)" strokeWidth={thickness} />
      {total > 0 && shown.map((s) => {
        const length = (s.value / total) * circumference;
        const visible = Math.max(length - gap, 0);
        const element = (
          <circle
            key={s.key} cx={size / 2} cy={size / 2} r={radius} fill="none"
            stroke={s.colour} strokeWidth={thickness}
            strokeDasharray={`${visible} ${circumference - visible}`}
            strokeDashoffset={-offset}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        );
        offset += length;
        return element;
      })}
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central" className="donut-n">{total}</text>
    </svg>
  );
}
