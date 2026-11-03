# syntax=docker/dockerfile:1.7
# Edge gateway image: the process that runs on the farm's Raspberry-Pi-class
# board. It carries the TFLite runtime (ai-edge-litert) and not TensorFlow, so
# it is a few hundred megabytes rather than a few gigabytes. The same image
# runs the collar simulator.
#
# The trained model is not baked in. Mount ./artifacts at /app/artifacts, as
# docker-compose.yml does, so a retrained model is picked up on the next start
# without rebuilding the image.
#
#   docker build -f docker/edge.Dockerfile -t vetra/edge .
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VETRA_ARTIFACT_DIR=/app/artifacts \
    VETRA_DATA_DIR=/app/data
WORKDIR /app
COPY requirements-edge.txt ./
RUN pip install -r requirements-edge.txt
COPY ml/ ./ml/
COPY edge/ ./edge/
COPY docker/wait-for-model.sh ./wait-for-model.sh
RUN chmod +x wait-for-model.sh \
    && useradd --uid 1000 --create-home vetra \
    && mkdir -p /data artifacts/models \
    && chown -R vetra:vetra /app /data
USER vetra
EXPOSE 5001
HEALTHCHECK --interval=5s --timeout=3s --start-period=30s --retries=5 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=3).status == 200 else 1)"
CMD ["./wait-for-model.sh", "python", "edge/agent.py", "--http-port", "5001", "--db", "/data/vetra_edge.db"]
