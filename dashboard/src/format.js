/** Presentation helpers shared across components. */

export const SEVERITY_CLASS = {
  critical: 'sev-critical',
  high: 'sev-high',
  medium: 'sev-medium',
  low: 'sev-low',
};

export const severityClass = (severity) => SEVERITY_CLASS[severity] ?? 'sev-low';

export const conditionClass = (condition) =>
  condition === 'healthy' || !condition ? 'sev-ok' : 'sev-medium';

const TITLES = {
  healthy: 'Healthy',
  cardiac_disorder: 'Cardiac disorder',
  respiratory_disease: 'Respiratory disease',
  obesity: 'Obesity',
  diabetes: 'Diabetes / metabolic',
  heat_stress: 'Heat stress',
  infectious_disease: 'Infectious disease',
  mobility_disorder: 'Mobility disorder',
};

export const conditionLabel = (condition) =>
  TITLES[condition] ?? String(condition ?? 'unknown').replace(/_/g, ' ');

/** "3 min ago" — a farmer cares that a reading is stale, not its exact clock time. */
export function timeAgo(iso) {
  if (!iso) return 'never';
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return 'unknown';
  if (seconds < 60) return 'just now';
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.round(hours)} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export const clockTime = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? '--'
    : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
};

export const pct = (value) => `${Math.round((Number(value) || 0) * 100)}%`;

/** Species reference ranges, mirrored from ml/vetra_ml/synth/species.py. */
export const NORMAL_RANGE = {
  cattle:  { heart_rate: [48, 84],  body_temperature: [38.0, 39.3], respiratory_rate: [26, 50], spo2: [95, 100] },
  buffalo: { heart_rate: [40, 60],  body_temperature: [37.2, 38.6], respiratory_rate: [12, 36], spo2: [95, 100] },
  goat:    { heart_rate: [70, 110], body_temperature: [38.6, 40.0], respiratory_rate: [15, 30], spo2: [95, 100] },
  sheep:   { heart_rate: [70, 90],  body_temperature: [38.3, 39.9], respiratory_rate: [16, 34], spo2: [95, 100] },
};

export function isOutOfRange(species, channel, value) {
  const range = NORMAL_RANGE[species]?.[channel];
  if (!range || value == null) return false;
  return value < range[0] || value > range[1];
}
