# =============================================================================
# Ontology Graph API Server
# Knowledge Graph for AI Agents — SQLite + FastAPI + ACL
# =============================================================================
FROM python:3.11-slim

LABEL maintainer="WAGMI Villa"
LABEL description="Ontology Graph API - Knowledge Graph for AI Agents"

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY api.py ontology.py helpers.py schema.yaml ./

# Copy dashboard static files
COPY static/ /app/static/

# Create data directory for SQLite (will be mounted as volume)
RUN mkdir -p /app/data

# Default paths (overridable via env)
ENV ONTOLOGY_DB_PATH=/app/data/graph.db
ENV ONTOLOGY_SCHEMA_PATH=/app/schema.yaml

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/health')" || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8100"]
