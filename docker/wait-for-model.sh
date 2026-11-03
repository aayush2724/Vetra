#!/bin/sh
# Hold the gateway until a trained model exists, then start it.
#
# The model is bind-mounted from ./artifacts rather than baked into the image,
# so a fresh clone has none until the trainer has run. Waiting with a clear
# message beats a crash loop that buries the reason in the logs.
set -eu

MODEL="${VETRA_ARTIFACT_DIR:-/app/artifacts}/models/vetra_dx_edge.tflite"

until [ -f "$MODEL" ]; do
  echo "gateway: waiting for a trained model at $MODEL  (run: make docker-train)"
  sleep 10
done

exec "$@"
