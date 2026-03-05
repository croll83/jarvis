# =============================================================================
# JARVIS Fastembed — CPU-only embedding server (nomic-embed-text-v1.5, ONNX)
# =============================================================================
#
# Replaces nomic-embed-text on Ollama GPU to eliminate CUDA context switching.
# Exposes Ollama-compatible API on port 11435.
#
# Build: docker compose build fastembed
# Size:  ~800 MB (Python 3.12 + fastembed + ONNX model pre-downloaded)
# RAM:   ~300 MB at runtime
# CPU:   minimal (embedding takes ~30-60ms per query on modern CPU)
# =============================================================================

FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download model at build time (avoids download at runtime)
# nomic-ai/nomic-embed-text-v1.5: 768-dim, ~260 MB ONNX
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('nomic-ai/nomic-embed-text-v1.5')"

# Copy server
COPY server.py .

ENV EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5
ENV PORT=11435

EXPOSE 11435

CMD ["python", "server.py"]
