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
| Test macro-F1 | 0.985 | 0.981 |
| Screening recall | 0.985 | 0.978 |
| False-alarm rate | 0.008 | 0.010 |
| Model size | 7 KB | **12.2 KB** |

Eight classes: healthy, cardiac disorder, respiratory disease, obesity,
diabetes, heat stress, infectious disease, mobility disorder.

**Early detection** — recall on the neural network by how far the disease had
progressed. The premise of the project is catching illness while it is still
mild, so this matters more than the headline number:

| Severity band | Recall |
|---|---|
| Early (0.15–0.35) | 0.873 |
| Mild (0.35–0.55) | 0.993 |
| Marked (0.55–0.75) | 0.970 |
| Severe (0.75–1.00) | 0.983 |

### Edge deployment

TFLite variants are all measured against the float model, and any that disagrees
by more than 1% is rejected automatically:

| Variant | Size | Agreement with float | Latency | Selected |
|---|---|---|---|---|
| float32 | 29.9 KB | 1.0000 | 0.013 ms | |
| dynamic int8 | **12.2 KB** | 0.9995 | **0.010 ms** | ✅ |
| full int8 | 13.0 KB | 0.1240 | 0.013 ms | rejected |

Full-int8 collapsed to 0.03 macro-F1 and the agreement gate caught it — see
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
| Alerts raised | 6,207 | **52** |
| Per animal-day | 25.86 | **0.22** |
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
diseases outside these eight classes. That a *logistic regression* scores 0.985
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

Requires **Python 3.11** (TensorFlow has no wheels for 3.13+) and **Node 20+**,
or just Docker — see [Running with Docker](#running-with-docker).

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
make test      # 113 Python tests + 35 Node tests
make help      # everything else
```

### Running with Docker

The same system runs as containers, with nothing installed locally but Docker:

```bash
make docker-train   # fresh clone only: build the dataset and train the models (~10 min)
make docker-demo    # build the images, start the cloud service and the gateway, stream a herd
make docker-down    # stop everything; the databases stay in their Docker volumes
```

Then open **http://localhost:4000**, as before.

| Service | Image | What it runs |
|---|---|---|
| `cloud` | `vetra/cloud` | the Node.js API with the built dashboard, on :4000 |
| `gateway` | `vetra/edge` | the edge agent with the TFLite runtime (no TensorFlow), on :5001 |
| `simulator` | `vetra/edge` | the collar simulator — profile `demo`, runs once and exits |
| `trainer` | `vetra/ml` | the ML pipeline — profile `ml`, runs once and exits |
| `mosquitto` | `eclipse-mosquitto:2` | an MQTT broker — profile `mqtt` |

The trained model is not baked into the gateway image. The container reads it
from `./artifacts` through a bind mount, which is also where the trainer writes,
so a retrained model is picked up on the next restart without rebuilding
anything; until a model exists the gateway waits and says so. The cloud and
edge databases live in the Docker volumes `vetra_cloud-db` and `vetra_edge-db`,
so records survive `make docker-down`; `make docker-clean` deletes them.

Settings — the sync API key, the demo herd size, host ports — come from a
`.env` file next to `docker-compose.yml`; copy `.env.example` to start. The
bind mounts carry Docker's `z` flag so that SELinux hosts (Fedora, RHEL) let
the containers read `./artifacts` and `./data`; other hosts ignore it.
`make docker-up` starts just the cloud service and the gateway, ready for real
collars to post to `http://<host>:5001/ingest`. `make docker-test` runs both
test suites inside the containers.

To stream over MQTT instead of REST, start the broker profile and point the
simulator at it:

```bash
docker compose --profile mqtt up -d
docker compose --profile demo --profile mqtt run --rm simulator \
  python edge/simulator.py --transport mqtt --mqtt-host mosquitto --animals 12 --days 3 --speed 40000
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

### Farmers and animal reports

Collars carry an animal id, never a person, so ownership is recorded on the
cloud: the dashboard keeps a registry of farmers (name, phone, village) and
each animal is assigned to one from its record. The herd grid, the alert feed
and the animal record then show whose animal is in trouble and which number to
call, the Overview has a farmers card with animals under alert per farmer, and
the feed and herd can be filtered to one farmer. A gateway sync never touches
ownership, so a re-sent animal keeps its farmer and name.

`make demo` registers four demonstration farmers and shares the herd between
them (`server/src/assign-farmers.js`, which only talks to the cloud API).

Every animal has a **Full report** button on its record: identity and owner,
the span of readings held, each vital's mean, range and share of time outside
the species band, the diagnosis history as runs of one condition, and every
alert ever raised, resolved ones included. Print it from the browser for a PDF
or download the JSON it was built from (`GET /api/animals/:id/report`).

### Symptom check

The dashboard's **Symptom check** tab diagnoses from drop-downs instead of
sensor data: pick the species, the weather, when the signs started and what the
animal shows (temperature, heart rate, breathing, oxygen level, activity, cud
chewing, walking). It needs the cloud service and the gateway running.

There is no second model. The gateway (`ml/vetra_ml/symptoms.py`) turns each
selection into the vital-sign change it describes — "high fever" is about +1.8 °C,
"panting" about +30 breaths/min, the same magnitudes as the disease signatures —
renders a day of telemetry for five simulated animals of that species, and runs
it through the unchanged diagnosis engine and clinical rules. The answer shows
the diagnosis, how many of the five animals agree, the clinical findings, what to
do next and the readings the model was given. A textbook presentation of each of
the eight conditions is diagnosed correctly (`tests/test_symptoms.py`).

```bash
curl -X POST localhost:4000/api/diagnose -H 'Content-Type: application/json' \
  -d '{"species":"cattle","weather":"hot","onset":"recent",
       "symptoms":{"breathing":"panting","rumination":"stopped"}}'
```

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
   field. A test runs the streaming engine over a day of readings and pins every
   window it diagnoses to the batch builder's output, bit for bit.
4. **Clinical rules run alongside the model.** Textbook thresholds fire
   regardless of what the model predicts, and they are what the alert shows the
   vet: "SpO₂ 88%, respiratory rate 64/min", not "p=0.94". A critical finding
   the model has no class for, such as hypothermia, still opens a
   "critical vital signs" alert on its own.

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
docker/               Dockerfiles and container helpers (docker-compose.yml is at the root)
docs/                 architecture and physiology references
artifacts/            trained models and evaluation reports (generated)
```

## Objectives

| # | Objective | Where | Status |
|---|---|---|---|
| 1 | Acquisition and processing pipeline | `schema.py`, `synth/`, `features.py` | ✅ |
| 2 | Disease prediction models | `train_sklearn.py`, `train_keras.py` | ✅ 0.985 macro-F1 |
| 3 | Lightweight offline edge models | `export_tflite.py`, `inference/` | ✅ 12.2 KB, 0.010 ms |
| 4 | Intelligent alert generation | `edge/alerts.py` | ✅ 99.2% noise reduction |
| 5 | Health records + cloud sync | `edge/store.py`, `edge/sync.py`, `server/` | ✅ |
| 6 | Dashboard | `dashboard/` | ✅ |

## Technology

Python 3.11 · TensorFlow 2.21 / TFLite · ai-edge-litert · scikit-learn · pandas ·
NumPy · SQLite · MQTT (paho) · REST · Node.js 20 · Express · better-sqlite3 ·
React 18 · Vite · Recharts · Docker Compose

## Known gaps

Things a production deployment would need that this project does not do:

- Real sensor data (see [Honest limitations](#honest-limitations)).
- Per-gateway credentials and authenticated dashboard sessions rather than a
  shared API key and a hardcoded operator name.
- TLS on both network hops, and MQTT ACLs restricting each collar to its topic.
- Comorbidity handling — an animal can currently hold two open alerts, which is
  right for genuine comorbidity but is also how model flip-flop would present.
- Model drift monitoring and a retraining path once real data accumulates.
- The symptom check is forwarded from the cloud service to the gateway, which
  assumes the cloud can reach the farm network. A real deployment would have the
  dashboard talk to the gateway on the farm LAN, or give the cloud its own copy
  of the model.
- Dashboard actions are open and any origin may call them; a deployment needs
  sessions and a CORS allow-list, not just the sync key.
