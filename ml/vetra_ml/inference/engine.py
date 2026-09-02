"""Stateful, offline diagnosis engine for the edge device.

The engine holds one rolling window and one trailing baseline per animal and
emits a diagnosis every `stride` samples. It is deliberately free of any
network, database or broker dependency: it takes records in and hands
diagnoses back, which is what makes it testable and what lets the same object
run inside the MQTT agent, inside a REST handler or inside a unit test.

Memory is the binding constraint on a Raspberry-Pi-class board, so the baseline
history is stored downsampled — one sample every five minutes over 24 hours is
288 values per channel instead of 1440, and a median over a full day does not
need minute resolution to be accurate.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from ..config import MODEL_DIR, SAMPLES_PER_WINDOW, WINDOW_STRIDE_MINUTES
from ..features import CORE_CHANNELS, feature_names, window_features
from ..schema import CONDITIONS, TelemetryRecord
from .rules import RuleFlag, evaluate_rules, worst_severity
from .runtime import InterpreterUnavailable, load_interpreter

BASELINE_DOWNSAMPLE = 5           # keep one baseline sample every 5 minutes
BASELINE_CAPACITY = 24 * 60 // BASELINE_DOWNSAMPLE
# Baseline deviation features are meaningless until enough history exists.
MIN_BASELINE_SAMPLES = 60 // BASELINE_DOWNSAMPLE * 6   # ~6 hours


@dataclass(slots=True)
class Diagnosis:
    animal_id: str
    species: str
    timestamp: str
    condition: str
    confidence: float
    probabilities: dict[str, float]
    rule_flags: list[RuleFlag] = field(default_factory=list)
    rule_severity: str = "info"
    baseline_ready: bool = False
    model_backend: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "animal_id": self.animal_id,
            "species": self.species,
            "timestamp": self.timestamp,
            "condition": self.condition,
            "confidence": round(self.confidence, 4),
            "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
            "rule_flags": [f.to_dict() for f in self.rule_flags],
            "rule_severity": self.rule_severity,
            "baseline_ready": self.baseline_ready,
            "model_backend": self.model_backend,
        }


@dataclass(slots=True)
class _AnimalState:
    species: str
    window: deque = field(default_factory=lambda: deque(maxlen=SAMPLES_PER_WINDOW))
    baseline: dict[str, deque] = field(
        default_factory=lambda: {c: deque(maxlen=BASELINE_CAPACITY) for c in CORE_CHANNELS}
    )
    samples_seen: int = 0
    samples_since_diagnosis: int = 0


class DiagnosisEngine:
    """Feeds telemetry in, gets diagnoses out. No I/O of its own."""

    def __init__(
        self,
        model_path: Path | None = None,
        sklearn_path: Path | None = None,
        stride: int = WINDOW_STRIDE_MINUTES,
    ) -> None:
        self.stride = stride
        self._states: dict[str, _AnimalState] = {}
        self._feature_names = feature_names()
        self._classes = list(CONDITIONS)

        self._interpreter = None
        self._sklearn = None
        self.backend = "none"

        self._load_model(model_path, sklearn_path)

    # --- model loading ---------------------------------------------------
    def _load_model(self, model_path: Path | None, sklearn_path: Path | None) -> None:
        model_path = model_path or (MODEL_DIR / "vetra_dx_edge.tflite")
        sklearn_path = sklearn_path or (MODEL_DIR / "sklearn_model.joblib")

        manifest = MODEL_DIR / "feature_names.json"
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            # A model trained on a different feature order would produce
            # confident nonsense, so refuse to run rather than guess.
            if saved.get("features") and saved["features"] != self._feature_names:
                raise ValueError(
                    "feature order in the model manifest does not match features.py; "
                    "retrain before deploying"
                )
            self._classes = saved.get("classes", self._classes)

        if model_path.exists():
            try:
                self._interpreter = load_interpreter(model_path)
                self._in = self._interpreter.get_input_details()[0]
                self._out = self._interpreter.get_output_details()[0]
                self.backend = f"tflite:{model_path.name}"
                return
            except InterpreterUnavailable:
                pass  # fall through to the classical model

        if sklearn_path.exists():
            import joblib

            bundle = joblib.load(sklearn_path)
            self._sklearn = bundle["model"]
            self._classes = bundle.get("classes", self._classes)
            self.backend = f"sklearn:{bundle.get('model_name', 'model')}"
            return

        raise FileNotFoundError(
            f"no model found at {model_path} or {sklearn_path}; run the training scripts first"
        )

    # --- prediction ------------------------------------------------------
    def _predict_proba(self, features: np.ndarray) -> np.ndarray:
        if self._interpreter is not None:
            row = features.reshape(1, -1).astype(self._in["dtype"])
            self._interpreter.set_tensor(self._in["index"], row)
            self._interpreter.invoke()
            return np.asarray(self._interpreter.get_tensor(self._out["index"])[0], dtype=np.float64)

        proba = self._sklearn.predict_proba(features.reshape(1, -1))[0]
        # sklearn only emits columns for classes it saw; re-expand to the full space.
        full = np.zeros(len(self._classes))
        for column, class_id in enumerate(self._sklearn.classes_):
            full[int(class_id)] = proba[column]
        return full

    # --- ingestion -------------------------------------------------------
    def ingest(self, record: TelemetryRecord | dict[str, Any]) -> Diagnosis | None:
        """Add one sample. Returns a diagnosis when a full window is due."""
        if isinstance(record, dict):
            record = TelemetryRecord.from_dict(record)

        state = self._states.get(record.animal_id)
        if state is None:
            state = _AnimalState(species=record.species)
            self._states[record.animal_id] = state

        row = record.to_dict()
        state.window.append(row)
        state.samples_seen += 1
        state.samples_since_diagnosis += 1

        if state.samples_seen % BASELINE_DOWNSAMPLE == 0:
            for channel in CORE_CHANNELS:
                state.baseline[channel].append(float(row[channel]))

        if len(state.window) < SAMPLES_PER_WINDOW:
            return None
        if state.samples_since_diagnosis < self.stride:
            return None

        state.samples_since_diagnosis = 0
        return self._diagnose(record.animal_id, state)

    def _diagnose(self, animal_id: str, state: _AnimalState) -> Diagnosis:
        window = pd.DataFrame(list(state.window))

        history = state.baseline[CORE_CHANNELS[0]]
        baseline_ready = len(history) >= MIN_BASELINE_SAMPLES
        baseline = (
            {c: float(np.median(state.baseline[c])) for c in CORE_CHANNELS}
            if baseline_ready
            else None
        )

        features = window_features(window, baseline, state.species)
        proba = self._predict_proba(features)
        best = int(np.argmax(proba))

        summary = {
            "heart_rate": float(window["heart_rate"].mean()),
            "hr_at_rest": float(features[self._feature_names.index("hr_at_rest")]),
            "body_temperature": float(window["body_temperature"].mean()),
            "respiratory_rate": float(window["respiratory_rate"].mean()),
            "spo2_min": float(window["spo2"].min()),
            "activity_index": float(window["activity_index"].mean()),
            "rumination_min": float(window["rumination_min"].mean()),
            "step_regularity": float(window["step_regularity"].mean()),
            "thi_mean": float(features[self._feature_names.index("thi_mean")]),
            "hour_of_day": float(pd.Timestamp(window["timestamp"].iloc[-1]).hour),
        }
        if baseline:
            summary.update({f"{c}_baseline": baseline[c] for c in CORE_CHANNELS})

        flags = evaluate_rules(summary, state.species)

        return Diagnosis(
            animal_id=animal_id,
            species=state.species,
            timestamp=str(window["timestamp"].iloc[-1]),
            condition=self._classes[best],
            confidence=float(proba[best]),
            probabilities={name: float(p) for name, p in zip(self._classes, proba)},
            rule_flags=flags,
            rule_severity=worst_severity(flags),
            baseline_ready=baseline_ready,
            model_backend=self.backend,
        )

    def ingest_many(self, records: Iterable[TelemetryRecord | dict]) -> list[Diagnosis]:
        out = []
        for record in records:
            result = self.ingest(record)
            if result is not None:
                out.append(result)
        return out

    def reset(self, animal_id: str | None = None) -> None:
        if animal_id is None:
            self._states.clear()
        else:
            self._states.pop(animal_id, None)

    @property
    def tracked_animals(self) -> int:
        return len(self._states)
