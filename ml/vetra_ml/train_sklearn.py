"""Train and compare classical baselines, then persist the best one.

These models are the reference the neural network has to beat, and the fallback
the edge agent uses when no TFLite runtime is present. Class weights are
balanced throughout: healthy windows outnumber every disease, and an unweighted
fit converges on the useless habit of calling everything healthy.
"""
from __future__ import annotations

import argparse
import json
import time

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

from .config import MODEL_DIR, RANDOM_SEED, REPORT_DIR, ensure_dirs
from .data import make_split, describe_split
from .evaluate import evaluate, print_confusion, print_report, save_report
from .features import feature_names
from .schema import CONDITIONS


def build_models(seed: int) -> dict[str, object]:
    return {
        # Linear reference point: tells us how much of the signal is simply
        # "this channel is high", before any interaction terms.
        "logistic_regression": Pipeline(
            [
                ("scale", StandardScaler()),
                # sklearn >=1.7 fits multinomial softmax by default for
                # multi-class targets; the old multi_class= argument is gone.
                ("clf", LogisticRegression(
                    max_iter=2000, class_weight="balanced", random_state=seed,
                )),
            ]
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=300, min_samples_leaf=2, class_weight="balanced_subsample",
            random_state=seed, n_jobs=-1,
        ),
        "gradient_boosting": HistGradientBoostingClassifier(
            max_iter=400, learning_rate=0.08, max_leaf_nodes=31,
            early_stopping=True, validation_fraction=0.12,
            random_state=seed,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train classical Vetra baselines")
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    ensure_dirs()
    split = make_split(seed=args.seed)
    print("Dataset")
    print(describe_split(split))
    print(f"  features     {split.n_features}")

    results = []
    fitted: dict[str, object] = {}

    for name, model in build_models(args.seed).items():
        print(f"\nTraining {name} ...")
        t0 = time.perf_counter()

        if isinstance(model, HistGradientBoostingClassifier):
            # HistGB has no class_weight parameter; weight the samples instead.
            weights = compute_sample_weight("balanced", split.y_train)
            model.fit(split.X_train, split.y_train, sample_weight=weights)
        else:
            model.fit(split.X_train, split.y_train)

        train_seconds = time.perf_counter() - t0
        report = evaluate(name, split.y_val, model.predict(split.X_val), split.meta_val)
        print(f"  fitted in {train_seconds:.1f}s   val macro-F1 {report.macro_f1:.3f}"
              f"   screening recall {report.screening_recall:.3f}")

        fitted[name] = model
        results.append((name, report.macro_f1, train_seconds))

    # Selection is on validation macro-F1: it weights every disease equally, so
    # a model cannot win by being excellent at the majority healthy class.
    best_name = max(results, key=lambda r: r[1])[0]
    best_model = fitted[best_name]
    print(f"\nBest on validation: {best_name}")

    print("\nHeld-out test set (animals never seen in training)")
    test_report = evaluate(f"{best_name} (test)", split.y_test, best_model.predict(split.X_test), split.meta_test)
    print_report(test_report)
    print_confusion(test_report)

    model_path = MODEL_DIR / "sklearn_model.joblib"
    joblib.dump(
        {
            "model": best_model,
            "model_name": best_name,
            "feature_names": feature_names(),
            "classes": list(CONDITIONS),
            "seed": args.seed,
        },
        model_path,
    )
    size_kb = model_path.stat().st_size / 1024
    print(f"\nSaved {model_path}  ({size_kb:,.0f} KB)")

    save_report(test_report, REPORT_DIR / "sklearn_test_report.json")
    (REPORT_DIR / "sklearn_model_comparison.json").write_text(
        json.dumps(
            [{"model": n, "val_macro_f1": round(f, 4), "train_seconds": round(s, 1)} for n, f, s in results],
            indent=2,
        )
    )
    print(f"Saved reports to {REPORT_DIR}")


if __name__ == "__main__":
    main()
