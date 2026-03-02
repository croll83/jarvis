import aiohttp
import asyncio
import json
import logging
from typing import Optional

import config
from database import get_llm_params
from prompts import load_prompt

logger = logging.getLogger("JARVIS_AI")


# ===========================================================================
# GEMINI INTENTS (per validazione)
# ===========================================================================
GEMINI_INTENTS = ["GEMINI", "VERIFY_WITH_GEMINI"]

# ===========================================================================
# IMAGE GENERATION INTENTS
# ===========================================================================
IMAGE_GENERATION_KEYWORDS = [
    "genera un'immagine", "genera immagine", "genera un immagine",
    "crea un'immagine", "crea immagine", "crea un immagine",
    "genera un disegno", "crea un disegno",
    "genera una foto", "crea una foto",
    "disegnami", "disegna",
    "mostrami un", "mostrami una",
    "fai vedere", "fammi vedere",
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


def _is_dirty_audio(text: str) -> bool:
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
    MAX_ENTITY_MAP_CHARS = 10000  # ~2500 token, cap di sicurezza

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

_STT_NORMALIZE_SYSTEM = (
    "Sei un normalizzatore di testo trascritto da un sistema di riconoscimento vocale (Whisper) "
    "per un assistente domotico italiano chiamato JARVIS.\n"
    "Il tuo compito:\n"
    "1. Correggi errori di trascrizione: parole storpiate, lingue sbagliate, punteggiatura errata\n"
    "2. NON cambiare il significato o aggiungere parole\n"
    "3. NON aggiungere formattazione, virgolette o commenti\n"
    "4. Se il testo è già corretto, restituiscilo identico\n"
    "5. Rispondi SOLO con il testo corretto, nient'altro\n\n"
    "Contesto — entità domotiche note:\n"
    "Stanze: Ingresso, Soggiorno, Cucina, Lavanderia, Disimpegno, Camera, "
    "Cabina armadio, Cameretta, Bagno grande, Bagno piccolo, Balcone interno, Balcone esterno, Garage, Box\n"
    "Device: TV, Cam, Lampada, Lampada Giorgio, Luce, Luci, Porta, Soundbar, Echo\n"
    "Luci: Centro Block, Strip Led, Divano, Faretto, Tavola, Braava, Roomba, Letto, Specchio\n"
    "Persone: Marco, Ada, Giorgio, Sofia, Loredana, Mario, Melina\n"
    "Azioni: accendi, spegni, apri, chiudi, alza, abbassa, muta, stop, silenzio"
)


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
    """Normalizzazione STT via Ollama (Qwen local)."""
    payload = {
        "model": config.ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _STT_NORMALIZE_SYSTEM},
            {"role": "user", "content": text}
        ],
        "options": {
            "temperature": 0.1,
            "num_predict": 150
        },
        "stream": False
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.OLLAMA_CHAT_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=llm_params["timeout"])
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("message", {}).get("content", "")
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
            {"role": "system", "content": _STT_NORMALIZE_SYSTEM},
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
#   ALTRO            - everything else                    -> OpenClaw
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
    if _is_dirty_audio(text):
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
    """Pre-route classification via Ollama (Qwen local)."""
    payload = {
        "model": config.ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _PRE_ROUTE_SYSTEM},
            {"role": "user", "content": text}
        ],
        "format": "json",
        "options": {
            "temperature": llm_params["temperature"],
            "num_predict": 120
        },
        "stream": False
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.OLLAMA_CHAT_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=llm_params["timeout"])
            ) as resp:
                if resp.status != 200:
                    logger.error(f"pre_route ollama error: HTTP {resp.status}")
                    return None
                data = await resp.json()
                content = data.get("message", {}).get("content", "{}")
                return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"pre_route ollama bad JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"pre_route ollama exception: {e}")
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
    # Add Gemini availability flag
    context["gemini_available"] = config.GEMINI_ENABLED

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
    service_status = context.pop("service_status", "")
    service_status_section = f"\n\n[STATO SERVIZI]:\n{service_status}" if service_status and service_status != "tutti i servizi online" else ""

    # Gemini availability section
    gemini_section = ""
    if context.pop("gemini_available", False):
        gemini_section = "\n\n[GEMINI DISPONIBILE]: Puoi usare intent GEMINI o VERIFY_WITH_GEMINI se appropriato."

    # Carica entity map dal DB (per-location, con fallback a user location)
    location_id = context.get("location")
    user_id = context.get("speaker_id")
    entity_map_str = _get_entity_map_for_prompt(location_id, user_id=user_id)

    full_prompt = f"""[MAPPA ENTITÀ]:
{entity_map_str}

[CONTESTO]:
{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}{service_status_section}{gemini_section}

[COMANDO UTENTE]:
{text}"""

    # Log dimensioni per monitoring
    sys_chars = len(SYSTEM_RULES)
    map_chars = len(entity_map_str)
    total_chars = sys_chars + len(full_prompt)
    est_tokens = total_chars // 4
    logger.info(f"Router prompt: {total_chars} chars (~{est_tokens} tok) | system={sys_chars} map={map_chars} user_prompt={len(full_prompt) - map_chars}")

    return full_prompt


async def _qwen_routing_call(text: str, context: dict) -> dict:
    """Chiamata effettiva a Qwen locale (Ollama) per routing."""

    # Usa il builder comune per il prompt
    full_prompt = _build_routing_prompt(text, context)

    _rp = get_llm_params("routing")
    payload = {
        "model": config.ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": full_prompt}
        ],
        "format": "json",
        "options": {
            "temperature": _rp["temperature"],
            "num_predict": _rp["max_tokens"]
        },
        "stream": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(config.OLLAMA_CHAT_URL,
                                   json=payload, timeout=_rp["timeout"]) as resp:
                if resp.status != 200:
                    logger.error(f"Routing error: HTTP {resp.status}")
                    return _fallback_routing()

                data = await resp.json()
                content = data.get("message", {}).get("content", "{}")

                try:
                    result = json.loads(content)
                    return _validate_routing(result)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from router: {content}")
                    return _fallback_routing()

    except Exception as e:
        logger.error(f"Routing exception: {e}")
        return _fallback_routing()


def _validate_routing(result: dict) -> dict:
    """Valida e normalizza la risposta del router."""
    VALID_INTENTS = [
        "HOME_CONTROL", "SET_PREFERENCE", "SET_LOCATION",
        "AUDIT_REPORT", "SIMPLE_CHAT", "RETRY", "IMAGE_GENERATION",
    ] + GEMINI_INTENTS

    intent = result.get("intent", "SIMPLE_CHAT")
    if intent not in VALID_INTENTS:
        intent = "SIMPLE_CHAT"

    # Se Gemini intent ma non abilitato, fallback a SIMPLE_CHAT
    if intent in GEMINI_INTENTS and not config.GEMINI_ENABLED:
        logger.warning(f"Gemini intent {intent} requested but GEMINI not enabled, falling back to SIMPLE_CHAT")
        intent = "SIMPLE_CHAT"

    # Se IMAGE_GENERATION ma Gemini non abilitato, fallback
    if intent == "IMAGE_GENERATION" and not config.GEMINI_ENABLED:
        logger.warning("IMAGE_GENERATION requested but GEMINI not enabled, falling back to SIMPLE_CHAT")
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

async def get_quick_response(text: str, context: dict) -> str:
    """
    Risposta rapida per SIMPLE_CHAT.
    - AI_BACKEND=api: OpenRouter (Qwen API) → Gemini fallback
    - AI_BACKEND=local: Ollama (Qwen locale)
    Questo è il fallback quando OpenClaw è down, quindi NON usa OpenClaw.
    """
    system_prompt = load_prompt(
        "quick_response_system",
        "Sei Jarvis, un assistente domestico amichevole. Rispondi in modo conciso e naturale in italiano."
    )
    _rp = get_llm_params("quick_response")

    # ── CLOUD MODE: OpenRouter → Gemini fallback ──
    if config.AI_BACKEND == "api":
        # Tentativo 1: OpenRouter (Qwen via API)
        if config.OPENROUTER_API_KEY:
            try:
                result = await _quick_response_openrouter(text, system_prompt, _rp)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"Quick response OpenRouter failed: {e}")

        # Tentativo 2: Gemini API (solo testo semplice)
        if config.GEMINI_API_KEY:
            try:
                result = await get_gemini_response(f"[Rispondi brevemente in italiano] {text}")
                if result and not result.startswith("Errore"):
                    return result
            except Exception as e:
                logger.warning(f"Quick response Gemini failed: {e}")

        return "Mi dispiace, c'è stato un problema. Puoi ripetere?"

    # ── LOCAL MODE: Ollama ──
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text}
    ]
    payload = {
        "model": config.ROUTER_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": _rp["temperature"],
            "num_predict": _rp["max_tokens"]
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(config.OLLAMA_CHAT_URL,
                                   json=payload, timeout=_rp["timeout"]) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("message", {}).get("content", "Non ho capito, puoi ripetere?")

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


# ===========================================================================
# GEMINI API INTEGRATION
# ===========================================================================

async def get_gemini_response(text: str, history: list = None) -> str:
    """
    Ottiene risposta da Gemini API.
    Usato per intent GEMINI diretto.
    """
    if not config.GEMINI_API_KEY:
        return "Mi dispiace, Gemini non è configurato."

    # Gemini usa un formato leggermente diverso
    contents = []

    if history:
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    contents.append({"role": "user", "parts": [{"text": text}]})

    _rp = get_llm_params("gemini")
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": _rp["temperature"],
            "maxOutputTokens": _rp["max_tokens"]
        }
    }

    url = f"{config.GEMINI_API_URL}/models/{config.GEMINI_REASONING_MODEL}:generateContent"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}?key={config.GEMINI_API_KEY}",
                json=payload,
                timeout=config.API_TIMEOUT_REASONING
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    # Gemini response structure
                    candidates = result.get("candidates", [])
                    if candidates:
                        content = candidates[0].get("content", {})
                        parts = content.get("parts", [])
                        if parts:
                            return parts[0].get("text", "Nessuna risposta da Gemini")
                    return "Nessuna risposta da Gemini"
                else:
                    error = await resp.text()
                    logger.error(f"Gemini error: {resp.status} - {error}")
                    return f"Errore Gemini: {resp.status}"

    except asyncio.TimeoutError:
        logger.error(f"Gemini timeout after {config.API_TIMEOUT_REASONING}s")
        return "Mi dispiace, Gemini non ha risposto in tempo."
    except Exception as e:
        logger.error(f"Gemini exception: {e}")
        return f"Errore: {e}"


async def verify_with_gemini(original_question: str, previous_response: str) -> str:
    """
    Chiede a Gemini di verificare/integrare una risposta precedente.
    Usato per intent VERIFY_WITH_GEMINI.
    """
    if not config.GEMINI_API_KEY:
        return "Mi dispiace, Gemini non è configurato per la verifica."

    _tpl = load_prompt("gemini_verification")
    verification_prompt = _tpl.format(
        original_question=original_question,
        previous_response=previous_response
    )

    return await get_gemini_response(verification_prompt)


def is_gemini_intent(intent: str) -> bool:
    """Verifica se l'intent richiede Gemini."""
    return intent in GEMINI_INTENTS


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
        # Via Ollama locale
        _rp = get_llm_params("summary")
        payload = {
            "model": config.ROUTER_MODEL,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": _rp["temperature"],
                "num_predict": max_tokens or _rp["max_tokens"]
            }
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.OLLAMA_CHAT_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=_rp["timeout"])
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result.get("message", {}).get("content", "")
                else:
                    raise Exception(f"Ollama error: {resp.status}")
