import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import {
  acknowledgeAlert, getAlerts, getAnimals, getSummary, resolveAlert, subscribe,
} from './api.js';
import AlertFeed from './components/AlertFeed.jsx';
import HerdGrid from './components/HerdGrid.jsx';

// The charting library is by far the largest dependency and is only needed once
// someone opens an individual animal. Splitting it keeps the alert screen — the
// one that matters on a slow rural connection — small.
const AnimalDetail = lazy(() => import('./components/AnimalDetail.jsx'));
import { timeAgo } from './format.js';

// Whoever is signed in acknowledges alerts under this name. A real deployment
// would take it from an auth session; the project scope stops short of that.
const OPERATOR = 'demo-vet';

// The live stream carries alerts, so polling only needs to catch counters and
// anything that changed while the page was backgrounded.
const POLL_MS = 20000;

export default function App() {
  const [tab, setTab] = useState('overview');
  const [summary, setSummary] = useState(null);
  const [animals, setAnimals] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [selected, setSelected] = useState(null);
  const [live, setLive] = useState(false);
  const [error, setError] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  // Alerts that arrived over the live stream, so they can be highlighted once.
  const [freshIds, setFreshIds] = useState(() => new Set());
  const freshTimers = useRef(new Map());

  const load = useCallback(async () => {
    try {
      const [summaryData, animalData, alertData] = await Promise.all([
        getSummary(), getAnimals(), getAlerts('open'),
      ]);
      setSummary(summaryData);
      setAnimals(animalData.animals);
      setAlerts(alertData.alerts);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const markFresh = useCallback((alertId) => {
    setFreshIds((previous) => new Set(previous).add(alertId));
    clearTimeout(freshTimers.current.get(alertId));
    const timer = setTimeout(() => {
      setFreshIds((previous) => {
        const next = new Set(previous);
        next.delete(alertId);
        return next;
      });
      freshTimers.current.delete(alertId);
    }, 6000);
    freshTimers.current.set(alertId, timer);
  }, []);

  useEffect(() => {
    const unsubscribe = subscribe({
      onStatus: setLive,
      onAlert: (alert) => {
        markFresh(alert.alert_id);
        setAlerts((previous) => {
          // The gateway re-sends an alert whenever it escalates, so replace in
          // place rather than accumulating duplicates of the same alert_id.
          const rest = previous.filter((a) => a.alert_id !== alert.alert_id);
          return alert.status === 'open' ? [alert, ...rest] : rest;
        });
      },
      onAlertUpdated: () => load(),
      onDiagnosis: () => setRefreshKey((k) => k + 1),
    });

    const timers = freshTimers.current;
    return () => {
      unsubscribe();
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, [load, markFresh]);

  const handleAcknowledge = useCallback(async (id) => {
    await acknowledgeAlert(id, OPERATOR);
    await load();
  }, [load]);

  const handleResolve = useCallback(async (id) => {
    await resolveAlert(id);
    setAlerts((previous) => previous.filter((a) => a.alert_id !== id));
    await load();
  }, [load]);

  const openAnimal = useCallback((animalId) => {
    setSelected(animalId);
    setTab('herd');
  }, []);

  const critical = alerts.filter((a) => a.severity === 'critical').length;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>Vetra</h1>
          <span>Smart Farming Animal Dx</span>
        </div>

        <nav className="tabs" role="tablist">
          {[['overview', 'Overview'], ['herd', 'Herd']].map(([key, label]) => (
            <button
              key={key} role="tab" aria-selected={tab === key}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </nav>

        <span className={`live ${live ? 'on' : ''}`} title={live ? 'Receiving live updates' : 'Reconnecting'}>
          <span className="dot" />
          {live ? 'Live' : 'Offline'}
        </span>
      </header>

      <main>
        {error && <div className="banner">Cannot reach the cloud service: {error}</div>}

        <div className="tiles">
          <Tile label="Animals monitored" value={summary?.animals ?? '--'} />
          <Tile
            label="Open alerts" value={alerts.length}
            sub={critical ? `${critical} critical` : 'none critical'}
            alarm={critical > 0}
          />
          <Tile label="Diagnoses recorded" value={fmt(summary?.diagnoses)} />
          <Tile label="Readings stored" value={fmt(summary?.readings)} />
          <Tile label="Last sync" value={summary?.last_sync_at ? timeAgo(summary.last_sync_at) : '--'}
                sub="from farm gateway" />
        </div>

        {tab === 'overview' ? (
          <div className="columns">
            <section className="card">
              <div className="card-head">
                <h2>Active alerts</h2>
                <span className="hint">grouped by animal, worst first</span>
              </div>
              <AlertFeed
                alerts={alerts} freshIds={freshIds}
                onAcknowledge={handleAcknowledge} onResolve={handleResolve}
                onSelectAnimal={openAnimal}
              />
            </section>

            <section className="card">
              <div className="card-head">
                <h2>Herd status</h2>
                <span className="hint">{animals.length} animals</span>
              </div>
              <div className="card-body">
                <HerdGrid animals={animals} selectedId={selected} onSelect={openAnimal} />
              </div>
            </section>
          </div>
        ) : (
          <div className="columns">
            <section className="card">
              <div className="card-head">
                <h2>Animal record</h2>
                <span className="hint">{selected ?? 'none selected'}</span>
              </div>
              <Suspense fallback={<div className="card-body"><div className="skeleton" style={{ height: 260 }} /></div>}>
                <AnimalDetail animalId={selected} refreshKey={refreshKey} />
              </Suspense>
            </section>

            <section className="card">
              <div className="card-head"><h2>Herd</h2></div>
              <div className="card-body">
                <HerdGrid animals={animals} selectedId={selected} onSelect={setSelected} />
              </div>
            </section>
          </div>
        )}
      </main>
    </div>
  );
}

const fmt = (n) => (n == null ? '--' : Number(n).toLocaleString());

function Tile({ label, value, sub, alarm }) {
  return (
    <div className={`tile ${alarm ? 'alarm' : ''}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}
