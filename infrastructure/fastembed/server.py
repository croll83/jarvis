# =============================================================================
# JARVIS Fastembed Server — Ollama-compatible embedding API (CPU-only, ONNX)
# =============================================================================
#
# Drop-in replacement for Ollama's nomic-embed-text endpoint.
# Eliminates CUDA context switching that causes ~1.3s latency penalty
# when Ollama switches between Qwen (LLM) and nomic (embeddings).
#
# API endpoints (Ollama-compatible):
#   POST /api/embeddings  {"model":"...","prompt":"text"}     -> {"embedding":[...]}
#   POST /api/embed       {"model":"...","input":["a","b"]}   -> {"embeddings":[[...],[...]]}
#   GET  /api/tags        -> {"models":[...]}  (healthcheck)
#
# =============================================================================

import os
import logging
from typing import Union

from fastapi import FastAPI
from pydantic import BaseModel
from fastembed import TextEmbedding
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FASTEMBED")

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "nomic-ai/nomic-embed-text-v1.5")
PORT = int(os.getenv("PORT", "11435"))

app = FastAPI(title="JARVIS Fastembed Server")
model: TextEmbedding = None


# --- Request models (Ollama-compatible) ---

class EmbeddingsRequest(BaseModel):
    """Ollama /api/embeddings format (single text)."""
    model: str = "nomic-embed-text"
    prompt: str = ""

class EmbedRequest(BaseModel):
    """Ollama /api/embed format (batch)."""
    model: str = "nomic-embed-text"
    input: Union[str, list[str]] = ""


# --- Lifecycle ---

@app.on_event("startup")
def load_model():
    global model
    logger.info(f"Loading embedding model: {MODEL_NAME}")
    model = TextEmbedding(model_name=MODEL_NAME)
    # Warmup — first call is slower due to ONNX session init
    list(model.embed(["warmup"]))
    logger.info(f"Model loaded and warmed up (768-dim, ONNX CPU)")


# --- Endpoints ---

@app.post("/api/embeddings")
def embeddings(req: EmbeddingsRequest):
    """Ollama-compatible single embedding endpoint.
    Used by: orchestrator (vector_store.py), ha_memory_service (main.py).
    """
    text = req.prompt or ""
    if not text.strip():
        return {"embedding": [0.0] * 768}
    result = list(model.embed([text]))
    return {"embedding": result[0].tolist()}


@app.post("/api/embed")
def embed(req: EmbedRequest):
    """Ollama-compatible batch embedding endpoint."""
    texts = req.input if isinstance(req.input, list) else [req.input or ""]
    texts = [t for t in texts if t.strip()] or [""]
    results = list(model.embed(texts))
    return {
        "model": req.model,
        "embeddings": [r.tolist() for r in results],
    }


@app.get("/api/tags")
def tags():
    """Healthcheck endpoint (mimics Ollama /api/tags)."""
    return {
        "models": [{
            "name": "nomic-embed-text",
            "model": MODEL_NAME,
            "size": 0,
            "details": {"family": "nomic", "parameter_size": "137M", "quantization_level": "ONNX"},
        }]
    }


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
