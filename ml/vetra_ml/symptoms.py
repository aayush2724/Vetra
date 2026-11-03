"""Diagnose from symptoms a person selects, using the same model as the collars.

The trained model reads an hour of minute-by-minute vitals, not a list of
symptoms. Rather than train a second model on made-up symptom tables, this
module turns each selection into the physiological change it describes — "high
fever" is roughly +1.8 C, "panting" is roughly +30 breaths/min — and renders a
day of telemetry for a few simulated animals of that species with those changes
applied. The readings then go through the unchanged diagnosis engine, clinical
rules included, and the probabilities are averaged across the animals.

Two consequences worth stating plainly:

* The answer comes from exactly the pipeline the gateway runs on live data, so
  any accuracy figure quoted for the model applies here too — and so does its
  limitation: the model was trained on simulated data.
* Rendering several animals rather than one means a result does not hinge on one
  animal's random personal baseline, and the agreement between them is reported
  as a measure of how clear-cut the presentation is.

Shift magnitudes mirror the disease signatures in `synth/conditions.py`, so a
selection lands inside the range the model was trained on.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .config import SAMPLES_PER_WINDOW, WINDOW_STRIDE_MINUTES
from .inference.engine import DiagnosisEngine
from .schema import CONDITION_LABELS, CONDITION_URGENCY, SPECIES
from .synth.conditions import ConditionSignature
from .synth.generator import AnimalPlan, simulate_animal


@dataclass(frozen=True, slots=True)
class Option:
    key: str
    label: str
    # Channel -> shift in native units, as in ConditionSignature.deltas.
    deltas: dict[str, float] = field(default_factory=dict)
    noise_scale: dict[str, float] = field(default_factory=dict)
    distance_factor: float = 1.0
    rest_heart_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class Question:
    key: str
    label: str
    options: tuple[Option, ...]

    def option(self, key: str) -> Option:
        for opt in self.options:
            if opt.key == key:
                return opt
        raise ValueError(
            f"{self.key}: unknown option {key!r}; choose one of {[o.key for o in self.options]}"
        )


NORMAL = "normal"

QUESTIONS: tuple[Question, ...] = (
    Question("body_temperature", "Body temperature", (
        Option(NORMAL, "Normal"),
        Option("raised", "Slightly raised (about +1 °C)", {"body_temperature": 0.9}),
        Option("fever", "High fever (about +2 °C)", {"body_temperature": 1.8},
               {"body_temperature": 1.4}),
        Option("low", "Below normal, cold to the touch", {"body_temperature": -1.5}),
    )),
    Question("heart_rate", "Heart rate", (
        Option(NORMAL, "Normal"),
        Option("raised", "Faster than usual", {"heart_rate": 14.0}),
        Option("very_fast", "Very fast and irregular, even at rest", {"heart_rate": 28.0},
               {"heart_rate": 2.4}, rest_heart_rate=12.0),
    )),
    Question("breathing", "Breathing", (
        Option(NORMAL, "Normal"),
        Option("faster", "Faster than usual", {"respiratory_rate": 10.0}),
        Option("laboured", "Rapid and laboured", {"respiratory_rate": 22.0},
               {"respiratory_rate": 1.7}),
        Option("panting", "Panting, open-mouth breathing", {"respiratory_rate": 30.0},
               {"respiratory_rate": 1.5}),
    )),
    Question("oxygen", "Oxygen level (SpO₂)", (
        Option(NORMAL, "Normal (95–99%)"),
        Option("slightly_low", "Slightly low (about 92–94%)", {"spo2": -3.0}, {"spo2": 1.5}),
        Option("low", "Low (below 91%)", {"spo2": -7.0}, {"spo2": 1.8}),
    )),
    Question("activity", "Activity", (
        Option(NORMAL, "Normal"),
        Option("less", "Less active than usual", {"activity_index": -15.0}, distance_factor=0.65),
        Option("dull", "Dull, mostly lying down", {"activity_index": -30.0}, distance_factor=0.4),
        Option("erratic", "Restless, then lethargic (erratic)", {"activity_index": -10.0},
               {"activity_index": 2.2, "heart_rate": 1.4}, distance_factor=0.85),
    )),
    Question("rumination", "Cud chewing / feeding", (
        Option(NORMAL, "Normal"),
        Option("reduced", "Chewing cud less than usual", {"rumination_min": -0.08}),
        Option("stopped", "Stopped chewing cud, off feed", {"rumination_min": -0.20}),
    )),
    Question("gait", "Walking", (
        Option(NORMAL, "Normal"),
        Option("stiff", "Slightly stiff", {"step_regularity": -0.05}, distance_factor=0.8),
        Option("limping", "Limping or reluctant to walk", {"step_regularity": -0.35},
               {"step_regularity": 2.0}, distance_factor=0.35),
    )),
)

# Context rather than symptoms: they shape how the selections are rendered.
WEATHER: dict[str, tuple[str, float, float]] = {
    # key: (label, ambient shift in C, humidity shift in % points)
    "cool": ("Cool (around 20 °C)", -9.0, 0.0),
    "warm": ("Warm (around 30 °C)", 0.0, 0.0),
    "hot": ("Hot and humid (above 35 °C)", 6.0, 12.0),
}
ONSET: dict[str, str] = {
    "recent": "Started within the last day",
    "long": "Present for weeks",
}

QUESTION_BY_KEY: dict[str, Question] = {q.key: q for q in QUESTIONS}

# What a farmer should do next. Deliberately conservative: the tool supports a
# veterinarian, it does not replace one.
ADVICE: dict[str, str] = {
    "healthy": "No action needed. Keep observing the animal and check again if anything changes.",
    "cardiac_disorder": "Keep the animal calm and rested, avoid driving or handling it, and "
                        "call a veterinarian today for a heart examination.",
    "respiratory_disease": "Separate the animal from the herd, move it to a shaded, well-ventilated "
                           "place and call a veterinarian. Breathing problems with low oxygen can "
                           "worsen quickly.",
    "obesity": "Review the ration with a veterinarian or nutritionist, cut back energy-dense feed "
               "and increase daily movement.",
    "diabetes": "Ask a veterinarian for a blood or urine test to confirm a metabolic disorder, and "
                "keep feeding times regular in the meantime.",
    "heat_stress": "Move the animal into shade now, give plenty of cool drinking water and wet its "
                   "body. Avoid handling it in the hottest hours, and call a veterinarian if its "
                   "breathing has not settled within an hour.",
    "infectious_disease": "Isolate the animal straight away to protect the herd, give water and "
                          "shelter, and call a veterinarian today. Clean shared feeders and "
                          "water troughs.",
    "mobility_disorder": "Keep the animal on soft, dry ground and limit walking. Check hooves and "
                         "legs for injury and arrange a veterinary lameness examination.",
}

DISCLAIMER = (
    "Decision support only. The model was trained on simulated data and has not been "
    "validated on real animals; confirm any diagnosis with a veterinarian."
)

# Readings rendered per animal: one full day of history for the baseline, then
# the morning of the second day up to 10:00 — a time the animal would normally be
# grazing, so a drop in activity is visible. Chosen so the final sample is one at
# which the engine emits a diagnosis.
HISTORY_SAMPLES = 24 * 60 + 10 * 60
assert (HISTORY_SAMPLES - SAMPLES_PER_WINDOW) % WINDOW_STRIDE_MINUTES == 0

# A recent problem builds over a few hours and is fully present for the last three.
RECENT_ONSET_SAMPLES = 6 * 60
RECENT_RAMP_SAMPLES = 3 * 60

DEFAULT_ANIMALS = 5
START = datetime(2026, 5, 1, tzinfo=timezone.utc)


def catalog() -> dict[str, Any]:
    """Everything a form needs to render its drop-downs."""
    return {
        "species": list(SPECIES),
        "questions": [
            {"key": q.key, "label": q.label,
             "options": [{"key": o.key, "label": o.label} for o in q.options]}
            for q in QUESTIONS
        ],
        "weather": [{"key": k, "label": v[0]} for k, v in WEATHER.items()],
        "onset": [{"key": k, "label": v} for k, v in ONSET.items()],
        "defaults": {"weather": "warm", "onset": "recent"},
    }


def build_signature(selections: dict[str, str], chronic: bool) -> ConditionSignature:
    """Combine the selected options into one presentation.

    Shifts add up; variability and distance take the most extreme selection, since
    two symptoms that both make the gait erratic do not make it twice as erratic.
    """
    deltas: dict[str, float] = {}
    noise: dict[str, float] = {}
    distance = 1.0
    rest_hr = 0.0

    for question_key, option_key in selections.items():
        question = QUESTION_BY_KEY.get(question_key)
        if question is None:
            raise ValueError(f"unknown symptom {question_key!r}; known: {sorted(QUESTION_BY_KEY)}")
        option = question.option(option_key)
        for channel, delta in option.deltas.items():
            deltas[channel] = deltas.get(channel, 0.0) + delta
        for channel, scale in option.noise_scale.items():
            noise[channel] = max(noise.get(channel, 1.0), scale)
        distance = min(distance, option.distance_factor)
        rest_hr += option.rest_heart_rate

    return ConditionSignature(
        name="symptom_check",
        deltas=deltas,
        noise_scale=noise,
        distance_factor=distance,
        chronic=chronic,
        rest_heart_rate=rest_hr,
    )


class SymptomChecker:
    """Owns a private diagnosis engine so checks never touch live herd state."""

    def __init__(self, engine: DiagnosisEngine | None = None, animals: int = DEFAULT_ANIMALS) -> None:
        # Only the final window matters, so stride over everything before it.
        self.engine = engine or DiagnosisEngine(stride=HISTORY_SAMPLES)
        self.animals = animals
        # The engine keeps per-animal state; one check at a time keeps them apart.
        self._lock = threading.Lock()

    def diagnose(
        self,
        species: str,
        symptoms: dict[str, str],
        weather: str = "warm",
        onset: str = "recent",
        seed: int = 0,
    ) -> dict[str, Any]:
        if species not in SPECIES:
            raise ValueError(f"unknown species {species!r}; choose one of {list(SPECIES)}")
        if weather not in WEATHER:
            raise ValueError(f"unknown weather {weather!r}; choose one of {list(WEATHER)}")
        if onset not in ONSET:
            raise ValueError(f"unknown onset {onset!r}; choose one of {list(ONSET)}")
        if not isinstance(symptoms, dict):
            raise ValueError("`symptoms` must map each symptom to a chosen option")

        selections = {k: v for k, v in symptoms.items() if v and v != NORMAL}
        signature = build_signature(selections, chronic=(onset == "long"))
        _, ambient_shift, humidity_shift = WEATHER[weather]

        runs = []
        with self._lock:
            for i in range(self.animals):
                runs.append(self._run_one(species, signature, ambient_shift, humidity_shift, seed + i))

        classes = list(runs[0]["probabilities"])
        mean = {c: float(np.mean([r["probabilities"][c] for r in runs])) for c in classes}
        condition = max(mean, key=mean.get)
        agreement = sum(1 for r in runs if r["condition"] == condition) / len(runs)

        # A clinical finding is reported when most of the simulated animals show it.
        seen: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for run in runs:
            for flag in run["rule_flags"]:
                counts[flag["code"]] = counts.get(flag["code"], 0) + 1
                seen.setdefault(flag["code"], flag)
        findings = [
            {"code": code, "severity": seen[code]["severity"], "message": seen[code]["message"]}
            for code, n in counts.items() if n * 2 > len(runs)
        ]

        ranked = sorted(mean.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "species": species,
            "condition": condition,
            "label": CONDITION_LABELS.get(condition, condition),
            "confidence": round(mean[condition], 4),
            "agreement": round(agreement, 2),
            "urgency": CONDITION_URGENCY.get(condition, "medium"),
            "advice": ADVICE.get(condition, ""),
            "probabilities": [
                {"condition": c, "label": CONDITION_LABELS.get(c, c), "probability": round(p, 4)}
                for c, p in ranked
            ],
            "findings": findings,
            "readings": {
                key: round(float(np.mean([r["readings"][key] for r in runs])), 2)
                for key in runs[0]["readings"]
            },
            "simulated_animals": len(runs),
            "model_backend": self.engine.backend,
            "disclaimer": DISCLAIMER,
        }

    def _run_one(
        self,
        species: str,
        signature: ConditionSignature,
        ambient_shift: float,
        humidity_shift: float,
        seed: int,
    ) -> dict[str, Any]:
        animal_id = f"symptom-check-{seed}"
        plan = AnimalPlan(
            animal_id=animal_id,
            species=species,
            condition="symptom_check",
            onset_minute=HISTORY_SAMPLES - RECENT_ONSET_SAMPLES,
            ramp_minutes=RECENT_RAMP_SAMPLES,
            peak_severity=1.0,
        )
        frame = simulate_animal(
            plan, START, days=2, rng=np.random.default_rng(seed),
            signature=signature, ambient_shift_c=ambient_shift, humidity_shift=humidity_shift,
        ).iloc[:HISTORY_SAMPLES]

        rows = frame.drop(columns=["severity", "label"]).to_dict("records")
        for row in rows:
            row["timestamp"] = row["timestamp"].isoformat()

        self.engine.reset(animal_id)
        try:
            diagnoses = self.engine.ingest_many(rows)
        finally:
            self.engine.reset(animal_id)

        result = diagnoses[-1].to_dict()
        window = frame.iloc[-SAMPLES_PER_WINDOW:]
        result["readings"] = {
            "heart_rate": float(window["heart_rate"].mean()),
            "body_temperature": float(window["body_temperature"].mean()),
            "respiratory_rate": float(window["respiratory_rate"].mean()),
            "spo2": float(window["spo2"].mean()),
            "activity_index": float(window["activity_index"].mean()),
            "step_regularity": float(window["step_regularity"].mean()),
        }
        return result
