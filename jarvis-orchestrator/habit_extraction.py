"""
Habit Extraction — orchestrator -> mem0

Job notturno che analizza la chat history per-utente e crea/aggiorna
record long-term in mem0 sulle abitudini (sia comandi domotici ricorrenti
che preferenze conversazionali).

Pipeline (V2 — ibrida SQL + LLM):
  1. Domotica: aggregazione SQL deterministica su chat_memory.meta
     (route=HOME_CONTROL, entity_id, action, params, ha_status).
     GROUP BY (entity_id, action) -> conteggi, time_window, weekdays,
     frequency, value piu' comune. Confidence = funzione di count/span.
  2. Preferenze/topic: LLM (Qwen) sui soli messaggi non-HOME_CONTROL
     (route SIMPLE_CHAT / delegate / null) per rilevare interessi
     ricorrenti e preferenze conversazionali.
  3. Match/drift/upsert via mem0 (stessa logica V1).

Tutte le scritture su mem0 usano:
  - user_id = mem0 namespace (marco|ada|...)
  - agent_id = "jarvis-habit-extractor"
  - metadata.type = "habit"
  - content prefisso "[Habit] ..."
"""

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

import config
from ai_engines import call_qwen_summary
from context_bus import speaker_to_user_id
from database import _get_conn, get_all_users

logger = logging.getLogger("JARVIS_HABIT")

MEM0_BASE_URL = config.MEM0_BASE_URL
MEM0_TIMEOUT = 60.0
HABIT_AGENT_ID = "jarvis-habit-extractor"

# Prompt per la sola parte non-domotica (preferenze/topic).
HABIT_PROMPT_NON_DOMOTICA = """Analizza le interazioni delle ultime {lookback_days} giorni di {user_name}.
Considera SOLO messaggi conversazionali (NON comandi domotici, gia' aggregati a parte).
Estrai preferenze stabili e topic ricorrenti. Ignora menzioni singole o sporadiche.

Output JSON STRICT, una lista di oggetti, niente testo prima/dopo:
[
  {{
    "kind": "preference" | "topic",
    "entity": "string descrittiva o null",
    "action": "mention | prefer | null",
    "value": "valore associato (es. cucina_italiana) o null",
    "time_window": null,
    "frequency": "daily | weekly | sporadic",
    "weekdays": null,
    "confidence": 0.0..1.0,
    "sample_size": int,
    "description": "frase in italiano per il record mem0"
  }}
]

Vincoli:
  - Solo abitudini con sample_size >= {min_occurrences} e finestra di osservazione >= {min_span_days} giorni
  - confidence < {confidence_floor} -> ESCLUDI
  - Se nessuna abitudine valida, ritorna: []

Interazioni:
{interactions}
"""


# ===========================================================================
# DB — query separate per domotica (meta) e per topic (resto)
# ===========================================================================

def _fetch_home_control_events(speaker_id: int, lookback_days: int) -> List[Dict[str, Any]]:
    """Recupera eventi HOME_CONTROL con meta JSON parsata."""
    cutoff = time.time() - (lookback_days * 86400)
    conn = _get_conn()
    c = conn.cursor()
    # Solo righe utente (role='user') con route=HOME_CONTROL ed entity_id presente.
    c.execute("""
        SELECT
            timestamp,
            json_extract(meta, '$.ha_entity_id')  AS entity_id,
            json_extract(meta, '$.ha_domain')     AS domain,
            json_extract(meta, '$.ha_action')     AS action,
            json_extract(meta, '$.ha_params')     AS params_json,
            json_extract(meta, '$.ha_status')     AS status,
            json_extract(meta, '$.ha_location')   AS location,
            json_extract(meta, '$.ha_mode')       AS mode,
            content
        FROM chat_memory
        WHERE speaker_id = ?
          AND timestamp > ?
          AND role = 'user'
          AND json_extract(meta, '$.route') = 'HOME_CONTROL'
          AND json_extract(meta, '$.ha_entity_id') IS NOT NULL
        ORDER BY timestamp ASC
    """, (speaker_id, cutoff))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _fetch_non_domotica_messages(speaker_id: int, lookback_days: int) -> List[Dict[str, Any]]:
    """Recupera messaggi non-domotica (route != HOME_CONTROL o legacy senza meta)."""
    cutoff = time.time() - (lookback_days * 86400)
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, role, content, speaker_name, source,
               json_extract(meta, '$.route') AS route
        FROM chat_memory
        WHERE speaker_id = ? AND timestamp > ?
          AND (
                meta IS NULL
             OR json_extract(meta, '$.route') IS NULL
             OR json_extract(meta, '$.route') != 'HOME_CONTROL'
          )
        ORDER BY timestamp ASC
    """, (speaker_id, cutoff))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _format_interactions(messages: List[Dict[str, Any]], max_chars: int = 60000) -> str:
    """Formatta i messaggi per il prompt non-domotica."""
    lines = []
    for m in messages:
        dt = datetime.fromtimestamp(m["timestamp"]).strftime("%Y-%m-%d %H:%M")
        wd = datetime.fromtimestamp(m["timestamp"]).weekday()
        src = m.get("source") or "voice"
        role = m["role"]
        lines.append(f"[{dt} wd={wd} {src}] {role}: {m['content'][:250]}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


# ===========================================================================
# SQL AGGREGATION — domotica
# ===========================================================================

def _classify_frequency(count: int, span_days: float, weekday_hist: Counter) -> str:
    """Frequenza in base a count, span e distribuzione weekday."""
    if span_days <= 0:
        return "sporadic"
    per_day = count / max(span_days, 1)
    weekend_days = (weekday_hist.get(5, 0) + weekday_hist.get(6, 0))
    weekday_days = sum(weekday_hist.get(i, 0) for i in range(0, 5))
    total = weekend_days + weekday_days

    if per_day >= 0.7:
        # Pattern simil-quotidiano: distingui weekday/weekend se sbilanciato
        if total > 0 and weekend_days / total < 0.1:
            return "weekday"
        if total > 0 and weekday_days / total < 0.1:
            return "weekend"
        return "daily"
    if per_day >= 0.12:  # ~1x/settimana
        return "weekly"
    return "sporadic"


def _time_window_from_hours(hours: List[int]) -> Optional[Dict[str, str]]:
    """Stima una finestra HH:MM-HH:MM da una lista di ore."""
    if not hours:
        return None
    hist = Counter(hours)
    # Considera solo le ore che coprono >= 60% delle occorrenze.
    total = sum(hist.values())
    sorted_hours = sorted(hist.items(), key=lambda kv: kv[1], reverse=True)
    cumulative = 0
    picked: List[int] = []
    for h, cnt in sorted_hours:
        picked.append(h)
        cumulative += cnt
        if cumulative / total >= 0.6:
            break
    lo = min(picked)
    hi = max(picked)
    return {"start": f"{lo:02d}:00", "end": f"{(hi + 1) % 24:02d}:00"}


def _build_domotica_description(
    user_name: str,
    entity_id: str,
    domain: Optional[str],
    action: str,
    value: Optional[Any],
    time_window: Optional[Dict[str, str]],
    frequency: str,
) -> str:
    """Descrizione natural-language deterministica per habit domotica."""
    action_verb = {
        "turn_on": "accende",
        "turn_off": "spegne",
        "toggle": "cambia stato di",
        "open_cover": "apre",
        "close_cover": "chiude",
        "stop_cover": "ferma",
        "set_temperature": "imposta la temperatura di",
        "set_hvac_mode": "imposta la modalita' di",
        "set_cover_position": "posiziona",
        "volume_set": "imposta il volume di",
        "media_play": "avvia",
        "media_pause": "mette in pausa",
        "media_stop": "ferma",
        "lock": "blocca",
        "unlock": "sblocca",
    }.get(action, action)

    freq_phrase = {
        "daily": "ogni giorno",
        "weekday": "nei giorni feriali",
        "weekend": "nel weekend",
        "weekly": "una volta a settimana",
        "sporadic": "occasionalmente",
    }.get(frequency, frequency)

    entity_label = entity_id.split(".", 1)[1].replace("_", " ") if "." in entity_id else entity_id

    parts = [f"{user_name} {action_verb} {entity_label}"]
    if value is not None and value != "":
        parts.append(f"a {value}")
    if time_window:
        parts.append(f"tra le {time_window['start']} e le {time_window['end']}")
    parts.append(freq_phrase)
    return " ".join(parts)


def _aggregate_home_control_habits(
    user_name: str,
    events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Da eventi HOME_CONTROL produce habit deterministici (kind=domotica)."""
    if not events:
        return []

    # Group by (entity_id, action). Per i bulk ha_entity_id e' None: skippiamo.
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for ev in events:
        entity_id = ev.get("entity_id")
        action = ev.get("action")
        if not entity_id or not action:
            continue
        # Solo eventi riusciti contribuiscono al pattern.
        if (ev.get("status") or "ok") not in ("ok", "partial"):
            continue
        groups[(entity_id, action)].append(ev)

    habits: List[Dict[str, Any]] = []
    for (entity_id, action), evs in groups.items():
        count = len(evs)
        if count < config.HABIT_MIN_OCCURRENCES:
            continue
        timestamps = [e["timestamp"] for e in evs]
        span_days = (max(timestamps) - min(timestamps)) / 86400
        if span_days < config.HABIT_MIN_SPAN_DAYS:
            continue

        hours = [datetime.fromtimestamp(ts).hour for ts in timestamps]
        weekdays_hist = Counter(datetime.fromtimestamp(ts).weekday() for ts in timestamps)
        time_window = _time_window_from_hours(hours)
        frequency = _classify_frequency(count, span_days, weekdays_hist)

        # Estrai value piu' frequente dai params (brightness/temperature/etc.).
        value: Optional[Any] = None
        value_counter: Counter = Counter()
        for e in evs:
            params_raw = e.get("params_json")
            if not params_raw:
                continue
            try:
                params = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(params, dict):
                continue
            for key in ("temperature", "brightness", "brightness_pct", "percentage",
                        "position", "volume_level", "hvac_mode", "preset_mode"):
                if key in params and params[key] is not None:
                    value_counter[(key, json.dumps(params[key], ensure_ascii=False))] += 1
                    break
        if value_counter:
            (vk, vv), _ = value_counter.most_common(1)[0]
            try:
                value = f"{vk}={json.loads(vv)}"
            except Exception:
                value = f"{vk}={vv}"

        # Confidence: scala con sample density (count/span) + bonus se finestra oraria stretta.
        per_day = count / max(span_days, 1)
        density_conf = min(per_day / 1.0, 1.0)  # >=1/giorno -> 1.0
        consistency_bonus = 0.0
        if time_window:
            try:
                start_h = int(time_window["start"].split(":")[0])
                end_h = int(time_window["end"].split(":")[0])
                width = (end_h - start_h) % 24
                if width <= 2:
                    consistency_bonus = 0.15
                elif width <= 4:
                    consistency_bonus = 0.05
            except Exception:
                pass
        confidence = min(0.55 + 0.4 * density_conf + consistency_bonus, 0.99)
        if confidence < config.HABIT_CONFIDENCE_FLOOR:
            continue

        weekdays_list = sorted(weekdays_hist.keys())

        # Location attribution per behavioral analysis cross-house:
        # ha_location e' gia' letta da chat_memory.meta. Estraiamo la
        # location DOMINANTE per questo (entity_id, action) e l'elenco
        # delle locations osservate. Il fact rimane visibile a tutti gli
        # agenti (cross-house by design) ma con metadata che permette
        # query/filtering tipo "abitudini di Marco a Napoli".
        location_counter: Counter = Counter()
        for e in evs:
            loc = e.get("location")
            if loc:
                location_counter[loc] += 1
        location_dominant = location_counter.most_common(1)[0][0] if location_counter else None
        locations_seen = sorted(location_counter.keys())

        domain = evs[-1].get("domain") or (entity_id.split(".", 1)[0] if "." in entity_id else None)
        description = _build_domotica_description(
            user_name=user_name,
            entity_id=entity_id,
            domain=domain,
            action=action,
            value=value,
            time_window=time_window,
            frequency=frequency,
        )

        habits.append({
            "kind": "domotica",
            "entity": entity_id,
            "action": action,
            "value": value,
            "time_window": time_window,
            "frequency": frequency,
            "weekdays": weekdays_list,
            "confidence": round(confidence, 3),
            "sample_size": count,
            "description": description,
            "location": location_dominant,
            "locations_seen": locations_seen,
        })

    return habits


# ===========================================================================
# MEM0 I/O
# ===========================================================================

async def _mem0_search_habits(user_id: str) -> List[Dict[str, Any]]:
    """Cerca tutti gli habit esistenti in mem0 per un utente."""
    try:
        async with httpx.AsyncClient(timeout=MEM0_TIMEOUT) as client:
            resp = await client.post(
                f"{MEM0_BASE_URL}/search_contextual?summarize=false",
                json={
                    "query": "abitudini ricorrenti",
                    "user_id": user_id,
                    "agent_id": HABIT_AGENT_ID,
                    "limit": 100,
                },
            )
            if resp.status_code != 200:
                logger.warning(f"mem0 search habits failed: {resp.status_code}")
                return []
            data = resp.json()
            return data.get("results") or data.get("memories") or []
    except Exception as e:
        logger.warning(f"mem0 search habits exception: {e}")
        return []


async def _mem0_add_habit(user_id: str, habit: Dict[str, Any]) -> Optional[str]:
    """Aggiunge un nuovo habit in mem0. Ritorna l'id se ok."""
    content = f"[Habit] {habit['description']}"
    metadata = {
        "type": "habit",
        "kind": habit.get("kind"),
        "entity": habit.get("entity"),
        "action": habit.get("action"),
        "value": habit.get("value"),
        "time_window": habit.get("time_window"),
        "frequency": habit.get("frequency"),
        "weekdays": habit.get("weekdays"),
        "confidence": habit.get("confidence"),
        "sample_size": habit.get("sample_size"),
        "location": habit.get("location"),                 # dominante (wagmi|albani20|None)
        "locations_seen": habit.get("locations_seen"),     # tutte le location osservate
        "last_seen": datetime.now().strftime("%Y-%m-%d"),
        "version": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=MEM0_TIMEOUT) as client:
            resp = await client.post(
                f"{MEM0_BASE_URL}/add",
                json={
                    "messages": [{"role": "system", "content": content}],
                    "user_id": user_id,
                    "agent_id": HABIT_AGENT_ID,
                    "metadata": metadata,
                },
            )
            resp.raise_for_status()
            result = resp.json()
            return (result.get("results") or [{}])[0].get("id")
    except Exception as e:
        logger.error(f"mem0 add habit failed: {e}")
        return None


async def _mem0_update_habit(memory_id: str, user_id: str, habit: Dict[str, Any], prev_version: int) -> bool:
    """Aggiorna un habit esistente (nuova versione)."""
    content = f"[Habit] {habit['description']}"
    metadata = {
        "type": "habit",
        "kind": habit.get("kind"),
        "entity": habit.get("entity"),
        "action": habit.get("action"),
        "value": habit.get("value"),
        "time_window": habit.get("time_window"),
        "frequency": habit.get("frequency"),
        "weekdays": habit.get("weekdays"),
        "confidence": habit.get("confidence"),
        "sample_size": habit.get("sample_size"),
        "location": habit.get("location"),                 # dominante (wagmi|albani20|None)
        "locations_seen": habit.get("locations_seen"),     # tutte le location osservate
        "last_seen": datetime.now().strftime("%Y-%m-%d"),
        "version": prev_version + 1,
    }
    try:
        async with httpx.AsyncClient(timeout=MEM0_TIMEOUT) as client:
            resp = await client.put(
                f"{MEM0_BASE_URL}/memories/{memory_id}",
                json={"data": content, "metadata": metadata},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"mem0 update habit {memory_id} failed: {e}")
        return False


# ===========================================================================
# MATCH / DRIFT
# ===========================================================================

def _match_existing(habit: Dict[str, Any], existing: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Trova un habit esistente con stesso entity+action (o stesso kind+topic)."""
    h_entity = (habit.get("entity") or "").lower()
    h_action = (habit.get("action") or "").lower()
    h_kind = habit.get("kind")

    for ex in existing:
        meta = ex.get("metadata") or {}
        if meta.get("type") != "habit":
            continue
        ex_entity = (meta.get("entity") or "").lower()
        ex_action = (meta.get("action") or "").lower()
        if h_entity and h_entity == ex_entity and h_action == ex_action:
            return ex
        if not h_entity and h_kind == meta.get("kind"):
            if habit.get("description", "")[:40].lower() in (ex.get("memory") or "").lower():
                return ex
    return None


def _has_drift(habit: Dict[str, Any], existing_meta: Dict[str, Any], threshold: float) -> bool:
    """Drift significativo su time_window, frequency o value."""
    if habit.get("frequency") != existing_meta.get("frequency"):
        return True
    new_tw = habit.get("time_window") or {}
    old_tw = existing_meta.get("time_window") or {}
    if new_tw and old_tw:
        try:
            def _to_min(hm):
                h, m = hm.split(":")
                return int(h) * 60 + int(m)
            shift_start = abs(_to_min(new_tw.get("start", "00:00")) - _to_min(old_tw.get("start", "00:00")))
            shift_end = abs(_to_min(new_tw.get("end", "00:00")) - _to_min(old_tw.get("end", "00:00")))
            max_shift = max(shift_start, shift_end) / (24 * 60)
            if max_shift > threshold:
                return True
        except Exception:
            return False
    if habit.get("value") and habit.get("value") != existing_meta.get("value"):
        return True
    return False


# ===========================================================================
# PER-USER PIPELINE
# ===========================================================================

async def _extract_non_domotica_habits(user_id: str, speaker_id: int, user_name: str) -> List[Dict[str, Any]]:
    """LLM extraction sui soli messaggi non-HOME_CONTROL (topic/preferenze)."""
    messages = _fetch_non_domotica_messages(speaker_id, config.HABIT_LOOKBACK_DAYS)
    if len(messages) < config.HABIT_MIN_OCCURRENCES * 2:
        return []

    interactions = _format_interactions(messages)
    prompt = HABIT_PROMPT_NON_DOMOTICA.format(
        lookback_days=config.HABIT_LOOKBACK_DAYS,
        user_name=user_name or user_id,
        min_occurrences=config.HABIT_MIN_OCCURRENCES,
        min_span_days=config.HABIT_MIN_SPAN_DAYS,
        confidence_floor=config.HABIT_CONFIDENCE_FLOOR,
        interactions=interactions,
    )
    try:
        response = await call_qwen_summary(prompt, max_tokens=1500)
    except Exception as e:
        logger.error(f"User {user_id} non-domotica habit LLM call failed: {e}")
        return []

    response = response.strip()
    start = response.find("[")
    end = response.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        habits = json.loads(response[start:end + 1])
    except json.JSONDecodeError as e:
        logger.warning(f"User {user_id}: non-domotica JSON parse failed: {e}")
        return []
    return [h for h in habits if h.get("confidence", 0) >= config.HABIT_CONFIDENCE_FLOOR]


async def _process_user(speaker_id: int, speaker_name: str):
    user_id = speaker_to_user_id(speaker_id, speaker_name)
    if user_id == "shared":
        logger.info(f"Skipping habit extraction for unmapped speaker {speaker_id} ({speaker_name})")
        return

    # 1. DOMOTICA — SQL aggregation deterministica.
    events = _fetch_home_control_events(speaker_id, config.HABIT_LOOKBACK_DAYS)
    domotica_habits = _aggregate_home_control_habits(speaker_name or user_id, events)

    # 2. NON-DOMOTICA — LLM su messaggi conversazionali.
    non_domotica_habits = await _extract_non_domotica_habits(user_id, speaker_id, speaker_name)

    habits = domotica_habits + non_domotica_habits
    if not habits:
        logger.info(f"User {user_id}: no habits detected (events={len(events)})")
        return

    # 3. Match + upsert via mem0.
    existing = await _mem0_search_habits(user_id)
    added = updated = refreshed = 0

    for habit in habits:
        match = _match_existing(habit, existing)
        if match is None:
            if await _mem0_add_habit(user_id, habit):
                added += 1
        else:
            ex_meta = match.get("metadata") or {}
            prev_version = int(ex_meta.get("version", 1))
            if _has_drift(habit, ex_meta, config.HABIT_DRIFT_THRESHOLD):
                if await _mem0_update_habit(match.get("id"), user_id, habit, prev_version):
                    updated += 1
            else:
                merged = {**habit}
                merged["sample_size"] = max(habit.get("sample_size", 0), ex_meta.get("sample_size", 0))
                if await _mem0_update_habit(match.get("id"), user_id, merged, prev_version):
                    refreshed += 1

    logger.info(
        f"User {user_id} habit extraction: domotica={len(domotica_habits)} "
        f"non_domotica={len(non_domotica_habits)} -> "
        f"added={added} updated={updated} refreshed={refreshed}"
    )


# ===========================================================================
# ENTRY POINT
# ===========================================================================

async def run_habit_extraction_job():
    """Job notturno: itera su utenti attivi e estrae habit -> mem0."""
    users = get_all_users()
    if not users:
        logger.info("No users in DB, skip habit extraction")
        return

    for u in users:
        try:
            await _process_user(u.id, u.name)
        except Exception as e:
            logger.error(f"Habit extraction failed for user {u.id} ({u.name}): {e}")
