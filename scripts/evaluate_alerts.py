"""Measure the alert pipeline end to end on held-out animals.

Detection accuracy is measured per window elsewhere; this script answers the
question a farmer would actually ask: how many messages will this thing send
me, and does it catch the sick animals? It replays raw telemetry through the
real edge engine and the real alert engine, then compares against the naive
policy of alerting on every non-healthy window.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ml"))
sys.path.insert(0, str(ROOT / "edge"))

from alerts import AlertEngine, AlertPolicy          # noqa: E402
from vetra_ml.config import RAW_DIR, REPORT_DIR      # noqa: E402
from vetra_ml.data import make_split                 # noqa: E402
from vetra_ml.inference.engine import DiagnosisEngine  # noqa: E402
from vetra_ml.schema import TelemetryRecord          # noqa: E402


def replay(raw: pd.DataFrame, animal_ids: list[str]) -> dict:
    engine = DiagnosisEngine()
    alert_engine = AlertEngine(AlertPolicy())

    per_animal: dict[str, dict] = {}
    naive_alerts = 0
    policy_alerts = 0

    for animal_id in animal_ids:
        animal = raw[raw.animal_id == animal_id].sort_values("timestamp")
        if animal.empty:
            continue
        truth = animal["label"].iloc[0]
        engine.reset(animal_id)

        # First moment this animal is genuinely symptomatic, for lead-time maths.
        symptomatic = animal[animal.severity >= 0.15]
        onset = pd.Timestamp(symptomatic.timestamp.iloc[0]) if not symptomatic.empty else None

        opened: list[dict] = []
        naive_here = 0
        windows = 0

        for row in animal.to_dict("records"):
            row["timestamp"] = str(row["timestamp"])
            diagnosis = engine.ingest(TelemetryRecord.from_dict(row))
            if diagnosis is None:
                continue
            windows += 1
            payload = diagnosis.to_dict()

            if payload["condition"] != "healthy":
                naive_here += 1

            for event in alert_engine.observe(payload):
                if event.action == "opened":
                    opened.append(event.alert)

        naive_alerts += naive_here
        policy_alerts += len(opened)

        correct = [a for a in opened if a["condition"] == truth]
        lead_hours = None
        if correct and onset is not None:
            first = pd.Timestamp(correct[0]["opened_at"])
            lead_hours = (first - onset).total_seconds() / 3600.0

        per_animal[animal_id] = {
            "true_condition": truth,
            "windows": windows,
            "naive_alerts": naive_here,
            "policy_alerts": len(opened),
            "detected": bool(correct),
            "conditions_alerted": sorted({a["condition"] for a in opened}),
            "hours_after_onset": None if lead_hours is None else round(lead_hours, 2),
            "days": round(len(animal) / 1440, 2),
        }

    return {
        "per_animal": per_animal,
        "naive_alerts": naive_alerts,
        "policy_alerts": policy_alerts,
    }


def main() -> None:
    raw_path = RAW_DIR / "telemetry.parquet"
    if not raw_path.exists():
        raise SystemExit(f"{raw_path} missing; run `python -m vetra_ml.make_dataset` first")

    raw = pd.read_parquet(raw_path)
    test_animals = sorted(make_split().meta_test.animal_id.unique())
    print(f"Replaying {len(test_animals)} held-out animals through the full edge pipeline ...\n")

    result = replay(raw, test_animals)
    per_animal = result["per_animal"]
    frame = pd.DataFrame(per_animal).T

    sick = frame[frame.true_condition != "healthy"]
    healthy = frame[frame.true_condition == "healthy"]
    total_days = float(frame["days"].sum())

    detected = int(sick["detected"].sum())
    sensitivity = detected / len(sick) if len(sick) else 0.0
    false_positive_animals = int((healthy["policy_alerts"] > 0).sum())

    lead = [v for v in sick["hours_after_onset"] if v is not None]
    median_lead = float(np.median(lead)) if lead else float("nan")

    print(f"  animals               {len(frame)}  ({len(sick)} sick, {len(healthy)} healthy)")
    print(f"  monitored animal-days {total_days:,.0f}\n")

    print("  Alert burden")
    print(f"    naive  (every non-healthy window) {result['naive_alerts']:>6,} alerts"
          f"   = {result['naive_alerts'] / total_days:6.2f} per animal-day")
    print(f"    policy (confirm + hysteresis)     {result['policy_alerts']:>6,} alerts"
          f"   = {result['policy_alerts'] / total_days:6.2f} per animal-day")
    reduction = 1 - result["policy_alerts"] / max(result["naive_alerts"], 1)
    print(f"    reduction                         {reduction:>6.1%}\n")

    print("  Did it catch the sick animals?")
    print(f"    detected with the correct condition   {detected}/{len(sick)}  ({sensitivity:.1%})")
    print(f"    healthy animals that got any alert    {false_positive_animals}/{len(healthy)}"
          f"  ({false_positive_animals / max(len(healthy), 1):.1%})")
    print(f"    median time from onset to alert       {median_lead:.2f} h")
    print("      (negative = alerted before severity crossed the labelling threshold)\n")

    print("  By condition")
    print("    condition             animals  detected   median h to alert")
    for condition, group in sick.groupby("true_condition"):
        hits = int(group["detected"].sum())
        hours = [v for v in group["hours_after_onset"] if v is not None]
        shown = f"{np.median(hours):.2f}" if hours else "  n/a"
        print(f"    {condition:<22} {len(group):>5}  {hits:>5}/{len(group):<4}  {shown:>10}")

    summary = {
        "animals": len(frame),
        "sick_animals": len(sick),
        "healthy_animals": len(healthy),
        "monitored_animal_days": round(total_days, 1),
        "naive_alerts": result["naive_alerts"],
        "policy_alerts": result["policy_alerts"],
        "naive_alerts_per_animal_day": round(result["naive_alerts"] / total_days, 3),
        "policy_alerts_per_animal_day": round(result["policy_alerts"] / total_days, 3),
        "alert_reduction": round(reduction, 4),
        "sensitivity": round(sensitivity, 4),
        "healthy_animals_with_any_alert": false_positive_animals,
        "median_hours_onset_to_alert": None if np.isnan(median_lead) else round(median_lead, 2),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "alert_evaluation.json").write_text(
        json.dumps({"summary": summary, "per_animal": per_animal}, indent=2)
    )
    print(f"\n  Saved {REPORT_DIR / 'alert_evaluation.json'}")


if __name__ == "__main__":
    main()
