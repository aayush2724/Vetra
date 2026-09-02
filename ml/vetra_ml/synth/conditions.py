"""Disease signatures expressed as deviations from an animal's own baseline.

Each condition lists the shift it produces in every sensor channel at *full*
severity, in the channel's own physical units. The generator scales these
linearly by a severity that ramps up from onset, which is what makes early,
subtle presentations appear in the training set rather than only florid ones.

`noise_scale` multiplies a channel's minute-to-minute variability. It carries
real diagnostic information: an arrhythmic heart is not just faster, it is more
erratic, and that instability is often visible before the mean rate shifts.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ConditionSignature:
    name: str
    # Channel -> absolute shift at severity 1.0, in the channel's native units.
    deltas: dict[str, float] = field(default_factory=dict)
    # Channel -> multiplier on short-term variability at severity 1.0.
    noise_scale: dict[str, float] = field(default_factory=dict)
    # Fraction of normal distance covered at full severity.
    distance_factor: float = 1.0
    # True when the condition is environmentally triggered rather than intrinsic.
    heat_driven: bool = False
    # Chronic conditions hold a steady severity; acute ones ramp from onset.
    chronic: bool = False
    hallmark: str = ""


SIGNATURES: dict[str, ConditionSignature] = {
    "healthy": ConditionSignature(name="healthy", hallmark="all channels within reference range"),

    # Tachycardia at rest with an irregular rhythm and poor exercise tolerance:
    # the rate stays elevated even as the animal stops moving.
    "cardiac_disorder": ConditionSignature(
        name="cardiac_disorder",
        deltas={
            "heart_rate": 28.0,
            "respiratory_rate": 8.0,
            "spo2": -3.0,
            "activity_index": -18.0,
            "rumination_min": -0.05,
            "step_regularity": -0.02,
        },
        noise_scale={"heart_rate": 2.4, "spo2": 1.5},
        distance_factor=0.65,
        hallmark="resting tachycardia with high beat-to-beat irregularity",
    ),

    # Hypoxaemia is what separates this from a plain fever: the animal breathes
    # much harder yet saturates worse.
    "respiratory_disease": ConditionSignature(
        name="respiratory_disease",
        deltas={
            "respiratory_rate": 22.0,
            "spo2": -7.0,
            "body_temperature": 0.8,
            "heart_rate": 12.0,
            "activity_index": -20.0,
            "rumination_min": -0.12,
        },
        noise_scale={"respiratory_rate": 1.7, "spo2": 1.8},
        distance_factor=0.6,
        hallmark="high respiratory rate together with falling SpO2",
    ),

    # Chronic over-conditioning: sustained inactivity and a mild resting load on
    # heart and lungs, with no fever and no gait change.
    "obesity": ConditionSignature(
        name="obesity",
        deltas={
            "activity_index": -28.0,
            "heart_rate": 10.0,
            "respiratory_rate": 6.0,
            "body_temperature": 0.10,
            "step_regularity": -0.05,
            "rumination_min": 0.02,
        },
        noise_scale={"activity_index": 0.7},
        distance_factor=0.45,
        chronic=True,
        hallmark="persistent low activity with normal temperature",
    ),

    # Metabolic instability shows up as erratic behaviour — lethargy broken by
    # restless drinking trips — more than as any single shifted mean.
    "diabetes": ConditionSignature(
        name="diabetes",
        deltas={
            "heart_rate": 8.0,
            "activity_index": -10.0,
            "respiratory_rate": 4.0,
            "spo2": -1.0,
            "body_temperature": 0.10,
            "rumination_min": -0.08,
        },
        noise_scale={"activity_index": 2.2, "heart_rate": 1.4},
        distance_factor=0.85,
        chronic=True,
        hallmark="erratic activity variance with near-normal vitals",
    ),

    # Panting dominates. Only meaningful when the temperature-humidity index is
    # already high, so the generator forces a hot-environment window for these animals.
    "heat_stress": ConditionSignature(
        name="heat_stress",
        deltas={
            "respiratory_rate": 30.0,
            "body_temperature": 1.2,
            "heart_rate": 16.0,
            "rumination_min": -0.18,
            "activity_index": -15.0,
            "spo2": -1.5,
        },
        noise_scale={"respiratory_rate": 1.5},
        distance_factor=0.55,
        heat_driven=True,
        hallmark="extreme respiratory rate under a high temperature-humidity index",
    ),

    # Fever plus collapse of rumination and activity — the classic sick-animal
    # picture, distinguished from respiratory disease by preserved SpO2.
    "infectious_disease": ConditionSignature(
        name="infectious_disease",
        deltas={
            "body_temperature": 1.8,
            "heart_rate": 22.0,
            "respiratory_rate": 12.0,
            "activity_index": -30.0,
            "rumination_min": -0.20,
            "spo2": -2.5,
            "step_regularity": -0.04,
        },
        noise_scale={"body_temperature": 1.4},
        distance_factor=0.4,
        hallmark="fever with collapsed rumination and activity",
    ),

    # Lameness barely touches the vitals; it is written almost entirely in the
    # gait rhythm and the distance the animal is willing to cover.
    "mobility_disorder": ConditionSignature(
        name="mobility_disorder",
        deltas={
            "step_regularity": -0.35,
            "activity_index": -25.0,
            "heart_rate": 6.0,
            "respiratory_rate": 3.0,
            "body_temperature": 0.15,
            "rumination_min": -0.03,
        },
        noise_scale={"step_regularity": 2.0},
        distance_factor=0.35,
        hallmark="irregular gait rhythm with normal temperature",
    ),
}


def signature_for(condition: str) -> ConditionSignature:
    try:
        return SIGNATURES[condition]
    except KeyError as exc:
        raise KeyError(
            f"no signature for {condition!r}; known: {sorted(SIGNATURES)}"
        ) from exc
