"""Evaluation built around what the system is actually for.

Plain accuracy flatters a model on this problem: over half the windows are
healthy, so predicting "healthy" everywhere already scores ~53%. The metrics
that matter here are

* **screening recall** — of all genuinely sick windows, how many raised any
  alert at all (a farmer would rather be told "something is wrong" than nothing);
* **recall by severity band** — does the model catch disease while it is still
  mild, which is the whole premise of early diagnosis; and
* **detection latency** — hours between disease onset and the first correct
  alert, per animal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)

from .schema import CONDITIONS, CONDITION_TO_ID

HEALTHY_ID = CONDITION_TO_ID["healthy"]

SEVERITY_BANDS: tuple[tuple[str, float, float], ...] = (
    ("early   (0.15-0.35)", 0.15, 0.35),
    ("mild    (0.35-0.55)", 0.35, 0.55),
    ("marked  (0.55-0.75)", 0.55, 0.75),
    ("severe  (0.75-1.00)", 0.75, 1.01),
)


@dataclass
class EvalReport:
    model_name: str
    accuracy: float
    balanced_accuracy: float
    macro_f1: float
    screening_recall: float
    screening_precision: float
    false_alarm_rate: float
    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    recall_by_severity: dict[str, float] = field(default_factory=dict)
    median_detection_hours: float | None = None
    confusion: list[list[int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "model": self.model_name,
            "accuracy": round(self.accuracy, 4),
            "balanced_accuracy": round(self.balanced_accuracy, 4),
            "macro_f1": round(self.macro_f1, 4),
            "screening_recall": round(self.screening_recall, 4),
            "screening_precision": round(self.screening_precision, 4),
            "false_alarm_rate": round(self.false_alarm_rate, 4),
            "median_detection_hours": self.median_detection_hours,
            "recall_by_severity": {k: round(v, 4) for k, v in self.recall_by_severity.items()},
            "per_class": self.per_class,
            "confusion": self.confusion,
            "class_order": list(CONDITIONS),
        }


def _detection_latency(meta: pd.DataFrame, y_true: np.ndarray, y_pred: np.ndarray) -> float | None:
    """Median hours from the first symptomatic window to the first correct alert."""
    frame = meta.reset_index(drop=True).copy()
    frame["y_true"] = y_true
    frame["y_pred"] = y_pred

    latencies: list[float] = []
    for _, animal in frame.groupby("animal_id"):
        sick = animal[animal["y_true"] != HEALTHY_ID].sort_values("window_end")
        if sick.empty:
            continue
        onset = pd.Timestamp(sick["window_end"].iloc[0])
        correct = sick[sick["y_pred"] == sick["y_true"]]
        if correct.empty:
            continue  # never detected; excluded here, visible in recall
        first = pd.Timestamp(correct["window_end"].iloc[0])
        latencies.append((first - onset).total_seconds() / 3600.0)

    return round(float(np.median(latencies)), 2) if latencies else None


def evaluate(
    model_name: str,
    y_true: np.ndarray,
    y_pred: np.ndarray,
    meta: pd.DataFrame | None = None,
) -> EvalReport:
    labels = list(range(len(CONDITIONS)))

    report_dict = classification_report(
        y_true, y_pred, labels=labels, target_names=list(CONDITIONS),
        output_dict=True, zero_division=0,
    )
    per_class = {
        name: {
            "precision": round(report_dict[name]["precision"], 4),
            "recall": round(report_dict[name]["recall"], 4),
            "f1": round(report_dict[name]["f1-score"], 4),
            "support": int(report_dict[name]["support"]),
        }
        for name in CONDITIONS
    }

    # Screening view: did we flag the animal as unwell at all?
    sick_true = y_true != HEALTHY_ID
    sick_pred = y_pred != HEALTHY_ID
    tp = int((sick_true & sick_pred).sum())
    fn = int((sick_true & ~sick_pred).sum())
    fp = int((~sick_true & sick_pred).sum())
    tn = int((~sick_true & ~sick_pred).sum())

    screening_recall = tp / (tp + fn) if (tp + fn) else 0.0
    screening_precision = tp / (tp + fp) if (tp + fp) else 0.0
    false_alarm_rate = fp / (fp + tn) if (fp + tn) else 0.0

    recall_by_severity: dict[str, float] = {}
    median_hours: float | None = None
    if meta is not None and "severity" in meta:
        severity = meta["severity"].to_numpy()
        for name, low, high in SEVERITY_BANDS:
            band = sick_true & (severity >= low) & (severity < high)
            n = int(band.sum())
            recall_by_severity[name] = (
                float((y_pred[band] == y_true[band]).mean()) if n else float("nan")
            )
        median_hours = _detection_latency(meta, y_true, y_pred)

    return EvalReport(
        model_name=model_name,
        accuracy=float(accuracy_score(y_true, y_pred)),
        balanced_accuracy=float(balanced_accuracy_score(y_true, y_pred)),
        macro_f1=float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
        screening_recall=screening_recall,
        screening_precision=screening_precision,
        false_alarm_rate=false_alarm_rate,
        per_class=per_class,
        recall_by_severity=recall_by_severity,
        median_detection_hours=median_hours,
        confusion=confusion_matrix(y_true, y_pred, labels=labels).tolist(),
    )


def print_report(report: EvalReport) -> None:
    print(f"\n=== {report.model_name} ===")
    print(f"  accuracy            {report.accuracy:.3f}")
    print(f"  balanced accuracy   {report.balanced_accuracy:.3f}")
    print(f"  macro F1            {report.macro_f1:.3f}")
    print(f"  screening recall    {report.screening_recall:.3f}   (sick windows raising any alert)")
    print(f"  screening precision {report.screening_precision:.3f}")
    print(f"  false alarm rate    {report.false_alarm_rate:.3f}   (healthy windows wrongly alerted)")
    if report.median_detection_hours is not None:
        print(f"  detection latency   {report.median_detection_hours:.2f} h (median, from onset)")

    print("\n  per condition           prec   recall     f1   support")
    for name, m in report.per_class.items():
        print(f"    {name:<20} {m['precision']:>6.3f} {m['recall']:>7.3f} {m['f1']:>6.3f} {m['support']:>8,}")

    if report.recall_by_severity:
        print("\n  recall by severity (early detection)")
        for band, value in report.recall_by_severity.items():
            shown = "  n/a" if np.isnan(value) else f"{value:.3f}"
            print(f"    {band:<22} {shown}")


def print_confusion(report: EvalReport) -> None:
    short = [c[:9] for c in CONDITIONS]
    print("\n  confusion matrix (rows = actual, cols = predicted)")
    print("             " + " ".join(f"{s:>9}" for s in short))
    for name, row in zip(short, report.confusion):
        print(f"    {name:<9}" + " ".join(f"{v:>9,}" for v in row))


def save_report(report: EvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2))
