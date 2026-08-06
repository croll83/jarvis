import aiohttp
import asyncio
import json
import logging
import os
import time
from typing import Optional

import config
from database import get_llm_params
from prompts import load_prompt

logger = logging.getLogger("JARVIS_AI")


# ===========================================================================
# AI_AGENT INTENTS (per validazione)
# ===========================================================================
AI_AGENT_INTENTS = ["AI_AGENT", "VERIFY_WITH_AI_AGENT"]

# ===========================================================================
# IMAGE GENERATION INTENTS
# ===========================================================================
IMAGE_GENERATION_KEYWORDS = [
    "genera un'immagine", "genera immagine", "genera un immagine",
    "crea un'immagine", "crea immagine", "crea un immagine",
    "genera un disegno", "crea un disegno",
    "genera una foto", "crea una foto",
    "disegnami", "disegna un", "disegna una",
    "mostrami un'immagine", "mostrami una foto", "mostrami un disegno",
    "fai vedere un'immagine", "fai vedere una foto",
    "fammi vedere un'immagine", "fammi vedere una foto",
    "genera l'immagine", "crea l'immagine",
]


def is_image_generation_intent(text: str) -> bool:
    """Verifica se il testo richiede generazione immagini."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in IMAGE_GENERATION_KEYWORDS)

# ===========================================================================
# DIRTY AUDIO DETECTION
# ===========================================================================

_MIN_MEANINGFUL_LENGTH = 3  # minimum chars for a meaningful command


def is_dirty_audio(text: str) -> bool:
    """
    Detect garbled / too-short voice transcriptions.
    Returns True if the text is likely not a real command.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_MEANINGFUL_LENGTH:
        return True
    # All punctuation / whitespace
    if not any(c.isalnum() for c in stripped):
        return True
    return False

# Caricamento system rules
try:
    with open(config.SYSTEM_RULES_PATH, 'r', encoding='utf-8') as f:
        SYSTEM_RULES = f.read()
except FileNotFoundError:
    logger.warning(f"System rules not found at {config.SYSTEM_RULES_PATH}")
    SYSTEM_RULES = "You are Jarvis, a home assistant."


async def _llm_chat(messages: list, temperature: float = 0.1,
                    max_tokens: int = 200, timeout: float = 15,
                    stop: list = None) -> Optional[str]:
    """Unified LLM chat call — routes to llama-server or Ollama based on config."""
    if config.ROUTER_ENGINE == "llamacpp":
        url = f"{config.ROUTER_URL}/v1/chat/completions"
        payload = {
            "model": config.ROUTER_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload,
                                       timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"llm_chat (llamacpp) error: {e}")
            return None
    else:
        payload = {
            "model": config.ROUTER_MODEL,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_gpu": 37,
            }
        }
        if stop:
            payload["options"]["stop"] = stop
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(config.OLLAMA_CHAT_URL, json=payload,
                                       timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return data.get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"llm_chat (ollama) error: {e}")
            return None


def _get_entity_map_for_prompt(location_id: Optional[str] = None, user_id: Optional[int] = None) -> str:
    """
    Recupera entity map dal database per il prompt del router.

    Ottimizzazioni:
    - Compact JSON (no indent) per risparmiare ~40% char
    - Se location_id noto → solo quella location
    - Se location_id unknown → cerca location dell'utente, poi fallback a tutte
    - Cap a MAX_ENTITY_MAP_CHARS per evitare di saturare il context
    - Formato compatto: rimuove livelli gerarchici inutili per il routing
    """
    MAX_ENTITY_MAP_CHARS = 12000  # ~3000 token, cap per ctx=20480 (preserva zone/piani)

    try:
        from database import get_entity_map_for_llm, get_all_locations, get_user_location

        target_locations = []

        if location_id and location_id != "unknown":
            target_locations = [location_id]
        elif user_id:
            # Telegram: location unknown → usa la location dell'utente
            user_loc = get_user_location(user_id)
            if user_loc and user_loc.location_id:
                target_locations = [user_loc.location_id]

        # Fallback: tutte le location
        if not target_locations:
            locations = get_all_locations(enabled_only=True)
            target_locations = [loc.id for loc in locations]

        all_maps = {}
        for loc_id in target_locations:
            em = get_entity_map_for_llm(loc_id)
            if em:
                all_maps[loc_id] = em

        if not all_maps:
            return "{}"

        # Compact JSON (no indent)
        result = json.dumps(all_maps, ensure_ascii=False, separators=(',', ':'))

        # Cap di sicurezza
        if len(result) > MAX_ENTITY_MAP_CHARS:
            # Prova a ridurre: flatten a Room → [entity_names] senza device layer
            result = _compact_entity_map(all_maps)
            if len(result) > MAX_ENTITY_MAP_CHARS:
                result = result[:MAX_ENTITY_MAP_CHARS - 20] + '..."TRONCATO"}'
                logger.warning(f"Entity map truncated to {MAX_ENTITY_MAP_CHARS} chars")

        return result

    except Exception as e:
        logger.warning(f"Could not load entity maps from DB: {e}")
        return "{}"


def _compact_entity_map(all_maps: dict) -> str:
    """
    Formato ultra-compatto dell'entity map quando supera il budget.
    Flatten: location → room → [entity_names] (rimuove zone/area/device nesting).
    """
    compact = {}
    for loc_id, loc_map in all_maps.items():
        rooms = {}
        _flatten_to_rooms(loc_map, rooms)
        compact[loc_id] = rooms
    return json.dumps(compact, ensure_ascii=False, separators=(',', ':'))


def _flatten_to_rooms(node: dict, rooms: dict, depth: int = 0):
    """Ricorsivamente flatten fino al livello room → entity type → [names]."""
    for key, value in node.items():
        if isinstance(value, dict):
            # Check se siamo al livello entity_type (value contiene liste)
            has_lists = any(isinstance(v, list) for v in value.values())
            if has_lists:
                # Questo è un room o device level con entity_type → [names]
                if key not in rooms:
                    rooms[key] = {}
                for etype, elist in value.items():
                    if isinstance(elist, list):
                        if etype not in rooms[key]:
                            rooms[key][etype] = []
                        rooms[key][etype].extend(
                            [e if isinstance(e, str) else e.get("name", str(e)) for e in elist]
                        )
                    elif isinstance(elist, dict):
                        # Device level sotto room: flatten le entity
                        for sub_etype, sub_elist in elist.items():
                            if isinstance(sub_elist, list):
                                if sub_etype not in rooms[key]:
                                    rooms[key][sub_etype] = []
                                rooms[key][sub_etype].extend(
                                    [e if isinstance(e, str) else e.get("name", str(e)) for e in sub_elist]
                                )
            else:
                # Vai più in profondità
                _flatten_to_rooms(value, rooms, depth + 1)
        elif isinstance(value, list):
            # Direttamente lista di entità
            if key not in rooms:
                rooms[key] = value


# ===========================================================================
# SECURITY CHECK (Rule-based + Prompt analysis)
# ===========================================================================

# Pattern sospetti per prompt injection
SUSPICIOUS_PATTERNS = [
    "ignora le istruzioni",
    "ignore the instructions",
    "dimentica le regole",
    "forget the rules",
    "fai finta di essere",
    "pretend to be",
    "il tuo vero obiettivo",
    "your real goal",
    "system prompt",
    "jailbreak",
    "DAN mode",
    "developer mode",
]

DANGEROUS_KEYWORDS = [
    "password", "token", "api key", "apikey", "secret",
    "seed phrase", "private key", "wallet password",
    "credit card", "carta di credito", "cvv",
]


async def is_safe(text: str, source: str = "unknown") -> tuple[bool, str]:
    """
    Verifica se il comando è sicuro.
    Restituisce (is_safe, reason).
    """
    text_lower = text.lower()

    # Check 1: Pattern sospetti (prompt injection)
    for pattern in SUSPICIOUS_PATTERNS:
        if pattern in text_lower:
            logger.warning(f"Suspicious pattern detected: '{pattern}' from {source}")
            return False, f"Pattern sospetto rilevato: {pattern}"

    # Check 2: Keyword pericolose
    for keyword in DANGEROUS_KEYWORDS:
        if keyword in text_lower:
            logger.warning(f"Dangerous keyword detected: '{keyword}' from {source}")
            return False, f"Keyword pericolosa rilevata: {keyword}"

    # Check 3: Per comandi da fonti esterne, verifica extra
    if source != "voice" and source != "telegram":
        # Qualsiasi comando "meta" da fonti esterne è sospetto
        if any(word in text_lower for word in ["esegui", "run", "execute", "eval"]):
            if "codice" in text_lower or "code" in text_lower or "script" in text_lower:
                return False, "Esecuzione codice non autorizzata da fonte esterna"

    return True, "OK"


# ===========================================================================
# STT NORMALIZATION (Qwen 7B — fix trascrizione Whisper)
# ===========================================================================
# Corregge errori comuni di Whisper: nomi entità, lingua mista, punteggiatura.
# Chiamata dopo STT e prima del pre-route. Se fallisce, ritorna testo originale.
# ===========================================================================

_STT_NORMALIZE_RULES = (
    "Sei un normalizzatore di testo trascritto da un sistema di riconoscimento vocale "
    "per un assistente domotico italiano chiamato JARVIS.\n"
    "Il tuo compito:\n"
    "1. Correggi errori di trascrizione: parole storpiate, lingue sbagliate, punteggiatura errata\n"
    "2. NON cambiare il significato o aggiungere parole\n"
    "3. NON aggiungere formattazione, virgolette o commenti\n"
    "4. Se il testo è già corretto, restituiscilo identico\n"
    "5. Rispondi SOLO con il testo corretto, nient'altro\n\n"
)

_stt_ctx_cache = (0.0, "")  # (timestamp, sezione contesto)


def _stt_normalize_system() -> str:
    """
    Prompt normalizzatore: regole statiche + contesto dinamico con le stanze/
    aree/zone REALI dall'entity map di tutte le location (niente liste
    hardcodate che invecchiano) e le storpiature note da STT_TARGET_ALIASES.
    Contesto in cache 5 min per evitare query DB a ogni comando vocale.
    """
    global _stt_ctx_cache
    now = time.time()
    if _stt_ctx_cache[1] and now - _stt_ctx_cache[0] < 300:
        return _STT_NORMALIZE_RULES + _stt_ctx_cache[1]

    rooms_line = ""
    try:
        from database import get_all_locations, get_entity_map_locations
        names: list = []
        for loc in get_all_locations():
            for n in get_entity_map_locations(loc.id):
                if n not in names:
                    names.append(n)
        if names:
            rooms_line = "Stanze e zone: " + ", ".join(names) + "\n"
    except Exception as e:
        logger.warning(f"STT normalize: entity map non disponibile per il prompt: {e}")

    by_canon: dict = {}
    for wrong, right in config.STT_TARGET_ALIASES.items():
        by_canon.setdefault(right, []).append(wrong)
    alias_lines = "".join(
        f"ATTENZIONE: '{canon}' viene spesso trascritto male "
        f"({', '.join(wrongs)}): nel contesto domotico correggilo in '{canon}'.\n"
        for canon, wrongs in by_canon.items()
    )

    context = (
        "Contesto — entità domotiche note:\n"
        + rooms_line
        + alias_lines
        + "Persone: Marco, Ada, Giorgio, Sofia, Loredana, Mario, Melina\n"
        "Azioni: accendi, spegni, apri, chiudi, cambia, imposta, alza, abbassa, muta, stop, silenzio"
    )
    _stt_ctx_cache = (now, context)
    return _STT_NORMALIZE_RULES + context


async def normalize_stt_text(text: str) -> str:
    """
    Normalizza il testo STT via Qwen per correggere errori di trascrizione.
    Se la chiamata LLM fallisce, ritorna il testo originale (fail-safe).
    Disabilitabile via config.STT_NORMALIZE_ENABLED = false.
    """
    if not config.STT_NORMALIZE_ENABLED:
        return text
    if not text or len(text.strip()) < 3:
        return text

    _rp = get_llm_params("routing")

    try:
        if config.AI_BACKEND == "api" and config.OPENROUTER_API_KEY:
            result = await _normalize_openrouter(text, _rp)
        else:
            result = await _normalize_ollama(text, _rp)

        if result and len(result.strip()) >= 2:
            # Sanity check: normalizzazione non dovrebbe stravolgere il testo
            if len(result) < len(text) * 3:
                if result.strip() != text.strip():
                    logger.info(f"STT normalized: '{text}' → '{result}'")
                return result.strip()
            else:
                logger.warning(f"STT normalize output troppo lungo, ignored: {len(result)} vs {len(text)}")
                return text
        return text

    except Exception as e:
        logger.warning(f"STT normalize failed (using original): {e}")
        return text


async def _normalize_ollama(text: str, llm_params: dict) -> Optional[str]:
    """Normalizzazione STT via LLM locale (Ollama o llama-server)."""
    messages = [
        {"role": "system", "content": _stt_normalize_system()},
        {"role": "user", "content": text}
    ]
    try:
        result = await _llm_chat(messages, temperature=0.1, max_tokens=150,
                                 timeout=llm_params["timeout"])
        return result
    except Exception as e:
        logger.warning(f"STT normalize ollama error: {e}")
        return None


async def _normalize_openrouter(text: str, llm_params: dict) -> Optional[str]:
    """Normalizzazione STT via OpenRouter API."""
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": config.OPENROUTER_REFERER,
        "X-Title": config.OPENROUTER_TITLE
    }
    payload = {
        "model": config.OPENROUTER_ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _stt_normalize_system()},
            {"role": "user", "content": text}
        ],
        "temperature": 0.1,
        "max_tokens": 150,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.OPENROUTER_API_URL}/chat/completions",
                headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=llm_params["timeout"])
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"STT normalize openrouter error: {e}")
        return None


# ===========================================================================
# PRE-ROUTE  (Qwen 7B  3-way classification)
# ===========================================================================
# Classifies a voice command into one of:
#   DOMOTICA_CERTA   - clearly a home-automation command  -> local Qwen
#   DOMOTICA_INCERTA - might be home-automation           -> local Qwen (with extra caution)
#   ALTRO            - everything else                    -> AI Agent
# ===========================================================================

_PRE_ROUTE_SYSTEM = (
    "Sei un classificatore di comandi vocali per un assistente domotico. "
    "Rispondi SOLO con un JSON valido, senza spiegazioni.\n"
    "Classi possibili:\n"
    "  DOMOTICA_CERTA   - il comando riguarda chiaramente la domotica "
    "(luci, tapparelle, clima, sensori, scene, stato della casa).\n"
    "  DOMOTICA_INCERTA - il comando potrebbe riguardare la domotica ma è ambiguo.\n"
    "  ALTRO            - il comando NON riguarda la domotica "
    "(ricerche web, domande generali, email, meteo, chat, conversazione).\n"
    "Formato risposta:\n"
    '{{"classification":"<CLASSE>","intent":"<breve descrizione>","confidence":<0.0-1.0>}}'
)


async def pre_route(text: str) -> dict:
    """
    3-way pre-routing via Qwen 7B.

    Returns dict:
        classification: DOMOTICA_CERTA | DOMOTICA_INCERTA | ALTRO
        intent:         short free-text description of detected intent
        confidence:     float 0.0 - 1.0
        payload:        {} (reserved for future use)
    """
    # Fast-path: dirty / garbled audio
    if is_dirty_audio(text):
        logger.debug(f"pre_route: dirty audio detected ({text!r})")
        return {
            "classification": "ALTRO",
            "intent": "audio_non_valido",
            "confidence": 0.1,
            "payload": {}
        }

    # Fast-path: explicit image-generation keyword -> skip LLM
    if is_image_generation_intent(text):
        return {
            "classification": "ALTRO",
            "intent": "IMAGE_GENERATION",
            "confidence": 0.95,
            "payload": {}
        }

    # --- Ask Qwen 7B ---------------------------------------------------------
    _rp = get_llm_params("routing")

    if config.AI_BACKEND == "api" and config.OPENROUTER_API_KEY:
        parsed = await _pre_route_openrouter(text, _rp)
    else:
        parsed = await _pre_route_ollama(text, _rp)

    if parsed is None:
        # LLM call failed -> safe fallback
        return {
            "classification": "ALTRO",
            "intent": "fallback_llm_error",
            "confidence": 0.2,
            "payload": {}
        }

    classification = parsed.get("classification", "ALTRO")
    # SESSIONE_LIVE rimossa dal prompt Qwen — gestita solo da keyword matching Python.
    # Se Qwen la emette comunque (hallucination), downgrade a ALTRO.
    if classification not in ("DOMOTICA_CERTA", "DOMOTICA_INCERTA", "ALTRO"):
        classification = "ALTRO"

    return {
        "classification": classification,
        "intent": str(parsed.get("intent", "")),
        "confidence": float(parsed.get("confidence", 0.5)),
        "payload": parsed.get("payload", {})
    }


async def _pre_route_ollama(text: str, llm_params: dict) -> Optional[dict]:
    """Pre-route classification via LLM locale (Ollama o llama-server)."""
    messages = [
        {"role": "system", "content": _PRE_ROUTE_SYSTEM},
        {"role": "user", "content": text}
    ]
    try:
        content = await _llm_chat(messages, temperature=llm_params["temperature"],
                                  max_tokens=120, timeout=llm_params["timeout"])
        if content is None:
            return None
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"pre_route bad JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"pre_route exception: {e}")
        return None


async def _pre_route_openrouter(text: str, llm_params: dict) -> Optional[dict]:
    """Pre-route classification via OpenRouter API."""
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": config.OPENROUTER_REFERER,
        "X-Title": config.OPENROUTER_TITLE
    }
    payload = {
        "model": config.OPENROUTER_ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _PRE_ROUTE_SYSTEM},
            {"role": "user", "content": text}
        ],
        "temperature": llm_params["temperature"],
        "max_tokens": 120,
        "response_format": {"type": "json_object"}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.OPENROUTER_API_URL}/chat/completions",
                headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=llm_params["timeout"])
            ) as resp:
                if resp.status != 200:
                    logger.error(f"pre_route openrouter error: HTTP {resp.status}")
                    return None
                result = await resp.json()
                content = result["choices"][0]["message"]["content"]
                return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"pre_route openrouter bad JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"pre_route openrouter exception: {e}")
        return None


# ===========================================================================
# ROUTING (Qwen 7B intent classification)
# ===========================================================================

async def get_routing(text: str, context: dict) -> dict:
    """
    Routing via Qwen 7B.
    Supports local (Ollama) or API (OpenRouter) backend.

    Valid intents returned by the LLM:
        HOME_CONTROL, SET_PREFERENCE, SET_LOCATION,
        AUDIT_REPORT, SIMPLE_CHAT, RETRY, IMAGE_GENERATION
    """
    # Add AI Agent availability flag
    context["ai_agent_available"] = config.AI_AGENT_ENABLED

    # LLM decides (local or API)
    if config.AI_BACKEND == "api":
        return await _get_routing_openrouter(text, context)
    else:
        return await _qwen_routing_call(text, context)


async def _get_routing_openrouter(text: str, context: dict) -> dict:
    """Routing via OpenRouter API (Qwen 2.5 7B)."""
    if not config.OPENROUTER_API_KEY:
        logger.error("OPENROUTER_API_KEY not configured, falling back to Ollama")
        return await _qwen_routing_call(text, context)

    # Costruisci prompt (stesso formato di Ollama)
    system_prompt = SYSTEM_RULES
    user_prompt = _build_routing_prompt(text, context)

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": config.OPENROUTER_REFERER,
        "X-Title": config.OPENROUTER_TITLE
    }

    _rp = get_llm_params("routing")
    payload = {
        "model": config.OPENROUTER_ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": _rp["temperature"],
        "max_tokens": _rp["max_tokens"],
        "response_format": {"type": "json_object"}
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.OPENROUTER_API_URL}/chat/completions",
                headers=headers, json=payload,
                timeout=config.API_TIMEOUT_ROUTING
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    content = result["choices"][0]["message"]["content"]
                    try:
                        parsed = json.loads(content)
                        return _validate_routing(parsed)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON from OpenRouter: {content}")
                        return _fallback_routing()
                else:
                    error = await resp.text()
                    logger.error(f"OpenRouter error: {resp.status} - {error}")
                    # Fallback a Ollama locale se disponibile
                    if config.OLLAMA_URL:
                        logger.warning("Falling back to Ollama routing")
                        return await _qwen_routing_call(text, context)
                    return _fallback_routing()

    except asyncio.TimeoutError:
        logger.error(f"OpenRouter timeout after {config.API_TIMEOUT_ROUTING}s")
        return _fallback_routing()
    except Exception as e:
        logger.error(f"OpenRouter exception: {e}")
        return _fallback_routing()


def _build_routing_prompt(text: str, context: dict) -> str:
    """Costruisce il prompt per il routing (usato sia da Ollama che OpenRouter)."""
    # Estrai service_status se presente
    service_status = context.get("service_status", "")
    service_status_section = f"\n\n[STATO SERVIZI]:\n{service_status}" if service_status and service_status != "tutti i servizi online" else ""

    # AI Agent availability section
    ai_agent_section = ""
    if context.get("ai_agent_available", False):
        ai_agent_section = "\n\n[AI_AGENT DISPONIBILE]: Puoi usare intent AI_AGENT o VERIFY_WITH_AI_AGENT se appropriato."

    # Previous intent per continuità multi-turn
    previous_intent_section = ""
    prev_intent = context.pop("previous_intent", None)
    prev_conf = context.pop("previous_confidence", None)
    prev_payload = context.pop("previous_payload", {})
    if prev_intent:
        # Includi dettagli entità per risolvere pronomi ("accendila" → quale entità?)
        payload_detail = ""
        if prev_payload:
            entity = prev_payload.get("entity", "")
            action = prev_payload.get("action", "")
            domain = prev_payload.get("domain", "")
            if entity:
                payload_detail = f" → {entity}"
                if action:
                    payload_detail += f", {action}"
                if domain:
                    payload_detail += f" ({domain})"
        previous_intent_section = f"\n\n[INTENT PRECEDENTE]: {prev_intent}{payload_detail} (confidence={prev_conf:.2f})"

    # Carica entity map dal DB (per-location, con fallback a user location)
    location_id = context.get("location")
    user_id = context.get("speaker_id")
    entity_map_str = _get_entity_map_for_prompt(location_id, user_id=user_id)

    # Estrai "memory" (conversazione recente) FUORI dal JSON contesto:
    # Qwen 3B/7B la perde dentro il blob JSON. Va come sezione markdown dedicata.
    memory_str = context.pop("memory", "") or ""
    memory_section = f"\n\n{memory_str}" if memory_str.strip() else ""

    full_prompt = f"""[MAPPA ENTITÀ]:
{entity_map_str}

[CONTESTO]:
{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}{service_status_section}{ai_agent_section}{previous_intent_section}{memory_section}

[COMANDO UTENTE]:
{text}"""

    # Log dimensioni per monitoring
    sys_chars = len(SYSTEM_RULES)
    map_chars = len(entity_map_str)
    total_chars = sys_chars + len(full_prompt)
    est_tokens = total_chars // 4
    logger.info(f"Router prompt: {total_chars} chars (~{est_tokens} tok) | system={sys_chars} map={map_chars} user_prompt={len(full_prompt) - map_chars}")
    if os.environ.get("ROUTER_DUMP_PROMPT") == "1":
        logger.info(f"=== FULL ROUTER PROMPT ===\n{full_prompt}\n=== END ROUTER PROMPT ===")

    return full_prompt


async def _qwen_routing_call(text: str, context: dict) -> dict:
    """Chiamata effettiva a Qwen locale per routing (Ollama o llama-server)."""

    # Usa il builder comune per il prompt
    full_prompt = _build_routing_prompt(text, context)
    _rp = get_llm_params("routing")

    if config.ROUTER_ENGINE == "llamacpp":
        return await _llamacpp_routing_call(full_prompt, _rp)
    else:
        return await _ollama_routing_call(full_prompt, _rp)


async def _llamacpp_routing_call(full_prompt: str, _rp: dict) -> dict:
    """Routing via llama-server (OpenAI-compatible API)."""
    payload = {
        "model": config.ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": full_prompt}
        ],
        "temperature": _rp["temperature"],
        "max_tokens": _rp["max_tokens"],
        "stop": ["<|im_start|>"],
        "stream": False
    }

    url = f"{config.ROUTER_URL}/v1/chat/completions"
    try:
        t0 = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                   timeout=aiohttp.ClientTimeout(total=_rp["timeout"])) as resp:
                t1 = time.time()
                if resp.status != 200:
                    logger.error(f"Routing error: HTTP {resp.status}")
                    return _fallback_routing()

                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                # Timing da llama-server
                timings = data.get("timings", {})
                usage = data.get("usage", {})
                ptok = usage.get("prompt_tokens", 0)
                etok = usage.get("completion_tokens", 0)
                prompt_ms = timings.get("prompt_ms", 0)
                predicted_ms = timings.get("predicted_ms", 0)
                logger.info(
                    f"Routing timing: total={((t1-t0)*1000):.0f}ms | "
                    f"llama.cpp: prompt={prompt_ms:.0f}ms({ptok}t) "
                    f"decode={predicted_ms:.0f}ms({etok}t) "
                    f"tok/s={timings.get('predicted_per_second', 0):.1f}"
                )

                try:
                    result = json.loads(content)
                    return _validate_routing(result)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from router: {content}")
                    return _fallback_routing()

    except Exception as e:
        logger.error(f"Routing exception (llamacpp): {e}", exc_info=True)
        return _fallback_routing()


async def _ollama_routing_call(full_prompt: str, _rp: dict) -> dict:
    """Routing via Ollama API (legacy)."""
    payload = {
        "model": config.ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": full_prompt}
        ],
        "options": {
            "temperature": _rp["temperature"],
            "num_predict": _rp["max_tokens"],
            "num_gpu": 37,
            "stop": ["<|im_start|>"],
        },
        "stream": False
    }

    try:
        t0 = time.time()
        async with aiohttp.ClientSession() as session:
            t1 = time.time()
            async with session.post(config.OLLAMA_CHAT_URL,
                                   json=payload, timeout=_rp["timeout"]) as resp:
                t2 = time.time()
                if resp.status != 200:
                    logger.error(f"Routing error: HTTP {resp.status}")
                    return _fallback_routing()

                data = await resp.json()
                t3 = time.time()
                content = data.get("message", {}).get("content", "{}").strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                ollama_load = data.get("load_duration", 0) / 1e6
                ollama_prompt = data.get("prompt_eval_duration", 0) / 1e6
                ollama_eval = data.get("eval_duration", 0) / 1e6
                ollama_total = data.get("total_duration", 0) / 1e6
                ptok = data.get("prompt_eval_count", 0)
                etok = data.get("eval_count", 0)
                logger.info(
                    f"Routing timing: session={((t1-t0)*1000):.0f}ms "
                    f"http={((t2-t1)*1000):.0f}ms parse={((t3-t2)*1000):.0f}ms | "
                    f"Ollama: total={ollama_total:.0f}ms load={ollama_load:.0f}ms "
                    f"prompt={ollama_prompt:.0f}ms({ptok}t) eval={ollama_eval:.0f}ms({etok}t)"
                )

                try:
                    result = json.loads(content)
                    return _validate_routing(result)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from router: {content}")
                    return _fallback_routing()

    except Exception as e:
        logger.error(f"Routing exception: {e}", exc_info=True)
        return _fallback_routing()


def _validate_routing(result: dict) -> dict:
    """Valida e normalizza la risposta del router."""
    VALID_INTENTS = [
        "HOME_CONTROL", "SET_LOCATION",
        "SIMPLE_CHAT", "RETRY", "IMAGE_GENERATION",
    ] + AI_AGENT_INTENTS

    intent = result.get("intent", "SIMPLE_CHAT")
    if intent not in VALID_INTENTS:
        intent = "SIMPLE_CHAT"

    # Se AI Agent intent ma non abilitato, fallback a SIMPLE_CHAT
    if intent in AI_AGENT_INTENTS and not config.AI_AGENT_ENABLED:
        logger.warning(f"AI Agent intent {intent} requested but AI Agent not configured, falling back to SIMPLE_CHAT")
        intent = "SIMPLE_CHAT"

    # Se IMAGE_GENERATION ma Gemini non abilitato, fallback
    if intent == "IMAGE_GENERATION" and not config.GEMINI_ENABLED:
        logger.warning("IMAGE_GENERATION requested but GEMINI_API_KEY not set, falling back to SIMPLE_CHAT")
        intent = "SIMPLE_CHAT"

    return {
        "intent": intent,
        "confidence": float(result.get("confidence", 0.5)),
        "response": result.get("response", ""),
        "interim_response": result.get("interim_response", "Hmm, ci penso..."),
        "payload": result.get("payload", {})
    }


def _fallback_routing() -> dict:
    """Routing di fallback in caso di errore."""
    return {
        "intent": "SIMPLE_CHAT",
        "confidence": 0.3,
        "response": "",
        "interim_response": "Ho avuto un problema, ma ci provo lo stesso...",
        "payload": {}
    }


# ===========================================================================
# QUICK RESPONSE (per intent semplici)
# ===========================================================================

async def get_quick_response(
    text: str,
    context: dict,
    user_id: int = None,
    location_id: str = None,
    enable_tools: bool = True
) -> str:
    """
    Risposta rapida per SIMPLE_CHAT con tool calling.
    - AI_BACKEND=api: OpenRouter (Qwen API) → Gemini fallback (no tools)
    - AI_BACKEND=local: Ollama (Qwen locale) con 4 tool:
        web_search, web_fetch, memory_search, home_status
    Questo è il fallback quando AI Agent è down, quindi NON usa AI Agent.
    """
    system_prompt = load_prompt(
        "quick_response_system",
        "Sei Jarvis, un assistente domestico amichevole. Rispondi in modo conciso e naturale in italiano."
    )
    _rp = get_llm_params("quick_response")

    # ── CLOUD MODE: OpenRouter → Gemini fallback (senza tools) ──
    if config.AI_BACKEND == "api":
        # Tentativo 1: OpenRouter (Qwen via API)
        if config.OPENROUTER_API_KEY:
            try:
                result = await _quick_response_openrouter(text, system_prompt, _rp)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"Quick response OpenRouter failed: {e}")

        return "Mi dispiace, c'è stato un problema. Puoi ripetere?"

    # ── LOCAL MODE: Ollama con Tool Calling ──
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text}
    ]

    if enable_tools:
        # Tool calling mode: usa call_qwen_with_tools per gestire il loop
        try:
            from web_tools import call_qwen_with_tools, ALL_TOOLS
            result = await call_qwen_with_tools(
                messages=messages,
                tools=ALL_TOOLS,
                max_iterations=3,
                user_id=user_id,
                location_id=location_id,
                temperature=_rp["temperature"],
                max_tokens=_rp.get("max_tokens", 500),
            )
            return result if result else "Non ho capito, puoi ripetere?"
        except Exception as e:
            logger.error(f"Quick response with tools error: {e}")
            # Fallback: prova senza tools
            logger.info("Falling back to quick response without tools")

    # Fallback senza tools (o se enable_tools=False)
    try:
        result = await _llm_chat(messages, temperature=_rp["temperature"],
                                 max_tokens=_rp.get("max_tokens", 200),
                                 timeout=_rp["timeout"])
        if result:
            return result
    except Exception as e:
        logger.error(f"Quick response error: {e}")

    return "Mi dispiace, c'è stato un problema. Puoi ripetere?"


async def _quick_response_openrouter(text: str, system_prompt: str, llm_params: dict) -> Optional[str]:
    """Quick response via OpenRouter API (Qwen). Text-only, no JSON parsing."""
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": config.OPENROUTER_REFERER,
        "X-Title": config.OPENROUTER_TITLE
    }
    payload = {
        "model": config.OPENROUTER_ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ],
        "temperature": llm_params.get("temperature", 0.7),
        "max_tokens": llm_params.get("max_tokens", 300),
    }
    timeout = aiohttp.ClientTimeout(total=llm_params.get("timeout", 15))
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{config.OPENROUTER_API_URL}/chat/completions",
            headers=headers, json=payload, timeout=timeout
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content.strip() if content else None
            else:
                logger.error(f"Quick response OpenRouter HTTP {resp.status}")
                return None


def is_ai_agent_intent(intent: str) -> bool:
    """Verifica se l'intent richiede AI Agent."""
    return intent in AI_AGENT_INTENTS


# ===========================================================================
# SUMMARY ENGINE (per memory jobs)
# ===========================================================================

async def call_qwen_summary(prompt: str, max_tokens: int = 150) -> str:
    """
    Chiama Qwen per task di summarization.
    Usato dai memory jobs per generare summaries orari/giornalieri.
    """
    messages = [
        {"role": "user", "content": prompt}
    ]

    if config.AI_BACKEND == "api":
        # Via OpenRouter
        headers = {
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        _rp = get_llm_params("summary")
        payload = {
            "model": config.OPENROUTER_ROUTER_MODEL,
            "messages": messages,
            "max_tokens": max_tokens or _rp["max_tokens"],
            "temperature": _rp["temperature"]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.OPENROUTER_API_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=_rp["timeout"])
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result["choices"][0]["message"]["content"]
                else:
                    raise Exception(f"Qwen API error: {resp.status}")
    else:
        # Via LLM locale (Ollama o llama-server)
        _rp = get_llm_params("summary")
        result = await _llm_chat(messages, temperature=_rp["temperature"],
                                 max_tokens=max_tokens or _rp["max_tokens"],
                                 timeout=_rp["timeout"])
        if result:
            return result
        raise Exception("LLM returned None")
