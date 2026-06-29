"""
JARVIS HA Memory Service
- Event ingestion da Home Assistant via WebSocket
- Summarization con Qwen (locale via Ollama o cloud via OpenRouter)
- API per orchestrator (riassunti SQL + sintesi event-driven)

NOTA: lo strato semantico (vector search) e' stato spostato in mem0-stack
(croll83/mem0-stack). Eventi memorabili emergono nel sistema mem0 tramite
l'orchestrator/hermes-plugin, non piu' qui dentro.
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

# ===========================================================================
# CONFIG
# ===========================================================================

LOCATION_ID = os.getenv("LOCATION_ID", "unknown")
HA_URL = os.getenv("HA_URL", "http://supervisor/core")
HA_TOKEN = os.getenv("HA_TOKEN", "")

# AI Backend: "local" (Ollama), "api" (OpenRouter) o "proxy" (billing proxy Anthropic-compatible)
AI_BACKEND = os.getenv("AI_BACKEND", "local")

# --- Local mode (Ollama for LLM) ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "qwen2.5:3b")

# --- API mode (OpenRouter for summarization) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1"
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "https://jarvis.yourdomain.com")
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "JARVIS HA Memory")
OPENROUTER_SUMMARY_MODEL = os.getenv("OPENROUTER_SUMMARY_MODEL", "qwen/qwen-2.5-7b-instruct")

# --- Proxy mode (billing proxy Anthropic-compatible, es. hermes :18801) ---
PROXY_URL = os.getenv("PROXY_URL", "http://100.116.99.9:18801")
PROXY_MODEL = os.getenv("PROXY_MODEL", "claude-sonnet-4-6")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")

# --- Common ---
SUMMARY_TEMPERATURE = float(os.getenv("SUMMARY_TEMPERATURE", "0.3"))
SUMMARY_TIMEOUT = int(os.getenv("SUMMARY_TIMEOUT", "30"))
DB_PATH = os.getenv("DB_PATH", "/data/ha_memory.db")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8100"))
WS_RETRY_DELAY = int(os.getenv("WS_RETRY_DELAY", "30"))
WS_RECONNECT_DELAY = int(os.getenv("WS_RECONNECT_DELAY", "10"))
SCHEDULER_INITIAL_DELAY = int(os.getenv("SCHEDULER_INITIAL_DELAY", "30"))
SCHEDULER_INTERVAL = int(os.getenv("SCHEDULER_INTERVAL", "60"))
SKIP_ENTITY_PREFIXES = os.getenv("SKIP_ENTITY_PREFIXES", "update.").split(",")
SKIP_ENTITY_SUFFIXES = os.getenv("SKIP_ENTITY_SUFFIXES", "_battery,_linkquality,_signal").split(",")

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

    # Push to Redis context bus (short-term cross-system memory)
    try:
        from context_bus import ContextBus
        bus = ContextBus(source="ha")
        # HA events go to "shared" since they're location-level, not user-specific
        bus.push(
            "shared",
            text=f"{entity_id}: {old_state_val} → {new_state_val}",
            room=LOCATION_ID,
            event_type="state_change",
            entities=[entity_id],
        )
    except Exception as e:
        logger.debug(f"Context bus push failed: {e}")


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
    """Chiama LLM per summarization. Backend scelto da AI_BACKEND: proxy/api/local."""
    if AI_BACKEND == "proxy":
        return await _call_anthropic_proxy(prompt, max_tokens)
    elif AI_BACKEND == "api":
        return await _call_openrouter(prompt, max_tokens)
    else:
        return await _call_ollama(prompt, max_tokens)


async def _call_anthropic_proxy(prompt: str, max_tokens: int = 150) -> str:
    """Chiamata al billing proxy Anthropic-compatible (es. hermes :18801).

    Endpoint Messages API (/v1/messages), nessuna auth lato proxy.
    La risposta Anthropic ha `content` come lista di blocchi: si ritorna
    il testo del primo blocco di tipo 'text'.
    """
    headers = {
        "content-type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    payload = {
        "model": PROXY_MODEL,
        "max_tokens": max_tokens,
        "temperature": SUMMARY_TEMPERATURE,
        "messages": [{"role": "user", "content": prompt}],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{PROXY_URL.rstrip('/')}/v1/messages",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=SUMMARY_TIMEOUT)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", "")
                return ""
            else:
                body = await resp.text()
                raise Exception(f"Proxy error {resp.status}: {body[:200]}")


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

        # Push behavioral patterns to mem0 (long-term memory)
        new_facts = data.get("new_facts", [])
        patterns = data.get("patterns", {})
        if new_facts or patterns:
            try:
                import httpx
                mem0_url = os.environ["MEM0_BASE_URL"]
                facts_text = f"Location {LOCATION_ID} - pattern giornalieri:\n"
                if patterns:
                    facts_text += "\n".join(f"- {k}: {v}" for k, v in patterns.items()) + "\n"
                if new_facts:
                    facts_text += "\n".join(f"- {f}" for f in new_facts)
                # location metadata = this add-on's LOCATION_ID. Keeps facts
                # cross-house (user_id=shared) ma permette filtering per
                # location lato consumer (behavioral analysis per casa).
                resp = httpx.post(
                    f"{mem0_url}/add",
                    json={
                        "text": facts_text,
                        "user_id": "shared",
                        "metadata": {
                            "source": "ha_memory_service",
                            "location": LOCATION_ID,
                        },
                    },
                    timeout=120.0,
                )
                resp.raise_for_status()
                result = resp.json()
                logger.info(f"HA patterns pushed to mem0: {len(result.get('results', []))} facts")
            except Exception as e:
                logger.warning(f"mem0 push failed: {e}")

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
        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        await asyncio.sleep(SCHEDULER_INTERVAL)


# ===========================================================================
# API
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log backend mode
    if AI_BACKEND == "proxy":
        logger.info(f"AI Backend: proxy ({PROXY_MODEL} @ {PROXY_URL})")
    elif AI_BACKEND == "api":
        logger.info(f"AI Backend: cloud (OpenRouter: {OPENROUTER_SUMMARY_MODEL})")
    else:
        logger.info(f"AI Backend: local (Ollama: {SUMMARY_MODEL})")

    init_db()
    # Memoria semantica/procedurale gestita da mem0-stack (esterno).
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
