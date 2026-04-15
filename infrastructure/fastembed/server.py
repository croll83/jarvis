# =============================================================================
# JARVIS Fastembed Server — Multi-format embedding API (CPU-only, ONNX)
# =============================================================================
#
# Drop-in replacement for Ollama's nomic-embed-text endpoint.
# Eliminates CUDA context switching that causes ~1.3s latency penalty
# when Ollama switches between Qwen (LLM) and nomic (embeddings).
#
# API endpoints:
#   Ollama format:
#     POST /api/embeddings  {"model":"...","prompt":"text"}     -> {"embedding":[...]}
#     POST /api/embed       {"model":"...","input":["a","b"]}   -> {"embeddings":[[...],[...]]}
#     GET  /api/tags        -> {"models":[...]}  (healthcheck)
#   OpenAI format:
#     POST /v1/embeddings   {"model":"...","input":"text"|[...]} -> {"data":[{"embedding":[...]}]}
#
# Usato da:
#   - Orchestrator (vector_store.py) — formato Ollama
#   - ha_memory_service (main.py)    — formato Ollama
#   - AI Agent (memorySearch)         — formato OpenAI
# =============================================================================

import os
import logging
import time
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


# --- Request models ---

class EmbeddingsRequest(BaseModel):
    """Ollama /api/embeddings format (single text)."""
    model: str = "nomic-embed-text"
    prompt: str = ""

class EmbedRequest(BaseModel):
    """Ollama /api/embed format (batch)."""
    model: str = "nomic-embed-text"
    input: Union[str, list[str]] = ""

class OpenAIEmbeddingsRequest(BaseModel):
    """OpenAI /v1/embeddings format (single or batch)."""
    model: str = "nomic-embed-text"
    input: Union[str, list[str]] = ""
    encoding_format: str = "float"


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


@app.post("/v1/embeddings")
def openai_embeddings(req: OpenAIEmbeddingsRequest):
    """OpenAI-compatible embedding endpoint.
    Used by: AI Agent (memorySearch, provider=openai, baseUrl=http://host:11435/v1).
    """
    texts = req.input if isinstance(req.input, list) else [req.input or ""]
    texts = [t if t.strip() else " " for t in texts]
    results = list(model.embed(texts))
    return {
        "object": "list",
        "model": req.model,
        "data": [
            {"object": "embedding", "index": i, "embedding": r.tolist()}
            for i, r in enumerate(results)
        ],
        "usage": {"prompt_tokens": sum(len(t.split()) for t in texts), "total_tokens": sum(len(t.split()) for t in texts)},
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
