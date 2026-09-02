"""Central paths and constants.

Everything downstream resolves file locations through here so the pipeline can
be relocated (e.g. onto an edge device) by changing one file.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repository root = two levels above this file (ml/vetra_ml/config.py)
ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("VETRA_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

ARTIFACT_DIR = Path(os.environ.get("VETRA_ARTIFACT_DIR", ROOT / "artifacts"))
MODEL_DIR = ARTIFACT_DIR / "models"
REPORT_DIR = ARTIFACT_DIR / "reports"

# --- Sampling / windowing ------------------------------------------------
# Sensor collars report once a minute; diagnosis runs on a rolling window.
SAMPLE_INTERVAL_MIN = 1
WINDOW_MINUTES = 60          # one hour of context per diagnostic decision
WINDOW_STRIDE_MINUTES = 30   # emit a decision every 30 min (50% overlap)

SAMPLES_PER_WINDOW = WINDOW_MINUTES // SAMPLE_INTERVAL_MIN

RANDOM_SEED = 42


def ensure_dirs() -> None:
    """Create every output directory the pipeline writes into."""
    for d in (RAW_DIR, PROCESSED_DIR, MODEL_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
