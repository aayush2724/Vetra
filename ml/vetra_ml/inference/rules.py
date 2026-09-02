"""Deterministic clinical guardrails that run alongside the model.

A statistical model trained on simulated data must never be the only thing
standing between an animal and a critical vital sign. These rules encode
textbook thresholds directly, so a collapsing SpO2 raises an alarm even if the
classifier is confidently wrong.

They serve a second purpose that matters just as much in the field: they are
the explanation. A veterinarian is asked to act on "SpO2 88%, respiratory rate
64/min", not on "respiratory_disease, p=0.94".
"""
from __future__ import annotations

from dataclasses import dataclass

from ..synth.species import profile_for

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True, slots=True)
class RuleFlag:
    code: str
    severity: str          # info | warning | critical
    message: str
    value: float
    threshold: float

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "value": round(self.value, 2),
            "threshold": round(self.threshold, 2),
        }


def _flag(code, severity, message, value, threshold) -> RuleFlag:
    return RuleFlag(code, severity, message, float(value), float(threshold))


def evaluate_rules(summary: dict[str, float], species: str) -> list[RuleFlag]:
    """Check a window summary against clinical thresholds.

    `summary` carries the window means and extremes the engine already computed:
    heart_rate, hr_at_rest, body_temperature, respiratory_rate, spo2_min,
    activity_index, rumination_min, step_regularity, thi_mean, plus optional
    `*_baseline` entries for the animal's own trailing medians.
    """
    profile = profile_for(species)
    flags: list[RuleFlag] = []

    temp = summary.get("body_temperature")
    if temp is not None:
        # A full degree above the species ceiling is a fever, not a hot day:
        # ambient heat alone rarely moves core temperature this far.
        fever_limit = profile.body_temperature.high + 1.0
        if temp >= fever_limit + 0.7:
            flags.append(_flag("HIGH_FEVER", "critical",
                               f"Body temperature {temp:.1f}C — marked fever", temp, fever_limit + 0.7))
        elif temp >= fever_limit:
            flags.append(_flag("FEVER", "warning",
                               f"Body temperature {temp:.1f}C above normal range", temp, fever_limit))
        elif temp <= profile.body_temperature.low - 1.0:
            flags.append(_flag("HYPOTHERMIA", "critical",
                               f"Body temperature {temp:.1f}C below normal range",
                               temp, profile.body_temperature.low - 1.0))

    spo2 = summary.get("spo2_min")
    if spo2 is not None:
        if spo2 < 88.0:
            flags.append(_flag("SEVERE_HYPOXAEMIA", "critical",
                               f"Oxygen saturation fell to {spo2:.0f}% — urgent", spo2, 88.0))
        elif spo2 < 92.0:
            flags.append(_flag("HYPOXAEMIA", "warning",
                               f"Oxygen saturation fell to {spo2:.0f}%", spo2, 92.0))

    # Resting rate is the meaningful one: any animal's heart races while grazing.
    hr_rest = summary.get("hr_at_rest")
    if hr_rest is not None:
        limit = profile.heart_rate.high * 1.30
        if hr_rest >= limit:
            flags.append(_flag("RESTING_TACHYCARDIA", "warning",
                               f"Resting heart rate {hr_rest:.0f} bpm well above normal", hr_rest, limit))

    rr = summary.get("respiratory_rate")
    if rr is not None:
        limit = profile.respiratory_rate.high * 1.50
        if rr >= limit * 1.25:
            flags.append(_flag("SEVERE_TACHYPNOEA", "critical",
                               f"Respiratory rate {rr:.0f}/min — severe respiratory distress",
                               rr, limit * 1.25))
        elif rr >= limit:
            flags.append(_flag("TACHYPNOEA", "warning",
                               f"Respiratory rate {rr:.0f}/min above normal", rr, limit))

    # Heat load is a property of the weather, not of the individual animal: on a
    # hot afternoon it is true of the whole herd at once. Raising it as a
    # per-animal warning would bury the genuine alerts, so mild heat is an
    # advisory and only genuinely dangerous indices escalate.
    thi = summary.get("thi_mean")
    if thi is not None:
        if thi >= 88.0:
            flags.append(_flag("SEVERE_HEAT_LOAD", "critical",
                               f"Temperature-humidity index {thi:.0f} — severe heat load, act now",
                               thi, 88.0))
        elif thi >= 84.0:
            flags.append(_flag("HEAT_LOAD", "warning",
                               f"Temperature-humidity index {thi:.0f} — provide shade and water",
                               thi, 84.0))
        elif thi >= 78.0:
            flags.append(_flag("HEAT_ADVISORY", "info",
                               f"Temperature-humidity index {thi:.0f} — mild heat load across the herd",
                               thi, 78.0))

    gait = summary.get("step_regularity")
    if gait is not None and gait < 0.68:
        flags.append(_flag("IRREGULAR_GAIT", "warning",
                           f"Gait rhythm irregular ({gait:.2f}) — check for lameness", gait, 0.68))

    # Lying still is what a healthy animal does at night, so stillness only
    # counts as recumbency during the hours this animal would normally graze,
    # and only when it is well below the animal's own usual activity.
    activity = summary.get("activity_index")
    activity_base = summary.get("activity_index_baseline")
    hour = summary.get("hour_of_day")
    daytime = hour is None or 6 <= hour < 18
    unusually_still = activity_base is None or activity < 0.35 * activity_base
    if activity is not None and activity < 3.0 and daytime and unusually_still:
        flags.append(_flag("RECUMBENCY", "warning",
                           "Almost no movement during normal grazing hours", activity, 3.0))

    # Rumination is judged against the animal's own habit, not a fixed number:
    # a 30% drop matters far more than any absolute value.
    rumination = summary.get("rumination_min")
    rumination_base = summary.get("rumination_min_baseline")
    if rumination is not None and rumination_base and rumination_base > 0.05:
        drop = 1.0 - (rumination / rumination_base)
        if drop >= 0.40:
            flags.append(_flag("RUMINATION_COLLAPSE", "warning",
                               f"Rumination down {drop:.0%} against this animal's own baseline",
                               rumination, rumination_base * 0.60))

    return flags


def worst_severity(flags: list[RuleFlag]) -> str:
    """Highest severity present, or 'info' when nothing fired."""
    if not flags:
        return "info"
    return max(flags, key=lambda f: SEVERITY_ORDER[f.severity]).severity
