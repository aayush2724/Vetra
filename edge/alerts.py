"""Turn a stream of per-window diagnoses into alerts a person should act on.

The naive version of this module — alert whenever the model says "not healthy"
— produces roughly one message every thirty minutes per animal and is switched
off by the farmer within a week. Everything here exists to stop that:

* **Confirmation.** A condition must appear in several of the recent windows
  before it opens an alert, so a single odd hour stays silent.
* **A fast path for emergencies.** Confirmation costs time, and an animal whose
  oxygen saturation is collapsing does not have an hour to spare. A critical
  clinical rule opens the alert immediately, bypassing confirmation entirely.
* **Hysteresis.** Alerts close only after a sustained run of healthy windows,
  so a borderline animal does not flap between states.
* **Cooldown.** A freshly resolved condition cannot immediately reopen.
* **One open alert per animal per condition,** updated in place as evidence
  accumulates rather than duplicated.

Severity combines how urgent the predicted disease is with how alarming the
measured vitals are, taking the worse of the two — a low-urgency prediction
alongside a critical vital sign is still an emergency.
"""
from __future__ import annotations

import sys
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ml"))

from vetra_ml.schema import CONDITION_LABELS, CONDITION_URGENCY  # noqa: E402

SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
RANK_TO_SEVERITY = {v: k for k, v in SEVERITY_RANK.items()}

# A clinical rule firing at this level maps onto this alert severity.
RULE_SEVERITY_TO_ALERT = {"info": "low", "warning": "medium", "critical": "critical"}

# Who needs to know, by severity.
ROUTING = {
    "low": ("farmer_digest",),
    "medium": ("farmer",),
    "high": ("farmer", "veterinarian"),
    "critical": ("farmer", "veterinarian", "emergency"),
}

# Which clinical findings actually explain each condition. The headline of an
# alert must be the finding that supports *this* diagnosis: telling a farmer to
# "provide shade and water" under a cardiac alert is worse than saying nothing,
# because it is confident, prominent and wrong.
RELEVANT_FLAGS: dict[str, frozenset[str]] = {
    "cardiac_disorder": frozenset({"RESTING_TACHYCARDIA", "HYPOXAEMIA", "SEVERE_HYPOXAEMIA"}),
    "respiratory_disease": frozenset({
        "HYPOXAEMIA", "SEVERE_HYPOXAEMIA", "TACHYPNOEA", "SEVERE_TACHYPNOEA", "FEVER"}),
    "obesity": frozenset({"RECUMBENCY"}),
    "diabetes": frozenset({"RECUMBENCY", "RUMINATION_COLLAPSE"}),
    "heat_stress": frozenset({
        "HEAT_ADVISORY", "HEAT_LOAD", "SEVERE_HEAT_LOAD", "TACHYPNOEA",
        "SEVERE_TACHYPNOEA", "FEVER", "RUMINATION_COLLAPSE"}),
    "infectious_disease": frozenset({
        "FEVER", "HIGH_FEVER", "HYPOTHERMIA", "RECUMBENCY",
        "RUMINATION_COLLAPSE", "RESTING_TACHYCARDIA"}),
    "mobility_disorder": frozenset({"IRREGULAR_GAIT", "RECUMBENCY"}),
}


@dataclass(slots=True)
class AlertPolicy:
    """Tunable thresholds. Defaults assume a 30-minute diagnosis interval."""

    history: int = 4                 # windows kept per animal (~2 hours)
    confirm_count: int = 2           # matching windows needed to open an alert
    min_confidence: float = 0.60     # below this a window is not evidence
    clear_count: int = 3             # consecutive healthy windows before resolving
    cooldown_hours: float = 6.0      # quiet period after a condition resolves


@dataclass(slots=True)
class AlertEvent:
    """What changed. `action` is one of opened | escalated | resolved."""

    action: str
    alert: dict[str, Any]


@dataclass(slots=True)
class _AnimalAlertState:
    recent: deque = field(default_factory=lambda: deque(maxlen=8))
    healthy_streak: int = 0
    cooldown_until: dict[str, datetime] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return _now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def combine_severity(condition: str, rule_severity: str) -> str:
    """Worse of predicted-disease urgency and measured-vital severity."""
    predicted = SEVERITY_RANK.get(CONDITION_URGENCY.get(condition, "medium"), 2)
    measured = SEVERITY_RANK.get(RULE_SEVERITY_TO_ALERT.get(rule_severity, "low"), 1)
    return RANK_TO_SEVERITY[max(predicted, measured)]


def build_message(diagnosis: dict[str, Any], severity: str) -> str:
    """One line a farmer can read on a phone without opening the app."""
    label = CONDITION_LABELS.get(diagnosis["condition"], diagnosis["condition"])
    animal = diagnosis["animal_id"]
    confidence = float(diagnosis.get("confidence", 0.0))

    flags = diagnosis.get("rule_flags") or []

    def rank(flag: dict) -> tuple[int, int]:
        relevant = flag.get("code") in RELEVANT_FLAGS.get(diagnosis["condition"], frozenset())
        severity = SEVERITY_RANK.get(RULE_SEVERITY_TO_ALERT.get(flag.get("severity", "info"), "low"), 1)
        return (int(relevant), severity)

    # A finding that explains the diagnosis beats a more severe but unrelated
    # one; an unrelated critical finding still gets its own flag in the payload.
    headline = max(flags, key=rank) if flags else None
    if headline is not None and rank(headline)[0] == 1:
        return f"{animal}: possible {label.lower()} ({confidence:.0%} confidence). {headline['message']}"
    return f"{animal}: possible {label.lower()} ({confidence:.0%} confidence)."


class AlertEngine:
    """Stateful alert generator. Feed it diagnoses; it emits state changes."""

    def __init__(
        self,
        policy: AlertPolicy | None = None,
        on_event: Callable[[AlertEvent], None] | None = None,
    ) -> None:
        self.policy = policy or AlertPolicy()
        self.on_event = on_event
        self._states: dict[str, _AnimalAlertState] = defaultdict(_AnimalAlertState)
        # animal_id -> condition -> open alert
        self._open: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def load_open_alerts(self, alerts: list[dict[str, Any]]) -> None:
        """Restore open alerts after a restart so the device does not re-notify."""
        for alert in alerts:
            if alert.get("status") == "open":
                self._open[alert["animal_id"]][alert["condition"]] = alert

    def observe(self, diagnosis: dict[str, Any]) -> list[AlertEvent]:
        """Process one diagnosis, returning any alert state changes it caused."""
        animal_id = diagnosis["animal_id"]
        condition = diagnosis["condition"]
        confidence = float(diagnosis.get("confidence", 0.0))
        rule_severity = diagnosis.get("rule_severity", "info")
        observed_at = _parse_time(diagnosis.get("timestamp", ""))

        state = self._states[animal_id]
        state.recent.append((condition, confidence, rule_severity))

        events: list[AlertEvent] = []

        if condition == "healthy" or confidence < self.policy.min_confidence:
            state.healthy_streak += 1
            events.extend(self._maybe_resolve(animal_id, observed_at))
            return self._emit(events)

        state.healthy_streak = 0

        # An animal in genuine distress is not made to wait for confirmation.
        emergency = rule_severity == "critical"

        window = list(state.recent)[-self.policy.history:]
        agreeing = sum(
            1 for c, conf, _ in window
            if c == condition and conf >= self.policy.min_confidence
        )
        if not emergency and agreeing < self.policy.confirm_count:
            return self._emit(events)

        until = state.cooldown_until.get(condition)
        if until and observed_at < until and not emergency:
            return self._emit(events)

        severity = combine_severity(condition, rule_severity)
        existing = self._open[animal_id].get(condition)

        if existing is None:
            events.append(AlertEvent("opened", self._new_alert(diagnosis, severity, observed_at, agreeing)))
        else:
            updated = self._update_alert(existing, diagnosis, severity, observed_at, agreeing)
            # Only re-notify when things got worse; a steady alert stays quiet.
            if SEVERITY_RANK[severity] > SEVERITY_RANK[existing["severity"]]:
                events.append(AlertEvent("escalated", updated))

        return self._emit(events)

    def _new_alert(self, diagnosis, severity, observed_at, agreeing) -> dict[str, Any]:
        alert = {
            "alert_id": str(uuid.uuid4()),
            "animal_id": diagnosis["animal_id"],
            "species": diagnosis.get("species", ""),
            "condition": diagnosis["condition"],
            "condition_label": CONDITION_LABELS.get(diagnosis["condition"], diagnosis["condition"]),
            "severity": severity,
            "status": "open",
            "opened_at": observed_at.isoformat(timespec="seconds"),
            "updated_at": observed_at.isoformat(timespec="seconds"),
            "resolved_at": None,
            "acknowledged_by": None,
            "confidence": round(float(diagnosis.get("confidence", 0.0)), 4),
            "supporting_windows": agreeing,
            "rule_flags": diagnosis.get("rule_flags", []),
            "notify": list(ROUTING.get(severity, ("farmer",))),
            "message": build_message(diagnosis, severity),
            "latest_diagnosis": diagnosis,
        }
        self._open[diagnosis["animal_id"]][diagnosis["condition"]] = alert
        return alert

    def _update_alert(self, alert, diagnosis, severity, observed_at, agreeing) -> dict[str, Any]:
        alert["severity"] = RANK_TO_SEVERITY[
            max(SEVERITY_RANK[severity], SEVERITY_RANK[alert["severity"]])
        ]
        alert["updated_at"] = observed_at.isoformat(timespec="seconds")
        alert["confidence"] = round(float(diagnosis.get("confidence", 0.0)), 4)
        alert["supporting_windows"] = agreeing
        alert["rule_flags"] = diagnosis.get("rule_flags", [])
        alert["notify"] = list(ROUTING.get(alert["severity"], ("farmer",)))
        alert["message"] = build_message(diagnosis, alert["severity"])
        alert["latest_diagnosis"] = diagnosis
        return alert

    def _maybe_resolve(self, animal_id: str, observed_at: datetime) -> list[AlertEvent]:
        state = self._states[animal_id]
        if state.healthy_streak < self.policy.clear_count:
            return []

        events = []
        for condition, alert in list(self._open[animal_id].items()):
            alert["status"] = "resolved"
            alert["resolved_at"] = observed_at.isoformat(timespec="seconds")
            alert["updated_at"] = alert["resolved_at"]
            alert["message"] = (
                f"{animal_id}: {alert['condition_label'].lower()} no longer detected "
                f"after {state.healthy_streak} normal readings."
            )
            state.cooldown_until[condition] = observed_at + timedelta(hours=self.policy.cooldown_hours)
            del self._open[animal_id][condition]
            events.append(AlertEvent("resolved", alert))
        return events

    def _emit(self, events: list[AlertEvent]) -> list[AlertEvent]:
        if self.on_event:
            for event in events:
                self.on_event(event)
        return events

    def open_alerts(self) -> list[dict[str, Any]]:
        return [a for by_condition in self._open.values() for a in by_condition.values()]
