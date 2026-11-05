import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  acknowledgeAlert, getAlerts, getAnimals, getFarmers, getSummary, resolveAlert, subscribe,
} from './api.js';
import AlertFeed from './components/AlertFeed.jsx';
import HerdGrid from './components/HerdGrid.jsx';
import Overview from './components/Overview.jsx';
import SymptomCheck from './components/SymptomCheck.jsx';
import { IconMoon, IconSun } from './components/icons.jsx';
import { timeAgo } from './format.js';

// The charting library is by far the largest dependency and is only needed once
// someone opens an individual animal. Splitting it keeps the alert screen — the
// one that matters on a slow rural connection — small. The report is split for
// the same reason: it is opened rarely and printed, not watched.
const AnimalDetail = lazy(() => import('./components/AnimalDetail.jsx'));
const AnimalReport = lazy(() => import('./components/AnimalReport.jsx'));

// Whoever is signed in acknowledges alerts under this name. A real deployment
// would take it from an auth session; the project scope stops short of that.
const OPERATOR = 'demo-vet';

// The live stream carries alerts, so polling only needs to catch counters and
// anything that changed while the page was backgrounded.
const POLL_MS = 20000;

// Light is the default look; the choice persists per browser. Storage can be
// unavailable (private windows, blocked site data), so every access is guarded.
const THEME_KEY = 'vetra-theme';
const readTheme = () => {
  try { return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'; } catch { return 'light'; }
};

export default function App() {
  const [tab, setTab] = useState('overview');
  const [summary, setSummary] = useState(null);
  const [animals, setAnimals] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [farmers, setFarmers] = useState([]);
  const [ownerFilter, setOwnerFilter] = useState('');
  const [selected, setSelected] = useState(null);
  const [reportFor, setReportFor] = useState(null);
  const [live, setLive] = useState(false);
  const [error, setError] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [theme, setTheme] = useState(readTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* per-viewer convenience only */ }
  }, [theme]);

  // Alerts that arrived over the live stream, so they can be highlighted once.
  const [freshIds, setFreshIds] = useState(() => new Set());
  const freshTimers = useRef(new Map());

  const load = useCallback(async () => {
    try {
      const [summaryData, animalData, alertData, farmerData] = await Promise.all([
        getSummary(), getAnimals(), getAlerts('active'),
        // An older cloud service without the registry must not blank the page.
        getFarmers().catch(() => ({ farmers: [] })),
      ]);
      setSummary(summaryData);
      setAnimals(animalData.animals);
      setAlerts(alertData.alerts);
      setFarmers(farmerData.farmers);
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
          return (alert.status === 'open' || alert.status === 'acknowledged') ? [alert, ...rest] : rest;
        });
      },
      onAlertUpdated: () => load(),
      onAnimalUpdated: () => load(),
      onDiagnosis: () => setRefreshKey((k) => k + 1),
    });

    const timers = freshTimers.current;
    return () => {
      unsubscribe();
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, [load, markFresh]);

  // Reload after every attempt, successful or not: on a conflict the list is
  // exactly what is stale, and the error itself is shown by the feed.
  const handleAcknowledge = useCallback(async (id) => {
    try {
      await acknowledgeAlert(id, OPERATOR);
    } finally {
      await load();
    }
  }, [load]);

  const handleResolve = useCallback(async (id) => {
    try {
      await resolveAlert(id);
      setAlerts((previous) => previous.filter((a) => a.alert_id !== id));
    } finally {
      await load();
    }
  }, [load]);

  const openAnimal = useCallback((animalId) => {
    setSelected(animalId);
    setReportFor(null);
    setTab('herd');
  }, []);

  const switchTab = (key) => {
    setTab(key);
    setReportFor(null);
  };

  // The farmer filter narrows the feed and the herd; the overview cards stay
  // herd-wide, since that is what "the herd at a glance" means.
  const animalsById = useMemo(() => new Map(animals.map((a) => [a.animal_id, a])), [animals]);
  const visibleAnimals = useMemo(
    () => (ownerFilter ? animals.filter((a) => a.owner === ownerFilter) : animals),
    [animals, ownerFilter],
  );
  const visibleAlerts = useMemo(
    () => (ownerFilter
      ? alerts.filter((a) => animalsById.get(a.animal_id)?.owner === ownerFilter)
      : alerts),
    [alerts, animalsById, ownerFilter],
  );

  const critical = alerts.filter((a) => a.severity === 'critical').length;

  const filterBar = (
    <FarmerFilter
      farmers={farmers} value={ownerFilter} onChange={setOwnerFilter}
      animals={visibleAnimals.length} alerts={visibleAlerts.length}
    />
  );

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>Vetra</h1>
          <span>Smart Farming Animal Dx</span>
        </div>

        <nav className="tabs" role="tablist">
          {[['overview', 'Overview'], ['herd', 'Herd'], ['symptoms', 'Symptom check']].map(([key, label]) => (
            <button
              key={key} role="tab" aria-selected={tab === key && !reportFor}
              onClick={() => switchTab(key)}
            >
              {label}
            </button>
          ))}
        </nav>

        <span className={`live ${live ? 'on' : ''}`} title={live ? 'Receiving live updates' : 'Reconnecting'}>
          <span className="dot" />
          {live ? 'Live' : 'Offline'}
        </span>

        <button
          type="button" className="theme-btn"
          onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
          title={theme === 'dark' ? 'Switch to the light theme' : 'Switch to the dark theme'}
        >
          {theme === 'dark' ? <IconSun /> : <IconMoon />}
          {theme === 'dark' ? 'Light' : 'Dark'}
        </button>
      </header>

      <main>
        {error && <div className="banner">Cannot reach the cloud service: {error}</div>}

        {tab !== 'overview' && !reportFor && <div className="tiles">
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
        </div>}

        {reportFor ? (
          <Suspense fallback={<div className="card"><div className="card-body"><div className="skeleton" style={{ height: 320 }} /></div></div>}>
            <AnimalReport animalId={reportFor} onClose={() => setReportFor(null)} />
          </Suspense>
        ) : tab === 'symptoms' ? (
          <SymptomCheck />
        ) : tab === 'overview' ? (
          <>
          <Overview
            summary={summary} animals={animals} alerts={alerts} live={live}
            farmers={farmers} ownerFilter={ownerFilter}
            onPickFarmer={(id) => setOwnerFilter((current) => (current === id ? '' : id))}
          />
          {filterBar}
          <div className="columns">
            <section className="card">
              <div className="card-head">
                <h2>Active alerts</h2>
                <span className="hint">grouped by animal, worst first</span>
              </div>
              <AlertFeed
                alerts={visibleAlerts} freshIds={freshIds} animalsById={animalsById}
                onAcknowledge={handleAcknowledge} onResolve={handleResolve}
                onSelectAnimal={openAnimal}
              />
            </section>

            <section className="card">
              <div className="card-head">
                <h2>Animals</h2>
                <span className="hint">{visibleAnimals.length} animals</span>
              </div>
              <div className="card-body">
                <HerdGrid animals={visibleAnimals} selectedId={selected} onSelect={openAnimal} />
              </div>
            </section>
          </div>
          </>
        ) : (
          <>
          {filterBar}
          <div className="columns">
            <section className="card">
              <div className="card-head">
                <h2>Animal record</h2>
                <span className="hint">{selected ?? 'none selected'}</span>
              </div>
              <Suspense fallback={<div className="card-body"><div className="skeleton" style={{ height: 260 }} /></div>}>
                <AnimalDetail
                  animalId={selected} refreshKey={refreshKey} farmers={farmers}
                  onChanged={load} onReport={setReportFor}
                />
              </Suspense>
            </section>

            <section className="card">
              <div className="card-head"><h2>Herd</h2><span className="hint">{visibleAnimals.length} animals</span></div>
              <div className="card-body">
                <HerdGrid animals={visibleAnimals} selectedId={selected} onSelect={setSelected} />
              </div>
            </section>
          </div>
          </>
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

function FarmerFilter({ farmers, value, onChange, animals, alerts }) {
  if (!farmers.length) return null;
  return (
    <div className="filter-bar no-print">
      <label>
        <span>Farmer</span>
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">All farmers</option>
          {farmers.map((f) => (
            <option key={f.farmer_id} value={f.farmer_id}>
              {f.name}{f.village ? ` · ${f.village}` : ''} ({f.animals})
            </option>
          ))}
        </select>
      </label>
      {value && <button type="button" className="btn" onClick={() => onChange('')}>Show all</button>}
      <span className="hint">{animals} animals · {alerts} active alerts</span>
    </div>
  );
}
