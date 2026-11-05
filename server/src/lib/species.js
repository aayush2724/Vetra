/**
 * Species reference ranges, mirrored from ml/vetra_ml/synth/species.py and
 * dashboard/src/format.js. The report uses them to say how much of an animal's
 * history sat outside its own species' normal band.
 */
export const NORMAL_RANGE = {
  cattle:  { heart_rate: [48, 84],  body_temperature: [38.0, 39.3], respiratory_rate: [26, 50], spo2: [95, 100] },
  buffalo: { heart_rate: [40, 60],  body_temperature: [37.2, 38.6], respiratory_rate: [12, 36], spo2: [95, 100] },
  goat:    { heart_rate: [70, 110], body_temperature: [38.6, 40.0], respiratory_rate: [15, 30], spo2: [95, 100] },
  sheep:   { heart_rate: [70, 90],  body_temperature: [38.3, 39.9], respiratory_rate: [16, 34], spo2: [95, 100] },
};

/** The channels a report summarises, in the order a vet reads them. */
export const REPORT_CHANNELS = [
  { key: 'heart_rate', label: 'Heart rate', unit: 'bpm', digits: 0 },
  { key: 'body_temperature', label: 'Body temperature', unit: '°C', digits: 1 },
  { key: 'respiratory_rate', label: 'Respiratory rate', unit: '/min', digits: 0 },
  { key: 'spo2', label: 'Oxygen saturation', unit: '%', digits: 0 },
  { key: 'activity_index', label: 'Activity index', unit: '', digits: 0 },
  { key: 'rumination_min', label: 'Rumination', unit: 'fraction', digits: 2 },
  { key: 'step_regularity', label: 'Gait rhythm', unit: '', digits: 2 },
  { key: 'distance_m', label: 'Distance per minute', unit: 'm', digits: 1 },
];
