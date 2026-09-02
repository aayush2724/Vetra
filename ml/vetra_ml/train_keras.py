"""Train the compact neural network that ships to the edge device.

Two choices here exist specifically to make deployment simple:

* **Normalisation is a layer inside the model**, adapted on the training set.
  The exported .tflite file is therefore self-contained — there is no separate
  scaler file to keep in sync with it, which is the usual way edge deployments
  silently break.
* **The network is deliberately small** (two hidden layers). It has to run on a
  Raspberry-Pi-class board every 30 minutes per animal, and on this feature set
  a larger network buys nothing over the classical baseline anyway.
"""
from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import numpy as np
import tensorflow as tf

from .config import MODEL_DIR, RANDOM_SEED, REPORT_DIR, ensure_dirs
from .data import describe_split, make_split
from .evaluate import evaluate, print_confusion, print_report, save_report
from .features import feature_names
from .schema import CONDITIONS


def build_model(n_features: int, n_classes: int, X_train: np.ndarray) -> tf.keras.Model:
    normalizer = tf.keras.layers.Normalization(axis=-1, name="normalize")
    normalizer.adapt(X_train)

    return tf.keras.Sequential(
        [
            tf.keras.layers.Input(shape=(n_features,), name="features"),
            normalizer,
            tf.keras.layers.Dense(64, activation="relu"),
            tf.keras.layers.Dropout(0.20),
            tf.keras.layers.Dense(32, activation="relu"),
            tf.keras.layers.Dropout(0.10),
            tf.keras.layers.Dense(n_classes, activation="softmax", name="diagnosis"),
        ],
        name="vetra_dx",
    )


def class_weights(y: np.ndarray, n_classes: int) -> dict[int, float]:
    """Inverse-frequency weights so rare diseases are not drowned out."""
    counts = np.bincount(y, minlength=n_classes).astype(np.float64)
    counts[counts == 0] = 1.0
    weights = counts.sum() / (n_classes * counts)
    return {i: float(w) for i, w in enumerate(weights)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the Vetra edge neural network")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    ensure_dirs()
    tf.keras.utils.set_random_seed(args.seed)

    split = make_split(seed=args.seed)
    print("Dataset")
    print(describe_split(split))

    n_classes = len(CONDITIONS)
    model = build_model(split.n_features, n_classes, split.X_train)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(1e-3),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )
    model.summary()

    callbacks = [
        tf.keras.callbacks.EarlyStopping(
            monitor="val_loss", patience=12, restore_best_weights=True, verbose=1
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss", factor=0.5, patience=5, min_lr=1e-5, verbose=0
        ),
    ]

    print("\nTraining ...")
    history = model.fit(
        split.X_train, split.y_train,
        validation_data=(split.X_val, split.y_val),
        epochs=args.epochs,
        batch_size=args.batch_size,
        class_weight=class_weights(split.y_train, n_classes),
        callbacks=callbacks,
        verbose=2,
    )

    print("\nHeld-out test set (animals never seen in training)")
    y_pred = model.predict(split.X_test, verbose=0).argmax(axis=1)
    report = evaluate("keras_mlp (test)", split.y_test, y_pred, split.meta_test)
    print_report(report)
    print_confusion(report)

    keras_path = MODEL_DIR / "vetra_dx.keras"
    model.save(keras_path)
    print(f"\nSaved {keras_path}  ({keras_path.stat().st_size / 1024:,.0f} KB)")

    save_report(report, REPORT_DIR / "keras_test_report.json")
    (REPORT_DIR / "keras_history.json").write_text(
        json.dumps({k: [float(x) for x in v] for k, v in history.history.items()}, indent=2)
    )
    (MODEL_DIR / "feature_names.json").write_text(
        json.dumps({"features": feature_names(), "classes": list(CONDITIONS)}, indent=2)
    )
    print("Wrote feature/class manifest for the edge runtime")


if __name__ == "__main__":
    main()
