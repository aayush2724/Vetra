# syntax=docker/dockerfile:1.7
# Training image: the full ML toolchain, TensorFlow included. It generates the
# dataset, trains the models, exports the TFLite file and runs the evaluation
# and the test suite. Nothing from this image is deployed; the gateway image is
# the one that ships to a device.
#
#   docker build -f docker/ml.Dockerfile -t vetra/ml .
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TF_CPP_MIN_LOG_LEVEL=3 \
    KERAS_HOME=/tmp/.keras \
    VETRA_ARTIFACT_DIR=/app/artifacts \
    VETRA_DATA_DIR=/app/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY ml/ ./ml/
COPY edge/ ./edge/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY docker/train.sh ./train.sh
RUN chmod +x train.sh \
    && useradd --uid 1000 --create-home vetra \
    && mkdir -p data artifacts \
    && chown -R vetra:vetra /app
USER vetra
CMD ["./train.sh"]
