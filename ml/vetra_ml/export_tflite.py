"""Convert the trained network to TFLite variants and verify each one.

A conversion that changes the model's answers is worse than no conversion at
all, so nothing is written here without measuring it: every variant is run
against the same held-out test set and reported with its size, its agreement
with the original Keras model, and its per-inference latency.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import numpy as np
import tensorflow as tf

from .config import MODEL_DIR, RANDOM_SEED, REPORT_DIR, ensure_dirs
from .data import make_split
from .evaluate import evaluate, save_report
from .inference.runtime import load_interpreter


def _converter(model: tf.keras.Model, export_dir: Path) -> tf.lite.TFLiteConverter:
    """Build a converter, going via SavedModel when the direct path is unavailable."""
    try:
        return tf.lite.TFLiteConverter.from_keras_model(model)
    except Exception:
        # Keras 3 models convert via SavedModel; silence the endpoint dump.
        with contextlib.redirect_stdout(io.StringIO()):
            model.export(str(export_dir))
        return tf.lite.TFLiteConverter.from_saved_model(str(export_dir))


def convert_float32(model: tf.keras.Model, export_dir: Path) -> bytes:
    return _converter(model, export_dir / "fp32").convert()


def convert_dynamic_int8(model: tf.keras.Model, export_dir: Path) -> bytes:
    """Weights quantised to int8, activations left float. ~4x smaller, no calibration data."""
    converter = _converter(model, export_dir / "dyn")
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    return converter.convert()


def convert_full_int8(model: tf.keras.Model, export_dir: Path, sample: np.ndarray) -> bytes:
    """Weights and activations int8, calibrated on real feature vectors.

    Inputs and outputs stay float32 so the edge runtime can keep feeding plain
    feature arrays; the converter inserts the quantise/dequantise pair itself.
    """
    converter = _converter(model, export_dir / "int8")
    converter.optimizations = [tf.lite.Optimize.DEFAULT]

    def representative_dataset():
        for row in sample:
            yield [row.reshape(1, -1).astype(np.float32)]

    converter.representative_dataset = representative_dataset
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    return converter.convert()


def run_tflite(model_bytes: bytes, X: np.ndarray) -> tuple[np.ndarray, float]:
    """Run every row through the interpreter one at a time, as the device would.

    Batch inference would understate latency: on the edge, windows arrive one
    animal at a time, so a single-row call is the honest unit to measure.
    """
    interpreter = load_interpreter(model_bytes)
    in_detail = interpreter.get_input_details()[0]
    out_detail = interpreter.get_output_details()[0]

    predictions = np.empty(len(X), dtype=np.int64)
    t0 = time.perf_counter()
    for i, row in enumerate(X):
        interpreter.set_tensor(in_detail["index"], row.reshape(1, -1).astype(in_detail["dtype"]))
        interpreter.invoke()
        predictions[i] = int(interpreter.get_tensor(out_detail["index"])[0].argmax())
    elapsed_ms = (time.perf_counter() - t0) * 1000.0 / max(len(X), 1)
    return predictions, elapsed_ms


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Vetra models to TFLite")
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    parser.add_argument("--latency-samples", type=int, default=2000,
                        help="rows used for the latency measurement")
    args = parser.parse_args()

    ensure_dirs()
    keras_path = MODEL_DIR / "vetra_dx.keras"
    if not keras_path.exists():
        raise FileNotFoundError(f"{keras_path} missing. Run `python -m vetra_ml.train_keras` first.")

    model = tf.keras.models.load_model(keras_path)
    split = make_split(seed=args.seed)

    keras_pred = model.predict(split.X_test, verbose=0).argmax(axis=1)
    keras_report = evaluate("keras (reference)", split.y_test, keras_pred, split.meta_test)
    print(f"Keras reference macro-F1 {keras_report.macro_f1:.4f}")

    rng = np.random.default_rng(args.seed)
    calibration = split.X_train[rng.choice(len(split.X_train), size=500, replace=False)]

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        export_dir = Path(tmp)
        variants = {
            "float32": lambda: convert_float32(model, export_dir),
            "dynamic_int8": lambda: convert_dynamic_int8(model, export_dir),
            "full_int8": lambda: convert_full_int8(model, export_dir, calibration),
        }

        for name, convert in variants.items():
            print(f"\nConverting {name} ...")
            try:
                blob = convert()
            except Exception as exc:  # a failed variant must not lose the others
                print(f"  conversion failed: {type(exc).__name__}: {exc}")
                continue

            out_path = MODEL_DIR / f"vetra_dx_{name}.tflite"
            out_path.write_bytes(blob)

            preds, latency_ms = run_tflite(blob, split.X_test[: args.latency_samples])
            full_preds, _ = run_tflite(blob, split.X_test)

            agreement = float((full_preds == keras_pred).mean())
            report = evaluate(f"tflite_{name}", split.y_test, full_preds, split.meta_test)

            size_kb = len(blob) / 1024
            print(f"  size {size_kb:7.1f} KB   macro-F1 {report.macro_f1:.4f}"
                  f"   agreement with Keras {agreement:.4f}   {latency_ms:.3f} ms/inference")

            save_report(report, REPORT_DIR / f"tflite_{name}_report.json")
            results.append({
                "variant": name,
                "path": str(out_path.relative_to(MODEL_DIR.parents[1])),
                "size_kb": round(size_kb, 1),
                "macro_f1": round(report.macro_f1, 4),
                "screening_recall": round(report.screening_recall, 4),
                "agreement_with_keras": round(agreement, 4),
                "ms_per_inference": round(latency_ms, 4),
            })

    if not results:
        raise SystemExit("no TFLite variant converted successfully")

    # Ship the smallest variant that still tracks the float model closely.
    # Losing more than 1% agreement is not worth any size saving here.
    acceptable = [r for r in results if r["agreement_with_keras"] >= 0.99] or results
    chosen = min(acceptable, key=lambda r: r["size_kb"])
    deploy_path = MODEL_DIR / "vetra_dx_edge.tflite"
    deploy_path.write_bytes((MODEL_DIR / f"vetra_dx_{chosen['variant']}.tflite").read_bytes())

    print(f"\nSelected '{chosen['variant']}' for deployment -> {deploy_path.name}"
          f"  ({chosen['size_kb']} KB, {chosen['ms_per_inference']} ms/inference)")

    (REPORT_DIR / "tflite_comparison.json").write_text(
        json.dumps({"keras_macro_f1": round(keras_report.macro_f1, 4),
                    "variants": results,
                    "deployed": chosen["variant"]}, indent=2)
    )
    print(f"Saved {REPORT_DIR / 'tflite_comparison.json'}")


if __name__ == "__main__":
    main()
