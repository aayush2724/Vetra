"""Generate the raw herd telemetry and the windowed training matrix.

Run:  python -m vetra_ml.make_dataset --animals 240 --days 5
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from .config import PROCESSED_DIR, RANDOM_SEED, RAW_DIR, ensure_dirs
from .features import build_windows, feature_names
from .synth.generator import generate_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Vetra training dataset")
    parser.add_argument("--animals", type=int, default=240)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    parser.add_argument("--healthy-fraction", type=float, default=0.35)
    args = parser.parse_args()

    ensure_dirs()

    t0 = time.perf_counter()
    print(f"Simulating {args.animals} animals x {args.days} days ...")
    raw = generate_dataset(
        n_animals=args.animals,
        days=args.days,
        seed=args.seed,
        healthy_fraction=args.healthy_fraction,
    )
    raw_path = RAW_DIR / "telemetry.parquet"
    raw.to_parquet(raw_path, index=False)
    print(f"  raw samples : {len(raw):,} -> {raw_path}")

    print("Building rolling windows ...")
    X, y, meta = build_windows(raw)
    print(f"  windows     : {len(X):,}  features: {X.shape[1]}")

    features_df = pd.DataFrame(X, columns=feature_names())
    features_df["label"] = y
    for column in ("animal_id", "species", "window_end", "severity", "true_condition", "has_baseline"):
        features_df[column] = meta[column].to_numpy()

    out_path = PROCESSED_DIR / "windows.parquet"
    features_df.to_parquet(out_path, index=False)
    print(f"  written     : {out_path}")

    counts = pd.Series(y).value_counts().sort_index()
    print("\nWindow label distribution")
    for label, count in counts.items():
        print(f"  {label:<22} {count:>7,}  ({count / len(y):6.1%})")

    summary = {
        "animals": int(args.animals),
        "days": int(args.days),
        "seed": int(args.seed),
        "raw_samples": int(len(raw)),
        "windows": int(len(X)),
        "n_features": int(X.shape[1]),
        "label_counts": {str(k): int(v) for k, v in counts.items()},
        "generated_seconds": round(time.perf_counter() - t0, 1),
    }
    (PROCESSED_DIR / "dataset_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nDone in {summary['generated_seconds']}s")


if __name__ == "__main__":
    main()
