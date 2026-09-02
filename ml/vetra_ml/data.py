"""Load the windowed dataset and split it honestly.

The split is grouped by `animal_id`. Windows overlap by 50% and consecutive
windows from one animal are near-duplicates, so a plain random split would put
almost-identical rows on both sides and report accuracy that collapses the
moment the system meets an animal it has never seen. Grouping by animal is the
only split that answers the question the farmer actually cares about.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

from .config import PROCESSED_DIR, RANDOM_SEED
from .features import feature_names
from .schema import CONDITIONS, CONDITION_TO_ID

META_COLUMNS = (
    "label", "animal_id", "species", "window_end",
    "severity", "true_condition", "has_baseline",
)


@dataclass(slots=True)
class Split:
    X_train: np.ndarray
    y_train: np.ndarray
    X_val: np.ndarray
    y_val: np.ndarray
    X_test: np.ndarray
    y_test: np.ndarray
    meta_train: pd.DataFrame
    meta_val: pd.DataFrame
    meta_test: pd.DataFrame

    @property
    def n_features(self) -> int:
        return self.X_train.shape[1]


def load_windows(path=None) -> pd.DataFrame:
    path = path or (PROCESSED_DIR / "windows.parquet")
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python -m vetra_ml.make_dataset` first."
        )
    return pd.read_parquet(path)


def _group_split(df: pd.DataFrame, test_size: float, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    left, right = next(splitter.split(df, groups=df["animal_id"]))
    return df.iloc[left].reset_index(drop=True), df.iloc[right].reset_index(drop=True)


def make_split(
    df: pd.DataFrame | None = None,
    test_size: float = 0.20,
    val_size: float = 0.15,
    seed: int = RANDOM_SEED,
) -> Split:
    """Split into train/val/test with no animal appearing in more than one."""
    df = load_windows() if df is None else df
    columns = feature_names()

    trainval, test = _group_split(df, test_size, seed)
    # val_size is a fraction of the whole dataset, so rescale it for this subset.
    train, val = _group_split(trainval, val_size / (1.0 - test_size), seed)

    leaked = (set(train.animal_id) | set(val.animal_id)) & set(test.animal_id)
    assert not leaked, f"animal leaked across splits: {sorted(leaked)[:5]}"

    def xy(part: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        X = part[columns].to_numpy(dtype=np.float32)
        y = np.asarray([CONDITION_TO_ID[label] for label in part["label"]], dtype=np.int64)
        return X, y

    X_train, y_train = xy(train)
    X_val, y_val = xy(val)
    X_test, y_test = xy(test)

    return Split(
        X_train, y_train, X_val, y_val, X_test, y_test,
        train[list(META_COLUMNS)], val[list(META_COLUMNS)], test[list(META_COLUMNS)],
    )


def describe_split(split: Split) -> str:
    lines = ["  split          windows   animals"]
    for name, X, meta in (
        ("train", split.X_train, split.meta_train),
        ("val", split.X_val, split.meta_val),
        ("test", split.X_test, split.meta_test),
    ):
        lines.append(f"  {name:<12} {len(X):>7,}   {meta.animal_id.nunique():>6}")
    return "\n".join(lines)


def class_names() -> list[str]:
    return list(CONDITIONS)
