# =============================================================================
# JARVIS HA Memory Service — Dockerfile
# =============================================================================
# Dual-purpose:
#   1. HAOS Add-on: uses ghcr.io/home-assistant base image (bashio, S6, etc.)
#   2. Standalone Docker: works with standard python:3.11-slim
#
# Build args:
#   BUILD_FROM — base image (set by HA build system or manually)
# =============================================================================

ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.21
FROM ${BUILD_FROM}

WORKDIR /app

# Build tools for native extensions (hnswlib, grpcio, etc.)
RUN apk add --no-cache --virtual .build-deps \
    gcc g++ musl-dev python3-dev cmake make linux-headers libffi-dev \
    && apk add --no-cache libstdc++

# Install Python dependencies
# ChromaDB depends on onnxruntime which has NO Alpine/musl wheel.
# We don't need it — we provide our own embedding function (Ollama/Gemini).
# Fix: create a stub onnxruntime package to satisfy pip, then install normally.
RUN mkdir -p /tmp/onnxstub/onnxruntime \
    && echo "# stub — not needed, embeddings via Ollama/Gemini" > /tmp/onnxstub/onnxruntime/__init__.py \
    && printf '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n[project]\nname = "onnxruntime"\nversion = "999.0.0"\n' > /tmp/onnxstub/pyproject.toml \
    && pip install --no-cache-dir /tmp/onnxstub \
    && rm -rf /tmp/onnxstub
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cleanup build tools (keep libstdc++ runtime)
RUN apk del .build-deps \
    && rm -rf /root/.cache /tmp/*

# Copy application
COPY main.py .
COPY prompts/ prompts/
COPY run.sh /

# Make entrypoint executable
RUN chmod +x /run.sh

# Environment defaults (overridden by addon options or docker -e)
ENV LOCATION_ID=unknown
ENV AI_BACKEND=local
ENV HA_URL=http://supervisor/core
ENV HA_TOKEN=""
ENV OLLAMA_URL=http://localhost:11434
ENV DB_PATH=/data/ha_memory.db
ENV CHROMA_PATH=/data/chroma
ENV SERVICE_PORT=8100

VOLUME /data
EXPOSE 8100

# Override ENTRYPOINT della base image (rimuove /init di s6-overlay)
# Con init: false nel config.yaml, tini (PID 1) lancia direttamente /run.sh
ENTRYPOINT []
CMD ["/run.sh"]
