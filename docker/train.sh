#!/bin/sh
# The whole ML pipeline, in the order the Makefile runs it: dataset, classical
# baselines, neural network, TFLite export, then the alert-policy evaluation.
# Outputs land in /app/data and /app/artifacts, which docker-compose.yml binds
# to ./data and ./artifacts on the host.
set -eu

cd /app/ml
echo "==> simulating ${TRAIN_ANIMALS:-240} animals over ${TRAIN_DAYS:-5} days"
python -m vetra_ml.make_dataset --animals "${TRAIN_ANIMALS:-240}" --days "${TRAIN_DAYS:-5}"

echo "==> classical baselines"
python -m vetra_ml.train_sklearn

echo "==> neural network"
python -m vetra_ml.train_keras

echo "==> TFLite export"
python -m vetra_ml.export_tflite

cd /app
echo "==> alert-policy evaluation on held-out animals"
python scripts/evaluate_alerts.py
