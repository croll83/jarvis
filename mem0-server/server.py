"""
Mem0 REST API Server for JARVIS with Async Queue + Raw Insert + Contextual Search
Exposes Mem0 memory operations via HTTP.
v2: adds /add_raw (bypass mem0 pipeline), /search_contextual (with 7B summary),
    background graph extraction, TTL cleanup.
"""

import os
import logging
import sqlite3
import threading
import time
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from openai import OpenAI
from mem0 import Memory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mem0-server")

CHROMA_HOST = os.getenv("CHROMA_HOST", "127.0.0.1")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
EMBED_URL = os.getenv("EMBED_URL", "http://127.0.0.1:11435/v1")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
EMBED_DIMS = int(os.getenv("EMBED_DIMS", "768"))
LLM_URL = os.getenv("LLM_URL", "http://127.0.0.1:30000/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-7b")
GRAPH_LLM_URL = os.getenv("GRAPH_LLM_URL", "http://127.0.0.1:30000/v1")
GRAPH_LLM_MODEL = os.getenv("GRAPH_LLM_MODEL", "qwen2.5-7b")
KUZU_PATH = os.getenv("KUZU_PATH", "/data/kuzu/db")

# ─── Custom Italian prompts ───────────────────────────────────────────────

CUSTOM_FACT_EXTRACTION_PROMPT = (
    "Sei un organizzatore di informazioni personali. Il tuo compito è estrarre "
    "fatti rilevanti dalle conversazioni e organizzarli in informazioni distinte e gestibili.\n\n"
    "Tipi di informazioni da ricordare:\n"
    "1. Preferenze personali: gusti, interessi, hobby, cibi preferiti, attività\n"
    "2. Dettagli personali importanti: nomi, relazioni familiari, date importanti\n"
    "3. Piani e intenzioni: eventi futuri, viaggi, obiettivi, appuntamenti\n"
    "4. Informazioni professionali: lavoro, progetti, competenze\n"
    "5. Informazioni su salute e benessere: dieta, sport, routine\n"
    "6. Dettagli vari: film, libri, brand, luoghi preferiti\n\n"
    "Esempi:\n\n"
    "Input: Ciao, come va?\nOutput: {\"facts\": []}\n\n"
    "Input: Domani devo andare a Napoli in treno.\n"
    "Output: {\"facts\": [\"Ha in programma un viaggio a Napoli in treno\"]}\n\n"
    "Input: Mia sorella Martina insegna danza a Modena.\n"
    "Output: {\"facts\": [\"Ha una sorella di nome Martina\", \"Martina insegna danza a Modena\"]}\n\n"
    "Input: Mi piace la pizza margherita ma Ada preferisce quella con le verdure.\n"
    "Output: {\"facts\": [\"Gli piace la pizza margherita\", \"Ada preferisce la pizza con le verdure\"]}\n\n"
    "Restituisci i fatti in formato JSON come mostrato sopra.\n\n"
    "Regole:\n"
    "- La data di oggi è {current_date}.\n"
    "- Non restituire nulla dagli esempi forniti sopra.\n"
    "- Se non trovi informazioni rilevanti, restituisci una lista vuota.\n"
    "- Estrai fatti dai messaggi dell'utente E dell'assistente.\n"
    "- I fatti devono essere in italiano, concisi e in terza persona.\n"
    "- La risposta DEVE essere in formato JSON con chiave \"facts\" e valore lista di stringhe."
).replace("{current_date}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

CUSTOM_UPDATE_MEMORY_PROMPT = (
    "Sei un gestore intelligente di memoria. Puoi eseguire quattro operazioni: "
    "(1) aggiungere alla memoria, (2) aggiornare la memoria, "
    "(3) cancellare dalla memoria, (4) nessuna modifica.\n\n"
    "Confronta i nuovi fatti estratti con la memoria esistente. Per ogni nuovo fatto, decidi se:\n"
    "- ADD: Aggiungere come nuovo elemento (genera un nuovo ID)\n"
    "- UPDATE: Aggiornare un elemento esistente (mantieni lo stesso ID)\n"
    "- DELETE: Cancellare un elemento esistente (quando il nuovo fatto lo contraddice)\n"
    "- NONE: Nessuna modifica (il fatto è già presente o irrilevante)\n\n"
    "Linee guida:\n\n"
    "1. **Aggiungi**: Informazioni nuove non presenti in memoria.\n"
    "   Esempio:\n"
    "   - Memoria: [{\"id\": \"0\", \"text\": \"È un ingegnere software\"}]\n"
    "   - Fatti: [\"Si chiama Marco\"]\n"
    "   - Risultato: {\"memory\": [{\"id\": \"0\", \"text\": \"È un ingegnere software\", \"event\": \"NONE\"}, "
    "{\"id\": \"1\", \"text\": \"Si chiama Marco\", \"event\": \"ADD\"}]}\n\n"
    "2. **Aggiorna**: Informazioni già presenti ma con dettagli diversi o più completi. "
    "Se il nuovo fatto trasmette la stessa informazione, non aggiornare.\n"
    "   Esempio:\n"
    "   - Memoria: [{\"id\": \"0\", \"text\": \"Gli piace giocare a calcio\"}]\n"
    "   - Fatti: [\"Gioca a calcio con gli amici il sabato\"]\n"
    "   - Risultato: {\"memory\": [{\"id\": \"0\", \"text\": \"Gioca a calcio con gli amici il sabato\", "
    "\"event\": \"UPDATE\", \"old_memory\": \"Gli piace giocare a calcio\"}]}\n\n"
    "3. **Cancella**: Il nuovo fatto contraddice la memoria esistente.\n\n"
    "4. **Nessuna modifica**: Il fatto è già presente in memoria.\n\n"
    "Restituisci SOLO il JSON {\"memory\": [...]} senza altro testo.\n"
    "Non generare nuovi ID per UPDATE o DELETE — usa solo gli ID dall'input."
)

CUSTOM_GRAPH_PROMPT = (
    "Usa nomi di entità nella lingua originale della conversazione. "
    "Usa relazioni in italiano quando possibile (es. 'sposato_con', 'padre_di', "
    "'lavora_a', 'interessato_a'). Identifica il soggetto della conversazione "
    "attraverso il user_id fornito."
)

config = {
    "llm": {
        "provider": "openai",
        "config": {
            "model": LLM_MODEL,
            "openai_base_url": LLM_URL,
            "api_key": "***",
            "temperature": 0.1,
        }
    },
    "graph_store": {
        "provider": "kuzu",
        "config": {"db": KUZU_PATH},
        "llm": {
            "provider": "openai",
            "config": {
                "model": GRAPH_LLM_MODEL,
                "openai_base_url": GRAPH_LLM_URL,
                "api_key": "***",
                "temperature": 0.1,
            }
        },
        "custom_prompt": CUSTOM_GRAPH_PROMPT,
    },
    "vector_store": {
        "provider": "chroma",
        "config": {
            "collection_name": "mem0_memories",
            "host": CHROMA_HOST,
            "port": CHROMA_PORT,
        }
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": EMBED_MODEL,
            "openai_base_url": EMBED_URL,
            "api_key": "***",
            "embedding_dims": EMBED_DIMS,
        }
    },
    "custom_fact_extraction_prompt": CUSTOM_FACT_EXTRACTION_PROMPT,
    "custom_update_memory_prompt": CUSTOM_UPDATE_MEMORY_PROMPT,
    "version": "v1.1",
}

app = FastAPI(title="Mem0 JARVIS Server", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
memory = Memory.from_config(config)
llm_client = OpenAI(base_url=LLM_URL, api_key="not-needed")
logger.info(f"Mem0 initialized (embedding dims: {EMBED_DIMS}, graph: {memory.enable_graph})")

# Background executors
graph_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph-extract")
graph_lock = threading.Lock()

# ─── SQLite queue (legacy async) ───────────────────────────────────────────

DB_PATH = "/data/mem0_queue.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            user_id TEXT NOT NULL,
            text TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            result TEXT,
            error TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_pending_jobs():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, text, metadata FROM jobs WHERE status = 'pending' ORDER BY created_at")
    jobs = cursor.fetchall()
    conn.close()
    return jobs

def update_job_status(job_id: str, status: str, result: str = None, error: str = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP, result = ?, error = ? WHERE id = ?",
        (status, result, error, job_id),
    )
    conn.commit()
    conn.close()

def get_job_status(job_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = cursor.fetchone()
    conn.close()
    return job

def background_worker():
    """Legacy queue worker — processes mem0.add() jobs."""
    logger.info("Background worker started")
    while True:
        try:
            jobs = get_pending_jobs()
            if jobs:
                job_id, user_id, text, metadata_str = jobs[0]
                logger.info(f"Processing job {job_id} for user {user_id}")
                update_job_status(job_id, 'processing')
                try:
                    metadata = json.loads(metadata_str) if metadata_str else None
                    result = memory.add(text, user_id=user_id, metadata=metadata)
                    update_job_status(job_id, 'completed', json.dumps(result))
                    logger.info(f"Job {job_id} completed")
                except Exception as e:
                    update_job_status(job_id, 'failed', error=str(e))
                    logger.error(f"Job {job_id} failed: {e}")
            else:
                time.sleep(2)
        except Exception as e:
            logger.error(f"Background worker error: {e}")
            time.sleep(5)

# ─── Graph extraction (background, non-blocking) ──────────────────────────

def _extract_graph_bg(text: str, user_id: str):
    """Extract entity relations and add to Kuzu graph store."""
    try:
        if memory.enable_graph and memory.graph:
            with graph_lock:
                result = memory.graph.add(text, {"user_id": user_id})
            logger.info(f"Graph extraction done for {user_id}: {len(result.get('added_entities', []))} added")
    except Exception as e:
        logger.error(f"Graph extraction failed for {user_id}: {e}")

# ─── TTL cleanup worker ───────────────────────────────────────────────────

def ttl_cleanup_worker():
    """Hourly cleanup of expired memories based on TTL metadata."""
    logger.info("TTL cleanup worker started")
    while True:
        time.sleep(3600)
        try:
            collection = memory.vector_store.collection
            results = collection.get(
                where={"ttl": {"$gt": 0}},
                include=["metadatas"],
            )
            if not results or not results.get("ids"):
                continue

            now = datetime.now(timezone.utc).timestamp()
            expired = []
            for i, meta in enumerate(results["metadatas"]):
                ts = meta.get("timestamp", "")
                ttl = meta.get("ttl", 0)
                if ts and ttl:
                    try:
                        created = datetime.fromisoformat(ts).timestamp()
                        if now > created + ttl:
                            expired.append(results["ids"][i])
                    except (ValueError, TypeError):
                        pass

            for mid in expired:
                collection.delete(ids=[mid])

            if expired:
                logger.info(f"TTL cleanup: deleted {len(expired)} expired memories")
        except Exception as e:
            logger.error(f"TTL cleanup error: {e}")

# ─── Initialize workers ───────────────────────────────────────────────────

init_db()
threading.Thread(target=background_worker, daemon=True, name="queue-worker").start()
threading.Thread(target=ttl_cleanup_worker, daemon=True, name="ttl-cleanup").start()

# ─── Request models ───────────────────────────────────────────────────────

class AddRequest(BaseModel):
    text: Optional[str] = None
    messages: Optional[List[dict]] = None
    user_id: str
    metadata: Optional[dict] = None

class SearchRequest(BaseModel):
    query: str
    user_id: str
    limit: Optional[int] = 10

class AsyncAddRequest(BaseModel):
    text: str
    user_id: str
    metadata: Optional[dict] = None

class AddRawRequest(BaseModel):
    text: str
    user_id: str
    metadata: Optional[dict] = None

class SearchContextualRequest(BaseModel):
    query: str
    user_id: str
    limit: Optional[int] = 10
    filters: Optional[dict] = None
    summarize: bool = False

# ─── Legacy endpoints ─────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0.0", "graph": memory.enable_graph}

@app.post("/add")
def add_memory_sync(req: AddRequest):
    """Full mem0 pipeline (fact extraction + dedup + graph). Blocks until done.
    Accepts either 'text' (string) or 'messages' (list of {role, content} dicts)."""
    try:
        data = req.messages if req.messages else req.text
        if not data:
            raise HTTPException(status_code=400, detail="Either 'text' or 'messages' required")
        result = memory.add(data, user_id=req.user_id, metadata=req.metadata)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"add error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search")
def search_memory(req: SearchRequest):
    try:
        results = memory.search(req.query, user_id=req.user_id, limit=req.limit)
        return results
    except Exception as e:
        logger.error(f"search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/memories/{user_id}")
def get_all_memories(user_id: str, limit: int = 100):
    try:
        results = memory.get_all(user_id=user_id, limit=limit)
        return results
    except Exception as e:
        logger.error(f"get_all error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/memory/{memory_id}")
def delete_memory(memory_id: str):
    try:
        memory.delete(memory_id)
        return {"status": "deleted", "memory_id": memory_id}
    except Exception as e:
        logger.error(f"delete error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search_cross")
def search_cross_user(req: SearchRequest):
    """Search across all users (no user_id filter)."""
    try:
        results = memory.search(req.query, limit=req.limit)
        return results
    except Exception as e:
        logger.error(f"search_cross error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─── Async queue endpoints (legacy) ───────────────────────────────────────

@app.post("/add_async")
def add_memory_async(req: AsyncAddRequest):
    """Queue a mem0.add() job — returns immediately."""
    job_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    metadata_str = json.dumps(req.metadata) if req.metadata else None
    conn.execute(
        "INSERT INTO jobs (id, user_id, text, metadata, status) VALUES (?, ?, ?, ?, 'pending')",
        (job_id, req.user_id, req.text, metadata_str),
    )
    conn.commit()
    conn.close()
    logger.info(f"Queued job {job_id} for user {req.user_id}")
    return {"job_id": job_id, "status": "queued"}

@app.get("/job/{job_id}")
def get_job(job_id: str):
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    id, status, user_id, text, metadata_str, created_at, updated_at, result, error = job
    response = {
        "job_id": id, "status": status, "user_id": user_id, "text": text,
        "created_at": created_at, "updated_at": updated_at,
    }
    if metadata_str:
        response["metadata"] = json.loads(metadata_str)
    if status == 'completed' and result:
        response["result"] = json.loads(result)
    elif status == 'failed' and error:
        response["error"] = error
    return response

@app.get("/jobs/pending")
def list_pending_jobs():
    jobs = get_pending_jobs()
    return {"pending_jobs": len(jobs), "jobs": [{"job_id": j[0], "user_id": j[1], "text": j[2]} for j in jobs]}

@app.get("/jobs/stats")
def queue_stats():
    conn = sqlite3.connect(DB_PATH)
    stats = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    conn.close()
    return {
        "pending": stats.get('pending', 0), "processing": stats.get('processing', 0),
        "completed": stats.get('completed', 0), "failed": stats.get('failed', 0),
        "total": sum(stats.values()),
    }

# ─── NEW: Raw insert (bypass mem0 pipeline) ───────────────────────────────

@app.post("/add_raw")
def add_raw(req: AddRawRequest):
    """Insert directly into ChromaDB with embedding. No LLM fact extraction.
    Graph extraction runs in background (non-blocking)."""
    try:
        memory_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Build flat metadata for ChromaDB (only str/int/float/bool)
        meta = {"user_id": req.user_id}
        if req.metadata:
            for k, v in req.metadata.items():
                if isinstance(v, list):
                    meta[k] = json.dumps(v)
                elif isinstance(v, (str, int, float, bool)):
                    meta[k] = v

        # Store text in metadata (ChromaDB documents not used by mem0 wrapper)
        meta["text"] = req.text

        # Ensure timestamp
        if "timestamp" not in meta:
            meta["timestamp"] = now

        # Embed
        embedding = memory.embedding_model.embed(req.text)

        # Insert into ChromaDB
        memory.vector_store.insert(
            vectors=[embedding],
            payloads=[meta],
            ids=[memory_id],
        )

        # Background graph extraction (fire-and-forget)
        graph_executor.submit(_extract_graph_bg, req.text, req.user_id)

        logger.info(f"Raw insert {memory_id[:8]} for {req.user_id} (scope={meta.get('scope', '-')})")
        return {"id": memory_id, "status": "inserted"}
    except Exception as e:
        logger.error(f"add_raw error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─── NEW: Contextual search (with optional 7B summary) ────────────────────

@app.post("/search_contextual")
def search_contextual(req: SearchContextualRequest):
    """Search ChromaDB with metadata filters + optional 7B summarization."""
    try:
        # Build filters
        filters = {"user_id": req.user_id}
        if req.filters:
            filters.update(req.filters)

        # Embed query
        query_vector = memory.embedding_model.embed(req.query)

        # Search
        results = memory.vector_store.search(
            query=req.query,
            vectors=[query_vector],
            limit=req.limit,
            filters=filters,
        )

        # Format results
        items = []
        for r in results:
            payload = r.payload or {}
            # Deserialize tags if present
            tags = payload.get("tags")
            if tags and isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Text can be in 'text' (new raw inserts) or 'memory' (legacy mem0)
            text = payload.get("text") or payload.get("data") or payload.get("memory", "")

            items.append({
                "id": r.id,
                "score": r.score,
                "text": text,
                "scope": payload.get("scope", ""),
                "subject": payload.get("subject", ""),
                "tags": tags or [],
                "role": payload.get("role", ""),
                "timestamp": payload.get("timestamp", ""),
                "user_id": payload.get("user_id", ""),
            })

        if not req.summarize or not items:
            return {"results": items, "count": len(items)}

        # Summarize with 7B
        mem_block = "\n".join(
            f"{i+1}. [{it['scope']}/{it['subject']}] {it['text']}"
            for i, it in enumerate(items) if it.get("text")
        )
        resp = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": (
                    "You are a memory retrieval summarizer. You receive a user query and a list of retrieved memory entries. "
                    "Your job is to synthesize ONLY the information present in the memories below. "
                    "Do NOT answer the query yourself. Do NOT add information not found in the memories. "
                    "If the memories don't contain relevant information, say so. "
                    "Be concise and factual. Use the same language as the query."
                )},
                {"role": "user", "content": f"Query: {req.query}\n\nMemories:\n{mem_block}\n\nSummarize."},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        summary = resp.choices[0].message.content

        return {"summary": summary, "results": items, "count": len(items)}
    except Exception as e:
        logger.error(f"search_contextual error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
