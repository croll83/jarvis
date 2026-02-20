FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# System deps: libopus for opuslib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 libopus-dev && rm -rf /var/lib/apt/lists/*

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download openWakeWord models at build time (avoid runtime download)
RUN python -c "import openwakeword; openwakeword.utils.download_models()"

# Application code
COPY *.py .

# Data directory (for future model cache etc.)
RUN mkdir -p /app/data

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8200"]
