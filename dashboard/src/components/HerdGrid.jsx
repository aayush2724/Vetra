import { conditionClass, conditionLabel, severityClass, timeAgo } from '../format.js';

/**
 * The herd at a glance. Each animal shows its most recent diagnosis, coloured
 * by its worst open alert so a farmer can scan for trouble without reading.
 */
export default function HerdGrid({ animals, selectedId, onSelect }) {
  if (!animals.length) {
    return (
      <div className="empty">
        <strong>No animals yet</strong>
        Start the edge gateway and the collar simulator to populate the herd.
      </div>
    );
  }

  return (
    <div className="herd">
      {animals.map((animal) => {
        const severity = animal.worst_severity
          ? severityClass(animal.worst_severity)
          : conditionClass(animal.latest_condition);

        return (
          <button
            key={animal.animal_id}
            className={`animal ${severity}`}
            aria-pressed={animal.animal_id === selectedId}
            onClick={() => onSelect(animal.animal_id)}
          >
            <div className="row">
              <span className="id">{animal.animal_id}</span>
              <span className="species">{animal.species}</span>
            </div>

            <div className="cond">
              {animal.latest_condition ? conditionLabel(animal.latest_condition) : 'Awaiting data'}
            </div>

            <div className="row">
              <span className="when">
                {animal.latest_seen ? `seen ${timeAgo(animal.latest_seen)}` : 'no readings'}
              </span>
              {animal.open_alerts > 0 && (
                <span className="pill">
                  {animal.open_alerts} {animal.open_alerts === 1 ? 'alert' : 'alerts'}
                </span>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}
