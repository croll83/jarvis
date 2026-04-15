"""
JARVIS Memory Jobs
- Summarization oraria/giornaliera per utenti
- Estrazione fatti long-term → mem0
- Estrazione abitudini comportamentali → mem0
"""

import asyncio
import json
import time
import logging
from datetime import datetime
from typing import Optional

import httpx

from database import (
    _get_conn,
    save_user_hourly_summary,
    save_user_daily_summary,
    save_user_longterm_fact,
    cleanup_old_user_memory
)
from ai_engines import call_qwen_summary
from context_bus import speaker_to_user_id
from prompts import load_prompt
import config

logger = logging.getLogger("JARVIS_MEMORY")

# ===========================================================================
# CONFIG
# ===========================================================================

MEM0_BASE_URL = config.MEM0_BASE_URL
MEM0_ADD_TIMEOUT = 120.0

# ===========================================================================
# PROMPTS (loaded from external .txt files)
# ===========================================================================

USER_HOURLY_SUMMARY_PROMPT = load_prompt("user_hourly_summary")
USER_DAILY_SUMMARY_PROMPT = load_prompt("user_daily_summary")

# Prompt per estrazione abitudini comportamentali
BEHAVIORAL_EXTRACTION_PROMPT = """Analizza le interazioni vocali delle ultime 24 ore di questo utente.
Estrai SOLO informazioni utili per la memoria a lungo termine:

- Abitudini ricorrenti (orari, routine, preferenze quotidiane)
- Preferenze esplicite o implicite (temperatura, luci, dispositivi preferiti)
- Pattern comportamentali (es: ogni mattina accende X, la sera fa Y)
- Informazioni nuove su persone, luoghi, contesti menzionati
- Comandi che rivelano interessi o bisogni

NON estrarre: comandi singoli non ricorrenti, azioni banali, stati temporanei,
cose gia' note dalle sessioni precedenti.

Interazioni giornaliere:
{interactions}

Rispondi con una lista di fatti in italiano, uno per riga. Se non ci sono
fatti rilevanti, rispondi con "NESSUNO".
"""


# ===========================================================================
# HOURLY SUMMARY
# ===========================================================================

async def run_user_hourly_summary(user_id: Optional[int] = None):
    """
    Genera summary orario per utente.
    Se user_id e' None, processa tutti gli utenti con messaggi nell'ultima ora.
    """
    conn = _get_conn()
    c = conn.cursor()

    now = time.time()
    hour_start = now - 3600

    # Trova utenti con messaggi nell'ultima ora
    if user_id:
        user_ids = [user_id]
    else:
        c.execute('''
            SELECT DISTINCT speaker_id FROM chat_memory
            WHERE timestamp > ? AND speaker_id IS NOT NULL
        ''', (hour_start,))
        user_ids = [row['speaker_id'] for row in c.fetchall()]

    conn.close()

    for uid in user_ids:
        await _process_user_hourly(uid, hour_start, now)


async def _process_user_hourly(user_id: int, hour_start: float, hour_end: float):
    """Processa summary orario per singolo utente."""
    conn = _get_conn()
    c = conn.cursor()

    # Recupera messaggi dell'ora
    c.execute('''
        SELECT role, content, speaker_name
        FROM chat_memory
        WHERE speaker_id = ? AND timestamp > ? AND timestamp <= ?
        ORDER BY timestamp ASC
    ''', (user_id, hour_start, hour_end))
    messages = c.fetchall()
    conn.close()

    if len(messages) < config.MEMORY_MIN_MESSAGES_FOR_SUMMARY:
        return

    # Formatta per Qwen
    msg_text = "\n".join([
        f"{m['speaker_name'] or 'User'}: {m['content'][:config.MEMORY_CONTENT_TRUNCATE]}"
        for m in messages
    ])

    prompt = USER_HOURLY_SUMMARY_PROMPT.format(messages=msg_text)

    try:
        summary = await call_qwen_summary(prompt, max_tokens=100)
        save_user_hourly_summary(user_id, hour_start, hour_end, summary, len(messages))
        logger.info(f"User {user_id} hourly summary: {len(messages)} msgs -> {len(summary)} chars")
    except Exception as e:
        logger.error(f"User {user_id} hourly summary failed: {e}")


# ===========================================================================
# DAILY SUMMARY
# ===========================================================================

async def run_user_daily_summary(user_id: Optional[int] = None):
    """
    Genera summary giornaliero + estrae fatti long-term.
    Da eseguire ogni notte (es: 03:00).
    """
    conn = _get_conn()
    c = conn.cursor()

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday_start = time.time() - 86400

    # Trova utenti con summaries orari di ieri
    if user_id:
        user_ids = [user_id]
    else:
        c.execute('''
            SELECT DISTINCT user_id FROM user_memory_hourly
            WHERE hour_start > ?
        ''', (yesterday_start,))
        user_ids = [row['user_id'] for row in c.fetchall()]

    conn.close()

    for uid in user_ids:
        await _process_user_daily(uid, yesterday_start, today)


async def _process_user_daily(user_id: int, start_time: float, date: str):
    """Processa summary giornaliero per singolo utente."""
    conn = _get_conn()
    c = conn.cursor()

    # Recupera summaries orari
    c.execute('''
        SELECT hour_start, summary
        FROM user_memory_hourly
        WHERE user_id = ? AND hour_start > ?
        ORDER BY hour_start ASC
    ''', (user_id, start_time))
    hourly = c.fetchall()
    conn.close()

    if not hourly:
        return

    hourly_text = "\n".join([
        f"- Ore {datetime.fromtimestamp(h['hour_start']).strftime('%H:%M')}: {h['summary']}"
        for h in hourly
    ])

    prompt = USER_DAILY_SUMMARY_PROMPT.format(hourly_summaries=hourly_text)

    try:
        response = await call_qwen_summary(prompt, max_tokens=300)
        data = json.loads(response)

        save_user_daily_summary(
            user_id,
            date,
            data.get("summary", ""),
            data.get("patterns", {}),
            data.get("anomalies", [])
        )

        # Estrai fatti long-term (SQLite only, no ChromaDB)
        for fact in data.get("new_facts", []):
            save_user_longterm_fact(user_id, fact, "extracted", "daily_summary")

        logger.info(f"User {user_id} daily summary: {len(hourly)} hours -> {len(data.get('new_facts', []))} new facts")

    except json.JSONDecodeError as e:
        logger.error(f"User {user_id} daily summary JSON parse failed: {e}")
    except Exception as e:
        logger.error(f"User {user_id} daily summary failed: {e}")


# ===========================================================================
# MEM0 BEHAVIORAL EXTRACTION (nightly batch)
# ===========================================================================

async def run_mem0_behavioral_extraction():
    """
    Estrae abitudini comportamentali dalle interazioni vocali giornaliere
    e le invia a mem0 per la long-term memory.
    Gira di notte insieme al daily summary.
    """
    conn = _get_conn()
    c = conn.cursor()

    yesterday_start = time.time() - 86400

    # Recupera tutti i messaggi user delle ultime 24h, raggruppati per speaker
    c.execute('''
        SELECT speaker_id, speaker_name, role, content, source, timestamp
        FROM chat_memory
        WHERE timestamp > ? AND speaker_id IS NOT NULL
        ORDER BY speaker_id, timestamp ASC
    ''', (yesterday_start,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        logger.info("No voice interactions in last 24h, skipping behavioral extraction")
        return

    # Raggruppa per speaker
    by_speaker = {}
    for row in rows:
        sid = row['speaker_id']
        if sid not in by_speaker:
            by_speaker[sid] = {
                'speaker_name': row['speaker_name'],
                'messages': []
            }
        by_speaker[sid]['messages'].append(row)

    for speaker_id, data in by_speaker.items():
        await _extract_and_push_to_mem0(speaker_id, data)


async def _extract_and_push_to_mem0(speaker_id: int, data: dict):
    """Estrai pattern comportamentali e invia a mem0."""
    messages = data['messages']
    speaker_name = data['speaker_name']
    mem0_user_id = speaker_to_user_id(speaker_id, speaker_name)

    # Formatta interazioni per il prompt
    interactions = []
    for msg in messages:
        ts = datetime.fromtimestamp(msg['timestamp']).strftime('%H:%M')
        role = msg['role']
        src = msg['source'] or 'voice'
        interactions.append(f"[{ts}] ({src}) {role}: {msg['content'][:300]}")

    interactions_text = "\n".join(interactions)
    prompt = BEHAVIORAL_EXTRACTION_PROMPT.format(interactions=interactions_text)

    try:
        response = await call_qwen_summary(prompt, max_tokens=500)

        if not response or "NESSUNO" in response.upper():
            logger.info(f"Speaker {speaker_id} ({speaker_name}): no behavioral facts extracted")
            return

        # Manda a mem0
        facts_text = response.strip()
        try:
            async with httpx.AsyncClient(timeout=MEM0_ADD_TIMEOUT) as client:
                resp = await client.post(
                    f"{MEM0_BASE_URL}/add",
                    json={"text": facts_text, "user_id": mem0_user_id},
                )
                resp.raise_for_status()
                result = resp.json()
                added = len(result.get("results", []))
                logger.info(
                    f"Speaker {speaker_id} ({speaker_name}) → mem0 user '{mem0_user_id}': "
                    f"{added} facts extracted from {len(messages)} interactions"
                )
        except Exception as e:
            logger.error(f"mem0 push failed for speaker {speaker_id}: {e}")

    except Exception as e:
        logger.error(f"Behavioral extraction failed for speaker {speaker_id}: {e}")


# ===========================================================================
# SCHEDULER
# ===========================================================================

async def memory_scheduler():
    """
    Background scheduler per job memoria.
    Da avviare come task in main.py.
    """
    # Attendi un po' prima di iniziare (permetti startup)
    await asyncio.sleep(30)
    logger.info("Memory scheduler started")

    while True:
        now = datetime.now()

        try:
            # Hourly summary ogni ora (al minuto configurato)
            if now.minute == config.MEMORY_HOURLY_TRIGGER_MINUTE:
                logger.info("Running hourly user memory summary...")
                await run_user_hourly_summary()

            # Daily summary + behavioral extraction all'ora configurata
            if now.hour == config.MEMORY_DAILY_TRIGGER_HOUR and now.minute == 0:
                logger.info("Running daily user memory summary...")
                await run_user_daily_summary()

                # Estrai abitudini comportamentali → mem0
                logger.info("Running behavioral extraction → mem0...")
                await run_mem0_behavioral_extraction()

                # Cleanup vecchi dati SQL
                cleanup_old_user_memory()

        except Exception as e:
            logger.error(f"Memory scheduler error: {e}")

        await asyncio.sleep(60)  # check ogni minuto
