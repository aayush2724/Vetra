# Vetra — Smart Farming Animal Dx

AI-based continuous health monitoring and early disease diagnosis for farm
animals, built to work where the network does not.

Physiological and behavioural telemetry (heart rate, body temperature,
respiratory rate, SpO₂, activity, rumination, GPS and gait) is ingested over
MQTT or REST, diagnosed **on the edge device with no internet connection**,
turned into alerts a farmer will actually read, and synchronised to the cloud
whenever a link happens to exist.

---

## Results

Measured on **48 animals never seen during training** (the split is grouped by
animal — see [Honest limitations](#honest-limitations)).

### Diagnosis

| | Classical baseline | Edge neural network |
|---|---|---|
| Test macro-F1 | 0.986 | 0.981 |
| Screening recall | 0.985 | 0.977 |
| False-alarm rate | 0.008 | 0.009 |
| Model size | 7 KB | **12.2 KB** |

Eight classes: healthy, cardiac disorder, respiratory disease, obesity,
diabetes, heat stress, infectious disease, mobility disorder.

**Early detection** — recall on the neural network by how far the disease had
progressed. The premise of the project is catching illness while it is still
mild, so this matters more than the headline number:

| Severity band | Recall |
|---|---|
| Early (0.15–0.35) | 0.884 |
| Mild (0.35–0.55) | 0.993 |
| Marked (0.55–0.75) | 0.967 |
| Severe (0.75–1.00) | 0.984 |

### Edge deployment

TFLite variants are all measured against the float model, and any that disagrees
by more than 1% is rejected automatically:

| Variant | Size | Agreement with float | Latency | Selected |
|---|---|---|---|---|
| float32 | 29.9 KB | 1.0000 | 0.0066 ms | |
| dynamic int8 | **12.2 KB** | 0.9997 | **0.0064 ms** | ✅ |
| full int8 | 13.0 KB | 0.4599 | 0.0076 ms | rejected |

Full-int8 collapsed to 0.08 macro-F1 and the agreement gate caught it — see
[docs/architecture.md](docs/architecture.md) for why.

End-to-end throughput of the full streaming engine — validate, window, extract
69 features, infer — measured at **~5,000 readings/second single-threaded** on a
development laptop. A Raspberry-Pi-class board will be substantially slower, and
this has not been measured on real hardware; the figure is here as a bound on
the software, not a deployment claim.

### Alerts

The measurement that decides whether anyone keeps using the system
(`make evaluate`, 48 held-out animals over 240 animal-days):

| | Alert on every abnormal window | With the alert policy |
|---|---|---|
| Alerts raised | 6,200 | **52** |
| Per animal-day | 25.83 | **0.22** |
| Sick animals caught | 35/35 | **35/35** |
| Healthy animals alerted | — | **0/13** |
| Median time from onset | — | **1.5 h** |

**A 99.2% reduction in alert volume with no loss of sensitivity.**

---

## Honest limitations

Read this before quoting any number above.

**The training data is simulated.** No public dataset of continuous, labelled,
multi-channel vitals for Indian smallholder livestock was available. Data is
generated from published veterinary reference ranges and documented disease
presentations, all sourced in
[docs/physiology-references.md](docs/physiology-references.md).

**So the accuracy figures measure the pipeline, not clinical performance.** Real
animals present with comorbidities, sensor dropout, individual quirks and
diseases outside these eight classes. That a *logistic regression* scores 0.986
is itself the clearest evidence the simulated signatures are cleaner than
reality — on real data the tree and neural models would be expected to pull
ahead, and every number would be lower.

What the results do establish is that the pipeline is sound end to end: the
split does not leak, the features carry the signal, the model fits in 12 KB and
runs offline, and the alert policy suppresses noise without losing sick animals.

**Swapping in real data changes one file.** Replace
`ml/vetra_ml/synth/generator.py` with a loader emitting the same columns
(`ml/vetra_ml/schema.py` defines them). Nothing else moves.

---

## Quick start

Requires **Python 3.11** (TensorFlow has no wheels for 3.13+) and **Node 20+**.

```bash
make setup     # virtualenv + Python and Node dependencies
make data      # simulate 240 animals over 5 days  (~2 min)
make train     # classical baselines, neural network, TFLite export  (~3 min)
make demo      # run the whole system and stream a herd through it
```

Then open **http://localhost:4000**.

`make demo` starts the cloud service, the edge gateway and a collar simulator,
streams three simulated days through the live pipeline in about half a minute,
and prints the alerts it raised. `make stop` shuts everything down.

```bash
make evaluate  # alert-policy measurement on held-out animals
make test      # 70 Python tests + 18 Node tests
make help      # everything else
```

### Running the pieces separately

```bash
make server                        # cloud service on :4000
make gateway                       # edge gateway on :5001
make simulate                      # stream collar data into the gateway
make dashboard                     # dashboard dev server on :5173 (hot reload)
```

The gateway also accepts MQTT. Install a broker (`sudo dnf install mosquitto`,
`sudo apt install mosquitto`), start it, then:

```bash
.venv/bin/python edge/agent.py --mqtt-host localhost      # subscribes vetra/telemetry/+
.venv/bin/python edge/simulator.py --transport mqtt
```

Without a broker the gateway logs that MQTT is unavailable and keeps serving
REST, so nothing in the demo depends on it.

---

## How it works

```
 collars ──MQTT/REST──►  EDGE GATEWAY  ────────────► CLOUD ────► DASHBOARD
                         validate                    records      alerts
                         store (SQLite)              API          charts
                         60-min rolling window       SSE push     herd view
                         TFLite model  (offline)
                         clinical rules
                         alert policy
                         sync when online
```

**Every diagnosis happens on the device.** The cloud is only somewhere to copy
records to. If the link is down for a week the farm keeps being monitored and
the upload queue simply grows — the property that makes the system usable where
connectivity is intermittent.

Four design decisions do most of the work:

1. **The split is grouped by animal.** Windows overlap 50%, so a random split
   would put near-identical rows on both sides and inflate every metric.
2. **Features are both species-normalised and self-relative.** Absolutes catch
   chronic conditions present at enrollment; deviation from the animal's own
   24-hour baseline catches acute change. Either alone misses half the classes.
3. **Training and the device share one feature implementation.** Two copies
   would drift, and drift shows up as a model that quietly underperforms in the
   field. A test pins the two paths to identical output.
4. **Clinical rules run alongside the model.** Textbook thresholds fire
   regardless of what the model predicts, and they are what the alert shows the
   vet: "SpO₂ 88%, respiratory rate 64/min", not "p=0.94".

Full reasoning in [docs/architecture.md](docs/architecture.md).

---

## Repository layout

```
ml/vetra_ml/          data pipeline, models, offline inference engine
  synth/                physiology-grounded herd simulator
  inference/            edge engine + clinical rules
edge/                 gateway: ingest, store, alert, sync, simulator
server/               Node.js cloud service (records API + live push)
dashboard/            React dashboard
tests/                Python test suite
scripts/              alert evaluation, demo runner
docs/                 architecture and physiology references
artifacts/            trained models and evaluation reports (generated)
```

## Objectives

| # | Objective | Where | Status |
|---|---|---|---|
| 1 | Acquisition and processing pipeline | `schema.py`, `synth/`, `features.py` | ✅ |
| 2 | Disease prediction models | `train_sklearn.py`, `train_keras.py` | ✅ 0.986 macro-F1 |
| 3 | Lightweight offline edge models | `export_tflite.py`, `inference/` | ✅ 12.2 KB, 0.006 ms |
| 4 | Intelligent alert generation | `edge/alerts.py` | ✅ 99.2% noise reduction |
| 5 | Health records + cloud sync | `edge/store.py`, `edge/sync.py`, `server/` | ✅ |
| 6 | Dashboard | `dashboard/` | ✅ |

## Technology

Python 3.11 · TensorFlow 2.21 / TFLite · ai-edge-litert · scikit-learn · pandas ·
NumPy · SQLite · MQTT (paho) · REST · Node.js 20 · Express · better-sqlite3 ·
React 18 · Vite · Recharts

## Known gaps

Things a production deployment would need that this project does not do:

- Real sensor data (see [Honest limitations](#honest-limitations)).
- Per-gateway credentials and authenticated dashboard sessions rather than a
  shared API key and a hardcoded operator name.
- TLS on both network hops, and MQTT ACLs restricting each collar to its topic.
- Comorbidity handling — an animal can currently hold two open alerts, which is
  right for genuine comorbidity but is also how model flip-flop would present.
- Model drift monitoring and a retraining path once real data accumulates.
