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

# Build tools for native extensions
RUN apk add --no-cache --virtual .build-deps \
    gcc g++ musl-dev python3-dev cmake make linux-headers libffi-dev \
    && apk add --no-cache libstdc++

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cleanup build tools (keep libstdc++ runtime)
RUN apk del .build-deps \
    && rm -rf /root/.cache /tmp/*

# Copy application
COPY main.py .
COPY context_bus.py .
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
ENV REDIS_URL=""
ENV MEM0_BASE_URL=""
ENV SERVICE_PORT=8100

VOLUME /data
EXPOSE 8100

# Override ENTRYPOINT della base image (rimuove /init di s6-overlay)
# Con init: false nel config.yaml, tini (PID 1) lancia direttamente /run.sh
ENTRYPOINT []
CMD ["/run.sh"]
