import { useState } from 'react';
import { assignAnimal, saveFarmer } from '../api.js';

/**
 * Who this animal belongs to, and the control to change it.
 *
 * Ownership is recorded on the cloud at the farm office: a collar carries an
 * animal id, never a person. Assigning here is what lets an alert say whose
 * animal is sick and which number to call.
 */
export default function OwnerPanel({ animal, farmers, onChanged }) {
  const [open, setOpen] = useState(false);
  const [owner, setOwner] = useState(animal.owner ?? '');
  const [name, setName] = useState(animal.name ?? '');
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({ name: '', phone: '', village: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let farmerId = owner || null;
      if (creating) {
        if (!draft.name.trim()) throw new Error('the farmer needs a name');
        const { farmer } = await saveFarmer({
          name: draft.name, phone: draft.phone || null, village: draft.village || null,
        });
        farmerId = farmer.farmer_id;
      }
      await assignAnimal(animal.animal_id, { owner: farmerId, name: name.trim() || null });
      setOpen(false);
      setCreating(false);
      setDraft({ name: '', phone: '', village: '' });
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="owner-box">
      <div className="owner-line">
        <div>
          <div className="k">Belongs to</div>
          {animal.farmer_name ? (
            <div className="v">
              <strong>{animal.farmer_name}</strong>
              {animal.farmer_phone && <span> · {animal.farmer_phone}</span>}
              {animal.farmer_village && <span> · {animal.farmer_village}</span>}
            </div>
          ) : (
            <div className="v none">No farmer assigned</div>
          )}
        </div>
        <button type="button" className="btn no-print" onClick={() => setOpen((o) => !o)}>
          {open ? 'Cancel' : animal.farmer_name ? 'Change' : 'Assign farmer'}
        </button>
      </div>

      {open && (
        <form className="owner-form no-print" onSubmit={submit}>
          {error && <div className="banner">{error}</div>}
          <label className="field">
            <span>Animal name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="optional, e.g. Ganga" />
          </label>
          {!creating ? (
            <label className="field">
              <span>Farmer</span>
              <select value={owner} onChange={(e) => setOwner(e.target.value)}>
                <option value="">Unassigned</option>
                {farmers.map((f) => (
                  <option key={f.farmer_id} value={f.farmer_id}>
                    {f.name}{f.village ? ` (${f.village})` : ''}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div className="field-grid">
              <label className="field">
                <span>Farmer name</span>
                <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} required />
              </label>
              <label className="field">
                <span>Phone</span>
                <input value={draft.phone} onChange={(e) => setDraft({ ...draft, phone: e.target.value })} placeholder="+91 …" />
              </label>
              <label className="field">
                <span>Village</span>
                <input value={draft.village} onChange={(e) => setDraft({ ...draft, village: e.target.value })} />
              </label>
            </div>
          )}
          <div className="form-actions">
            <button type="submit" className="btn primary" disabled={busy}>Save</button>
            <button type="button" className="btn" onClick={() => setCreating((c) => !c)}>
              {creating ? 'Pick an existing farmer' : 'New farmer…'}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
