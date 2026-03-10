"""
JARVIS HA Memory Service
- Event ingestion da Home Assistant via WebSocket
- Summarization con Qwen (locale via Ollama o cloud via OpenRouter)
- Embedding con nomic-embed-text (locale) o Gemini (cloud)
- Vector search con ChromaDB per retrieval semantico
- API per orchestrator
"""

import os
import asyncio
import aiohttp
import sqlite3
import json
import time
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

import chromadb
from chromadb.config import Settings

# ===========================================================================
# CONFIG
# ===========================================================================

LOCATION_ID = os.getenv("LOCATION_ID", "unknown")
HA_URL = os.getenv("HA_URL", "http://supervisor/core")
HA_TOKEN = os.getenv("HA_TOKEN", "")

# AI Backend: "local" (Ollama) or "api" (OpenRouter + Gemini embeddings)
AI_BACKEND = os.getenv("AI_BACKEND", "local")

# --- Local mode (Ollama for LLM, fastembed for embeddings) ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBEDDING_URL = os.getenv("EMBEDDING_URL", OLLAMA_URL)  # fastembed CPU (default: fallback a Ollama)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "qwen2.5:3b")

# --- API mode (OpenRouter for summarization, Gemini for embeddings) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1"
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "https://jarvis.yourdomain.com")
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "JARVIS HA Memory")
OPENROUTER_SUMMARY_MODEL = os.getenv("OPENROUTER_SUMMARY_MODEL", "qwen/qwen-2.5-7b-instruct")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"

# --- Common ---
SUMMARY_TEMPERATURE = float(os.getenv("SUMMARY_TEMPERATURE", "0.3"))
SUMMARY_TIMEOUT = int(os.getenv("SUMMARY_TIMEOUT", "30"))
EMBEDDING_TIMEOUT = int(os.getenv("EMBEDDING_TIMEOUT", "30"))
DB_PATH = os.getenv("DB_PATH", "/data/ha_memory.db")
CHROMA_PATH = os.getenv("CHROMA_PATH", "/data/chroma")  # Legacy fallback
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8100"))
WS_RETRY_DELAY = int(os.getenv("WS_RETRY_DELAY", "30"))
WS_RECONNECT_DELAY = int(os.getenv("WS_RECONNECT_DELAY", "10"))
SCHEDULER_INITIAL_DELAY = int(os.getenv("SCHEDULER_INITIAL_DELAY", "30"))
SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "60"))
COLLECTION_LOCATION_EVENTS = "location_events"
SKIP_ENTITY_PREFIXES = os.getenv("SKIP_ENTITY_PREFIXES", "update.").split(",")
SKIP_ENTITY_SUFFIXES = os.getenv("SKIP_ENTITY_SUFFIXES", "_battery,_linkquality,_signal").split(",")

EMBEDDING_DIM = 768  # Comune a nomic-embed-text e gemini-embedding-001 (con output_dimensionality)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HA_MEMORY")


# ===========================================================================
# DATABASE
# ===========================================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # Eventi raw (TTL 30 min, gestito da cleanup)
    c.execute('''
        CREATE TABLE IF NOT EXISTS events_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id TEXT NOT NULL,
            old_state TEXT,
            new_state TEXT,
            attributes TEXT,
            timestamp REAL NOT NULL
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_events_time ON events_raw(timestamp)')

    # Summaries orari
    c.execute('''
        CREATE TABLE IF NOT EXISTS location_memory_hourly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hour_start REAL NOT NULL,
            hour_end REAL NOT NULL,
            summary TEXT NOT NULL,
            event_count INTEGER,
            anomalies TEXT,
            created_at REAL DEFAULT (unixepoch())
        )
    ''')

    # Summaries giornalieri
    c.execute('''
        CREATE TABLE IF NOT EXISTS location_memory_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            summary TEXT NOT NULL,
            patterns TEXT,
            anomalies TEXT,
            created_at REAL DEFAULT (unixepoch())
        )
    ''')

    # Fatti long-term location
    c.execute('''
        CREATE TABLE IF NOT EXISTS location_memory_longterm (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            category TEXT,
            confidence REAL DEFAULT 0.8,
            created_at REAL DEFAULT (unixepoch()),
            updated_at REAL
        )
    ''')

    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_PATH}")


# ===========================================================================
# EMBEDDING FUNCTIONS
# ===========================================================================
# AI_BACKEND=local → OllamaEmbeddingFunction (nomic-embed-text, fastembed CPU)
# AI_BACKEND=api   → GeminiEmbeddingFunction (gemini-embedding-001)
# ===========================================================================

class OllamaEmbeddingFunction:
    """Embedding via fastembed server (Ollama-compatible API, CPU-only ONNX)."""

    def __init__(self, model: str = EMBEDDING_MODEL, url: str = EMBEDDING_URL):
        self.model = model
        self.url = f"{url.rstrip('/')}/api/embeddings"

    def name(self) -> str:
        return f"ollama-{self.model}"

    def __call__(self, input: list) -> list:
        import requests
        embeddings = []
        for text in input:
            try:
                response = requests.post(
                    self.url,
                    json={"model": self.model, "prompt": text},
                    timeout=EMBEDDING_TIMEOUT
                )
                if response.status_code == 200:
                    embeddings.append(response.json()["embedding"])
                else:
                    logger.error(f"Ollama embedding error: {response.status_code}")
                    embeddings.append([0.0] * EMBEDDING_DIM)
            except Exception as e:
                logger.error(f"Ollama embedding exception: {e}")
                embeddings.append([0.0] * EMBEDDING_DIM)
        return embeddings


class GeminiEmbeddingFunction:
    """Embedding via Gemini API (gemini-embedding-001).
    Stessa dimensionalita (768) di nomic-embed-text.
    """

    def __init__(self):
        self.api_key = GEMINI_API_KEY
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY required for cloud embeddings (AI_BACKEND=api)")

    def name(self) -> str:
        return "gemini-embedding-001"

    def __call__(self, input: list) -> list:
        import requests
        embeddings = []
        for text in input:
            try:
                response = requests.post(
                    GEMINI_EMBED_URL,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self.api_key,
                    },
                    json={
                        "content": {"parts": [{"text": text}]},
                        "output_dimensionality": EMBEDDING_DIM,
                    },
                    timeout=EMBEDDING_TIMEOUT,
                )
                if response.status_code == 200:
                    data = response.json()
                    embeddings.append(data["embedding"]["values"])
                else:
                    logger.error(f"Gemini embedding error {response.status_code}: {response.text[:200]}")
                    embeddings.append([0.0] * EMBEDDING_DIM)
            except Exception as e:
                logger.error(f"Gemini embedding exception: {e}")
                embeddings.append([0.0] * EMBEDDING_DIM)
        return embeddings


def get_embedding_function():
    """Factory: seleziona embedding function in base a AI_BACKEND."""
    if AI_BACKEND == "api":
        logger.info("Using Gemini embeddings (cloud mode)")
        return GeminiEmbeddingFunction()
    else:
        logger.info("Using Ollama embeddings (local mode)")
        return OllamaEmbeddingFunction()


# ===========================================================================
# VECTOR STORE (Location Events)
# ===========================================================================

class LocationVectorStore:
    """Vector store per eventi location."""

    def __init__(self):
        self.client = None
        self.collection = None
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return

        try:
            self.client = chromadb.HttpClient(
                host=CHROMA_HOST,
                port=CHROMA_PORT,
                settings=Settings(anonymized_telemetry=False)
            )
            self.client.heartbeat()
            logger.info(f"Connected to ChromaDB server at {CHROMA_HOST}:{CHROMA_PORT}")
        except Exception as e:
            logger.warning(f"ChromaDB server unreachable ({CHROMA_HOST}:{CHROMA_PORT}): {e}")
            logger.warning(f"Falling back to PersistentClient at {CHROMA_PATH}")
            self.client = chromadb.PersistentClient(
                path=CHROMA_PATH,
                settings=Settings(anonymized_telemetry=False)
            )

        self.embedding_fn = get_embedding_function()

        # Collection WITHOUT embedding_function — ChromaDB 0.6.x HttpClient
        # can't serialize custom embedding functions (KeyError '_type').
        # We compute embeddings manually in add/query methods instead.
        self.collection = self.client.get_or_create_collection(
            name=f"{COLLECTION_LOCATION_EVENTS}_{LOCATION_ID}",
            metadata={"hnsw:space": "cosine"}
        )

        self._initialized = True
        logger.info(f"Location vector store initialized for {LOCATION_ID}")

    def add_event(
        self,
        entity_id: str,
        old_state: str,
        new_state: str,
        timestamp: float
    ):
        """Aggiunge evento al vector store."""
        if not self._initialized:
            self.initialize()

        doc_id = f"evt_{LOCATION_ID}_{int(timestamp * 1000)}"

        # Testo descrittivo per embedding
        text = f"{entity_id} changed from {old_state} to {new_state}"

        try:
            embeddings = self.embedding_fn([text])
            self.collection.add(
                documents=[text],
                embeddings=embeddings,
                metadatas=[{
                    "entity_id": entity_id,
                    "old_state": old_state or "",
                    "new_state": new_state or "",
                    "timestamp": timestamp,
                    "location_id": LOCATION_ID
                }],
                ids=[doc_id]
            )
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.error(f"Vector add error: {e}")

    def search_events(
        self,
        query: str,
        n_results: int = 30,
        min_timestamp: float = None
    ) -> list:
        """Cerca eventi semanticamente simili."""
        if not self._initialized:
            self.initialize()

        if min_timestamp is None:
            min_timestamp = time.time() - (7 * 86400)  # 7 giorni

        try:
            query_embeddings = self.embedding_fn([query])
            results = self.collection.query(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where={"timestamp": {"$gte": min_timestamp}},
                include=["documents", "metadatas", "distances"]
            )

            # Format con recency boost
            formatted = []
            now = time.time()

            docs = results['documents'][0] if results['documents'] else []
            metas = results['metadatas'][0] if results['metadatas'] else []
            distances = results['distances'][0] if results['distances'] else []

            for doc, meta, dist in zip(docs, metas, distances):
                similarity = 1 - dist
                age_hours = (now - meta.get('timestamp', now)) / 3600
                recency = 1 / (1 + age_hours * 0.05)

                formatted.append({
                    "content": doc,
                    "metadata": meta,
                    "similarity": similarity,
                    "recency": recency,
                    "final_score": similarity * recency
                })

            formatted.sort(key=lambda x: x['final_score'], reverse=True)
            return formatted

        except Exception as e:
            logger.error(f"Vector search error: {e}")
            return []

    def cleanup_old(self, max_age_days: int = 7):
        """Rimuove vettori vecchi."""
        if not self._initialized:
            return

        cutoff = time.time() - (max_age_days * 86400)

        try:
            old = self.collection.get(
                where={"timestamp": {"$lt": cutoff}},
                include=[]
            )
            if old and old.get('ids'):
                self.collection.delete(ids=old['ids'])
                logger.info(f"Cleaned {len(old['ids'])} old event vectors")
        except Exception as e:
            logger.error(f"Vector cleanup error: {e}")


# Istanza globale
location_vector_store = LocationVectorStore()


# ===========================================================================
# EVENT INGESTION
# ===========================================================================

def _get_ws_url() -> str:
    """
    Costruisce WebSocket URL in base al contesto:
    - HAOS addon: ws://supervisor/core/websocket (via Supervisor proxy)
    - Standalone: ws://<HA_URL>/api/websocket
    """
    if "supervisor" in HA_URL:
        # HAOS addon mode — Supervisor proxy
        return "ws://supervisor/core/websocket"
    else:
        # Standalone Docker — connessione diretta
        return HA_URL.replace("http", "ws") + "/api/websocket"


async def subscribe_ha_events():
    """Sottoscrivi agli eventi state_changed di Home Assistant."""
    ws_url = _get_ws_url()

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    # Auth
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
                    auth_result = await ws.receive_json()

                    if auth_result.get("type") != "auth_ok":
                        logger.error("HA WebSocket auth failed")
                        await asyncio.sleep(WS_RETRY_DELAY)
                        continue

                    # Subscribe
                    await ws.send_json({
                        "id": 1,
                        "type": "subscribe_events",
                        "event_type": "state_changed"
                    })

                    logger.info(f"Subscribed to HA events for location {LOCATION_ID}")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("type") == "event":
                                await process_state_change(data["event"]["data"])
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break

        except Exception as e:
            logger.error(f"HA WebSocket error: {e}")
            await asyncio.sleep(WS_RECONNECT_DELAY)


async def process_state_change(data: dict):
    """
    Processa un evento state_changed.
    Salva in SQLite + ChromaDB.
    """
    entity_id = data.get("entity_id", "")
    old_state = data.get("old_state", {})
    new_state = data.get("new_state", {})

    if should_skip_entity(entity_id, old_state, new_state):
        return

    timestamp = time.time()
    old_state_val = old_state.get("state") if old_state else None
    new_state_val = new_state.get("state") if new_state else None

    # Salva in SQLite
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO events_raw (entity_id, old_state, new_state, attributes, timestamp)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        entity_id,
        old_state_val,
        new_state_val,
        json.dumps(new_state.get("attributes", {})) if new_state else None,
        timestamp
    ))
    conn.commit()
    conn.close()

    # Salva in vector store
    try:
        location_vector_store.add_event(
            entity_id=entity_id,
            old_state=old_state_val,
            new_state=new_state_val,
            timestamp=timestamp
        )
    except Exception as e:
        logger.warning(f"Vector write failed: {e}")


def should_skip_entity(entity_id: str, old_state: dict, new_state: dict) -> bool:
    """Skip SOLO noise puro. Nessuna logica smart qui."""
    # Configurable prefix filter (e.g. "update.")
    if any(entity_id.startswith(prefix) for prefix in SKIP_ENTITY_PREFIXES):
        return True

    # Configurable suffix filter (e.g. "_battery", "_linkquality", "_signal")
    if any(suffix in entity_id for suffix in SKIP_ENTITY_SUFFIXES):
        return True

    # Stato non cambiato
    if old_state and new_state:
        if old_state.get("state") == new_state.get("state"):
            return True

    return False


# ===========================================================================
# SUMMARIZATION
# ===========================================================================

from prompts import load_prompt

LOCATION_HOURLY_PROMPT = load_prompt("location_hourly")
LOCATION_DAILY_PROMPT = load_prompt("location_daily")


async def call_llm_summary(prompt: str, max_tokens: int = 150) -> str:
    """Chiama LLM per summarization. Usa Ollama o OpenRouter in base a AI_BACKEND."""
    if AI_BACKEND == "api":
        return await _call_openrouter(prompt, max_tokens)
    else:
        return await _call_ollama(prompt, max_tokens)


async def _call_ollama(prompt: str, max_tokens: int = 150) -> str:
    """Chiamata Ollama locale."""
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": SUMMARY_TEMPERATURE, "num_predict": max_tokens}
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{OLLAMA_URL.rstrip('/')}/api/chat",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=SUMMARY_TIMEOUT)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                return result.get("message", {}).get("content", "")
            else:
                raise Exception(f"Ollama error: {resp.status}")


async def _call_openrouter(prompt: str, max_tokens: int = 150) -> str:
    """Chiamata OpenRouter (formato OpenAI-compatible)."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    }
    payload = {
        "model": OPENROUTER_SUMMARY_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": SUMMARY_TEMPERATURE,
        "max_tokens": max_tokens,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{OPENROUTER_API_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=SUMMARY_TIMEOUT)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                return result["choices"][0]["message"]["content"]
            else:
                body = await resp.text()
                raise Exception(f"OpenRouter error {resp.status}: {body[:200]}")


async def run_hourly_summary():
    """Job orario per summarization eventi."""
    conn = get_db()
    c = conn.cursor()

    now = time.time()
    hour_start = now - 3600

    c.execute('''
        SELECT entity_id, old_state, new_state, timestamp
        FROM events_raw
        WHERE timestamp > ?
        ORDER BY timestamp ASC
    ''', (hour_start,))
    events = c.fetchall()

    if not events:
        logger.info("No events in last hour, skipping summary")
        conn.close()
        return

    events_text = "\n".join([
        f"- {datetime.fromtimestamp(e['timestamp']).strftime('%H:%M')} | {e['entity_id']}: {e['old_state']} -> {e['new_state']}"
        for e in events[:100]
    ])

    prompt = LOCATION_HOURLY_PROMPT.format(
        location_id=LOCATION_ID,
        events=events_text
    )

    try:
        summary = await call_llm_summary(prompt, max_tokens=150)

        c.execute('''
            INSERT INTO location_memory_hourly (hour_start, hour_end, summary, event_count)
            VALUES (?, ?, ?, ?)
        ''', (hour_start, now, summary, len(events)))

        # Cleanup eventi vecchi (> 30 min)
        cleanup_cutoff = now - 1800
        c.execute('DELETE FROM events_raw WHERE timestamp < ?', (cleanup_cutoff,))

        conn.commit()
        logger.info(f"Hourly summary: {len(events)} events -> {len(summary)} chars")

    except Exception as e:
        logger.error(f"Hourly summary failed: {e}")

    conn.close()


async def run_daily_summary():
    """Job giornaliero per summary + pattern extraction."""
    conn = get_db()
    c = conn.cursor()

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday_start = time.time() - 86400

    c.execute('''
        SELECT hour_start, summary
        FROM location_memory_hourly
        WHERE hour_start > ?
        ORDER BY hour_start ASC
    ''', (yesterday_start,))
    hourly = c.fetchall()

    if not hourly:
        logger.info("No hourly summaries, skipping daily")
        conn.close()
        return

    hourly_text = "\n".join([
        f"- {datetime.fromtimestamp(h['hour_start']).strftime('%H:%M')}: {h['summary']}"
        for h in hourly
    ])

    prompt = LOCATION_DAILY_PROMPT.format(
        location_id=LOCATION_ID,
        hourly_summaries=hourly_text
    )

    try:
        response = await call_llm_summary(prompt, max_tokens=400)
        data = json.loads(response)

        c.execute('''
            INSERT OR REPLACE INTO location_memory_daily (date, summary, patterns, anomalies)
            VALUES (?, ?, ?, ?)
        ''', (
            today,
            data.get("summary", ""),
            json.dumps(data.get("patterns", {})),
            json.dumps(data.get("anomalies", []))
        ))

        for fact in data.get("new_facts", []):
            c.execute('''
                INSERT INTO location_memory_longterm (fact, category)
                VALUES (?, 'pattern')
            ''', (fact,))

        conn.commit()
        logger.info(f"Daily summary: {len(data.get('new_facts', []))} new facts")

    except Exception as e:
        logger.error(f"Daily summary failed: {e}")

    conn.close()


# ===========================================================================
# SCHEDULER
# ===========================================================================

async def scheduler():
    """Background scheduler per job periodici."""
    await asyncio.sleep(SCHEDULER_INITIAL_DELAY)
    logger.info("Scheduler started")

    while True:
        now = datetime.now()

        try:
            if now.minute == 5:
                await run_hourly_summary()

            if now.hour == 3 and now.minute == 0:
                await run_daily_summary()

                # Cleanup vector store
                location_vector_store.cleanup_old(max_age_days=7)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        await asyncio.sleep(SCHEDULER_INTERVAL)


# ===========================================================================
# API
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log backend mode
    if AI_BACKEND == "api":
        logger.info(f"AI Backend: cloud (OpenRouter: {OPENROUTER_SUMMARY_MODEL}, Gemini embeddings)")
    else:
        logger.info(f"AI Backend: local (Ollama: {SUMMARY_MODEL}, {EMBEDDING_MODEL})")

    init_db()
    location_vector_store.initialize()
    asyncio.create_task(subscribe_ha_events())
    asyncio.create_task(scheduler())
    yield

app = FastAPI(title="JARVIS HA Memory Service", lifespan=lifespan)


class MemoryResponse(BaseModel):
    location_id: str
    hot: List[Dict[str, Any]]
    warm: List[Dict[str, Any]]
    cold: List[Dict[str, Any]]
    longterm: List[Dict[str, Any]]
    token_estimate: int


@app.get("/memory", response_model=MemoryResponse)
async def get_location_memory_endpoint(
    hot_minutes: int = 30,
    warm_hours: int = 24,
    cold_days: int = 7
):
    """Endpoint per orchestrator: recupera memoria location."""
    conn = get_db()
    c = conn.cursor()

    now = time.time()

    c.execute('''
        SELECT entity_id, old_state, new_state, timestamp
        FROM events_raw WHERE timestamp > ?
        ORDER BY timestamp DESC LIMIT 50
    ''', (now - hot_minutes * 60,))
    hot = [dict(row) for row in c.fetchall()]

    c.execute('''
        SELECT hour_start, summary, anomalies
        FROM location_memory_hourly WHERE hour_start > ?
        ORDER BY hour_start DESC
    ''', (now - warm_hours * 3600,))
    warm = [dict(row) for row in c.fetchall()]

    c.execute('''
        SELECT date, summary, patterns
        FROM location_memory_daily
        ORDER BY date DESC LIMIT ?
    ''', (cold_days,))
    cold = [dict(row) for row in c.fetchall()]

    c.execute('''
        SELECT fact, category, confidence
        FROM location_memory_longterm
        ORDER BY confidence DESC, updated_at DESC LIMIT 30
    ''')
    longterm = [dict(row) for row in c.fetchall()]

    conn.close()

    total_chars = sum(len(str(item)) for layer in [hot, warm, cold, longterm] for item in layer)

    return MemoryResponse(
        location_id=LOCATION_ID,
        hot=hot,
        warm=warm,
        cold=cold,
        longterm=longterm,
        token_estimate=total_chars // 4
    )


class VectorSearchRequest(BaseModel):
    query: str
    n_results: int = 30
    min_timestamp: Optional[float] = None


class VectorSearchResponse(BaseModel):
    location_id: str
    results: List[Dict[str, Any]]
    count: int


@app.post("/memory/search", response_model=VectorSearchResponse)
async def search_location_memory(request: VectorSearchRequest):
    """
    Ricerca semantica negli eventi location.
    Usato dall'orchestrator per query tipo "quando ho lasciato aperto X?"
    """
    results = location_vector_store.search_events(
        query=request.query,
        n_results=request.n_results,
        min_timestamp=request.min_timestamp
    )

    return VectorSearchResponse(
        location_id=LOCATION_ID,
        results=results,
        count=len(results)
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "location_id": LOCATION_ID,
        "ai_backend": AI_BACKEND,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
