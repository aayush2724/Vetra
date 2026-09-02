# Architecture

## The constraint that shapes everything

Rural connectivity is intermittent. That single assumption decides the whole
design: **every diagnosis is made on the edge device, and the cloud is only ever
a place to copy records to.** If the network is down for a week, the farm keeps
being monitored, alerts keep reaching the farmer's phone over the local network,
and the upload queue simply grows.

The inverse design — stream vitals to a server and classify them there — is
easier to build and useless in the field, because the moment the link drops the
farm stops being monitored.

```
  collars / sensors
        │  MQTT (QoS 1)  or  REST POST
        ▼
  ┌──────────────────────────── edge gateway (Raspberry Pi class) ───────────┐
  │                                                                          │
  │  ingest ──► validate ──► SQLite ──► rolling window ──► TFLite model       │
  │  (agent.py)  (schema.py)  (store.py)  (features.py)     (engine.py)       │
  │                                              │                            │
  │                                              ├──► clinical rules          │
  │                                              │    (rules.py)              │
  │                                              ▼                            │
  │                                        alert engine ──► farmer / vet      │
  │                                        (alerts.py)                        │
  │                                              │                            │
  │                                    cloud sync (sync.py) ─ when online ─┐  │
  └────────────────────────────────────────────────────────────────────────┼──┘
                                                                           ▼
                              ┌──────────── cloud service (Node.js) ──────────┐
                              │  /api/sync/*  ──►  health records             │
                              │  /api/*       ──►  dashboard reads            │
                              │  /api/events  ──►  live push (SSE)            │
                              └───────────────────────┬───────────────────────┘
                                                      ▼
                                        React dashboard (farmer / vet)
```

## Layers and their objectives

| Layer | Directory | Objective |
|---|---|---|
| Telemetry schema and validation | `ml/vetra_ml/schema.py` | 1 |
| Synthetic herd generator | `ml/vetra_ml/synth/` | 1 |
| Windowed feature engineering | `ml/vetra_ml/features.py` | 1 |
| Model training and evaluation | `ml/vetra_ml/train_*.py`, `evaluate.py` | 2 |
| TFLite export and quantisation | `ml/vetra_ml/export_tflite.py` | 3 |
| Offline inference engine | `ml/vetra_ml/inference/` | 3 |
| Alert generation | `edge/alerts.py` | 4 |
| Local records and cloud sync | `edge/store.py`, `edge/sync.py` | 5 |
| Cloud records API | `server/` | 5 |
| Dashboard | `dashboard/` | 6 |

## Decisions worth defending

### The train/test split is grouped by animal

Windows overlap by 50%, so consecutive windows from one animal are near
duplicates. A random split puts almost-identical rows on both sides and reports
accuracy that evaporates on a new animal. Splitting by animal answers the
question that matters: *how does this perform on an animal it has never seen?*
`tests/test_pipeline.py::test_no_animal_appears_in_two_splits` pins it.

### Features are both absolute and self-relative

Species-normalised absolutes catch conditions already present at enrollment
(obesity, diabetes). Deviation from the animal's own trailing 24-hour baseline
catches acute change (fever, panting, lameness). Either view alone misses half
the label space.

The baseline is shifted by one sample so a window can never contribute to the
baseline it is compared against — that leak would make acute change look normal.

### One feature implementation, used by both training and the device

`window_features()` is called by the batch builder *and* by the streaming
engine. Two implementations would drift, and the drift would show up as a model
that quietly performs worse in the field than on the bench.
`tests/test_features.py::test_streaming_and_batch_features_agree` pins it.

### Normalisation lives inside the exported model

The `.tflite` file carries its own `Normalization` layer, so there is no
separate scaler file to keep in sync with it — the usual way edge deployments
silently break.

The cost of that choice showed up in conversion: full-int8 quantisation
collapsed to 0.08 macro-F1, because the raw features span from `step_regularity`
(~0.9) to `distance_sum` (thousands), and int8 *input* activations cannot
represent both. The export script measures every variant against the float model
and refuses any that disagrees by more than 1%, which rejected full-int8
automatically. Dynamic-range int8 was selected: **12.2 KB, 0.006 ms/inference,
0.982 macro-F1**.

### Clinical rules run alongside the model, not after it

A model trained on simulated data must never be the only thing between an animal
and a critical vital sign. The rules in `inference/rules.py` fire on textbook
thresholds regardless of what the model says, and they double as the
explanation: a vet acts on "SpO₂ 88%, respiratory rate 64/min", not on
"respiratory_disease, p=0.94".

### The alert policy is the product

Alerting on every non-healthy window produces ~26 messages per animal per day
and gets muted within a week. Confirmation, hysteresis, cooldown and
one-alert-per-animal-per-condition cut that to 0.22 per animal-day — a 99.2%
reduction — while still catching 35/35 sick animals and alerting on none of the
13 healthy ones (`make evaluate`).

A critical clinical rule bypasses confirmation entirely, because an animal whose
oxygen is collapsing does not have an hour to spare.

### Sync state is a column, not an outbox

`synced_at` is nullable on each table. Re-uploading is therefore idempotent, an
interrupted sync resumes by re-asking for unsynced rows, and there is no second
structure that can drift out of step with the data. Records are marked synced
only after the server acknowledges them, so a dropped connection costs a retry
and never a record.

Tables sync in dependency order — animals, telemetry, diagnoses, alerts — and a
failure stops the pass rather than hammering a link that is down. Diagnoses and
alerts are small and drain first; bulk telemetry follows.

## Swapping SQLite for Firestore

The brief names Firebase/Firestore. Firestore needs a Google Cloud project,
service-account credentials and network access, none of which a rural
deployment or an offline examiner can rely on, so the implementation uses SQLite
and confines every query to one module.

`server/src/db.js` exports `HealthRecordRepository`. Nothing above it writes
SQL. A Firestore version implements the same methods:

| Repository method | Firestore equivalent |
|---|---|
| `saveAnimals` | `animals/{animal_id}` — `set(..., { merge: true })` |
| `saveTelemetry` | `animals/{animal_id}/telemetry/{timestamp}` — batched writes |
| `saveDiagnoses` | `animals/{animal_id}/diagnoses/{timestamp}` |
| `saveAlerts` | `alerts/{alert_id}` |
| `listAnimals` | `animals` collection + a denormalised `latest_diagnosis` field |
| `getTelemetry` | subcollection query ordered by document id, limited |
| `listAlerts` | `alerts` where `status == 'open'`, ordered by severity |
| `herdSummary` | counter documents maintained on write |

Two differences matter. Firestore has no joins, so `listAnimals` needs the
latest diagnosis denormalised onto the animal document at write time. And
Firestore charges per document read, so minute-resolution telemetry should be
downsampled or stored in hour-blocks rather than one document per reading.

## Data volumes

Minute-resolution telemetry is roughly 0.6 KB per row in the local store.

| Herd | Per day | Per 7-day retention window |
|---|---|---|
| 12 animals | ~10 MB | ~70 MB |
| 240 animals | ~200 MB | ~1.4 GB |

The gateway prunes hourly, deleting only raw telemetry that is both older than
the retention window and already uploaded. Diagnoses and alerts are the
permanent health record and are never pruned. On a constrained device or a
metered link, `SyncConfig.upload_telemetry = False` keeps diagnoses and alerts
flowing while leaving bulk vitals on the device.

## Security

The scope here is a college project, and the authentication is deliberately
minimal but not absent:

* Sync endpoints accept a shared bearer token when `VETRA_API_KEY` is set.
  Reads stay open so the dashboard does not need the gateway's key.
* Table names reaching SQL are allow-listed, and every other value is bound as a
  parameter (`tests/test_store.py::test_unknown_table_is_rejected`).
* Incoming payloads are validated against plausible physiological ranges before
  anything is stored or classified.

What a real deployment would add: per-gateway credentials rather than one shared
key, authenticated dashboard sessions instead of a hardcoded operator name, TLS
on both hops, and MQTT broker ACLs restricting each collar to its own topic.
