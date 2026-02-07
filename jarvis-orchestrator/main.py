"""
JARVIS Core Orchestrator
- Voice command processing con speaker identification
- Weighted memory context (differenziato per router vs reasoning)
- Intent routing con fallback a OpenClaw per comandi avanzati
"""

import asyncio
import time
import re
import uuid
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
import uvicorn

# Import moduli JARVIS
import config
from database import (
    init_db, smart_cache, log_event,
    save_chat_message, get_weighted_context, format_weighted_context_for_llm,
    save_action, get_action, delete_action, cleanup_old_actions,
    set_user_preference, get_user_preference, set_global_preference, get_global_preference,
    get_audit_summary, save_telegram_stream,
    get_telegram_stream, clear_telegram_stream,
    get_user_by_name, get_user_by_id, User,
    # Multi-location
    get_all_locations, get_location, get_user_location, set_user_location, clear_user_location,
    # Telegram auth
    get_user_by_telegram_id, is_telegram_authorized,
    # Default location (runtime from DB)
    get_default_location_id
)
from integrations import (
    call_hass_service, speak, send_telegram, edit_telegram,
    send_telegram_approval, denoise_audio, transcribe_audio,
    quick_feedback, speak_with_sound, play_feedback_sound
)
from ai_engines import (
    is_safe, get_routing, get_quick_response, pre_route,
    # Gemini
    get_gemini_response, verify_with_gemini, is_gemini_intent,
    # Image generation
    is_image_generation_intent
)
from security import SecurityManager, should_require_approval, get_approval_priority
from security_levels import check_security, needs_approval as needs_l3_approval, SecurityLevel, get_security_summary
from tools_api import router as tools_router
from voice_recognition import voice_recognizer, get_speaker_context
from user_api import router as user_router, web_router
from admin_api import router as admin_router, metrics as admin_metrics
from auth_api import router as auth_router
from device_api import router as device_router, get_device_speaker_config
from image_api import router as image_router
from service_status import service_status, ServiceState
from multi_ha import multi_ha
from memory_jobs import memory_scheduler
from location_memory import load_memory_services_from_db
from context_builder import build_full_context, build_routing_context, build_reasoning_context
from proactive import proactive_check_loop
from vector_store import init_vector_store

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("JARVIS_MAIN")

# Tracking stato "speaking" per room (per notificare AtomS3R)
# Struttura: {"salotto": {"speaking": True, "started_at": timestamp, "device_id": "..."}}
speaking_state: dict = {}
speaking_state_lock = asyncio.Lock()

# Device DND status (per sapere quali device sono in DND)
device_dnd_status: dict = {}


# ===========================================================================
# LIFECYCLE
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestisce startup e shutdown dell'applicazione."""
    global security
    logger.info("🚀 JARVIS Core starting...")
    init_db()

    # Inizializza security manager (richiede DB pronto)
    security = SecurityManager()

    # Inizializza vector store
    init_vector_store()

    # Avvia task periodici
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(warmup_models())
    asyncio.create_task(periodic_health_check())

    # Carica memory services e avvia scheduler memoria
    load_memory_services_from_db()
    asyncio.create_task(memory_scheduler())
    asyncio.create_task(proactive_check_loop())

    logger.info("✅ JARVIS Core ready!")
    yield
    logger.info("👋 JARVIS Core shutting down...")


app = FastAPI(title="Jarvis Core Orchestrator", lifespan=lifespan)

# Registra routers
app.include_router(user_router)
app.include_router(web_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(device_router)
app.include_router(image_router)
app.include_router(tools_router)


# ===========================================================================
# MIDDLEWARE — Access Logging per sicurezza
# ===========================================================================

# Path da escludere dal logging (troppo frequenti / non rilevanti)
_ACCESS_LOG_SKIP_PREFIXES = ("/assets/", "/health", "/favicon")

@app.middleware("http")
async def access_logging_middleware(request: Request, call_next):
    """Logga tutte le richieste HTTP per monitoraggio sicurezza."""
    start_time = time.time()

    response = await call_next(request)

    # Skip path non rilevanti
    path = request.url.path
    if any(path.startswith(p) for p in _ACCESS_LOG_SKIP_PREFIXES):
        return response

    elapsed_ms = (time.time() - start_time) * 1000
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")

    # Tenta di recuperare user_id dalla sessione (non bloccante)
    user_id = None
    try:
        from auth_api import get_current_user
        user = get_current_user(request)
        if user:
            user_id = user.id
    except Exception:
        pass

    from database import log_access
    log_access(
        method=request.method,
        path=path,
        status_code=response.status_code,
        ip_address=ip_address,
        user_agent=user_agent,
        user_id=user_id,
        response_time_ms=elapsed_ms
    )

    return response


async def periodic_cleanup():
    """Task periodico per pulizia DB e immagini generate."""
    while True:
        await asyncio.sleep(config.INTERVALS["cleanup"])
        cleanup_old_actions(timeout=config.APPROVAL_TIMEOUT)

        # Cleanup immagini generate (se Gemini abilitato)
        if config.GEMINI_ENABLED:
            try:
                from image_generation import cleanup_old_images
                await cleanup_old_images(config.IMAGE_CLEANUP_HOURS)
            except Exception as e:
                logger.warning(f"Image cleanup failed: {e}")

        # Cleanup access log e auth attempts vecchi (>30 giorni)
        try:
            from database import cleanup_old_access_logs
            cleanup_old_access_logs(max_age_days=30)
        except Exception as e:
            logger.warning(f"Access log cleanup failed: {e}")

        logger.debug("Cleanup completed")


async def warmup_models():
    """Preload dei modelli all'avvio."""
    try:
        await asyncio.sleep(5)
        logger.info("Warming up models...")
        await get_routing("test", {"source": "warmup"})
        logger.info("✅ Router model ready")
    except Exception as e:
        logger.warning(f"Model warmup failed: {e}")


async def periodic_health_check():
    """Task periodico per health check dei servizi esterni."""
    # Primo check dopo 10 secondi (permette warmup)
    await asyncio.sleep(10)
    logger.info("🏥 Starting periodic health checks...")

    while True:
        try:
            summary = await service_status.check_all()
            online_count = sum(1 for s in summary.values() if s == ServiceState.ONLINE)
            total = len(summary)
            logger.debug(f"Health check: {online_count}/{total} services online")

            # Log warning se servizi critici offline
            if not service_status.is_critical_online():
                logger.warning("⚠️ CRITICAL: Router model offline!")

            # Pubblica su SSE
            try:
                from event_bus import event_bus
                await event_bus.publish("health_update", {
                    "online_count": online_count,
                    "total": total,
                    "services": {k: v.value for k, v in summary.items()},
                    "critical_online": service_status.is_critical_online()
                })
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Health check error: {e}")

        await asyncio.sleep(config.INTERVALS["health_check"])


# ===========================================================================
# INITIALIZATION
# ===========================================================================

security: SecurityManager = None  # type: ignore — inizializzato in lifespan dopo init_db()

# Mapping sinonimi preferenze
CANONICAL_KEYS = {
    "fine_orario_notturno": "silent_hour_end",
    "fine_notte": "silent_hour_end",
    "inizio_orario_notturno": "silent_hour_start",
    "inizio_notte": "silent_hour_start",
    "modalità_silenziosa": "dnd_mode",
    "non_disturbare": "dnd_mode",
}


def get_confidence_thresholds() -> tuple[float, float]:
    """
    Recupera le soglie di confidenza dal DB (global_preferences).
    Fallback ai valori di config.py se non presenti.
    """
    high = get_global_preference("confidence_threshold_high")
    low = get_global_preference("confidence_threshold_low")

    try:
        high_val = float(high) if high else config.CONFIDENCE_THRESHOLD_HIGH
    except (ValueError, TypeError):
        high_val = config.CONFIDENCE_THRESHOLD_HIGH

    try:
        low_val = float(low) if low else config.CONFIDENCE_THRESHOLD_LOW
    except (ValueError, TypeError):
        low_val = config.CONFIDENCE_THRESHOLD_LOW

    return high_val, low_val


# ===========================================================================
# HELPER: Speaking State (per AtomS3R)
# ===========================================================================

async def set_speaking_state(room: str, speaking: bool, device_id: str = None):
    """Imposta lo stato speaking per una room."""
    async with speaking_state_lock:
        if speaking:
            speaking_state[room] = {
                "speaking": True,
                "started_at": time.time(),
                "device_id": device_id
            }
            logger.info(f"🔊 Speaking state ON for room: {room}")
        else:
            if room in speaking_state:
                del speaking_state[room]
                logger.info(f"🔇 Speaking state OFF for room: {room}")


async def clear_speaking_state_after_delay(room: str, delay_seconds: float = None):
    """Pulisce lo stato speaking dopo un delay (tempo stimato TTS)."""
    if delay_seconds is None:
        delay_seconds = config.INTERVALS["tts_clear_delay"]
    await asyncio.sleep(delay_seconds)
    async with speaking_state_lock:
        if room in speaking_state:
            if time.time() - speaking_state[room]["started_at"] >= delay_seconds - 0.5:
                del speaking_state[room]
                logger.info(f"🔇 Speaking state auto-cleared for room: {room}")


# ===========================================================================
# HELPER: Location Context
# ===========================================================================

def extract_location_from_device(device_id: str) -> Optional[str]:
    """
    Estrae location da device_id.
    atoms3r_wagmi_salotto -> wagmi
    atoms3r_albani_camera -> albani
    """
    if not device_id:
        return None
    parts = device_id.lower().split('_')
    if len(parts) >= 2:
        potential_location = parts[1]
        # Verifica che sia una location valida
        loc = get_location(potential_location)
        if loc:
            return potential_location
    return None


async def resolve_telegram_location(user_id: int, text: str, router_data: dict) -> tuple:
    """
    Risolve la location per un comando Telegram.

    Returns:
        (location_id, needs_keyboard): location risolta o None, True se serve keyboard
    """
    # 1. Check se il router ha parsato una location esplicita
    payload_location = router_data.get("payload", {}).get("location")
    if payload_location and payload_location not in ["unknown", "reset"]:
        # Verifica che la location esista
        loc = get_location(payload_location)
        if loc and loc.enabled:
            # Aggiorna user location (inline)
            set_user_location(user_id, payload_location, "telegram_inline")
            return payload_location, False

    # 2. Check sticky location
    user_loc = get_user_location(user_id)
    if user_loc and user_loc.source in ["telegram_sticky", "telegram_inline"]:
        # Verifica che la location sia ancora valida
        loc = get_location(user_loc.location_id)
        if loc and loc.enabled:
            return user_loc.location_id, False

    # 3. Nessuna location -> serve keyboard
    return None, True


async def send_location_keyboard(chat_id: str, original_text: str, action_context: dict):
    """Invia keyboard inline per selezione location."""
    import aiohttp

    locations = get_all_locations(enabled_only=True)

    # Serializza contesto per callback
    action_id = str(uuid.uuid4())[:8]
    save_action(action_id, {
        "type": "location_select",
        "original_text": original_text,
        "action_context": action_context
    })

    keyboard = {
        "inline_keyboard": [[
            {"text": f"🏠 {loc.name}", "callback_data": f"loc_{loc.id}_{action_id}"}
            for loc in locations
        ]]
    }

    url = f"{config.TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "📍 In quale casa vuoi eseguire questo comando?",
        "reply_markup": keyboard
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=config.TIMEOUTS["telegram"]) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"Send location keyboard error: {e}")
        return False


def get_room_speakers(location_id: str) -> dict:
    """
    Restituisce il mapping stanza -> speaker per una location.
    Legge dall'entity map nel database, con fallback a valori di default.
    """
    from database import get_entities_by_type

    # Cerca media_players nell'entity map della location
    try:
        media_players = get_entities_by_type(location_id, "media_player")
        if media_players:
            speakers = {}
            for mp in media_players:
                room = mp["room"].lower()
                # Converti nome entity in entity_id (es. "Echo Camera" -> "media_player.echo_camera")
                entity_name = mp["name"].lower().replace(" ", "_")
                entity_id = f"media_player.{entity_name}"
                # Prendi il primo speaker di ogni stanza
                if room not in speakers:
                    speakers[room] = entity_id
            if speakers:
                return speakers
    except Exception as e:
        logger.warning(f"Could not load speakers from DB for {location_id}: {e}")

    # Fallback: se entity map non disponibile, usa solo il fallback speaker da config/DB
    fallback_speaker = get_global_preference("default_fallback_speaker", config.DEFAULT_FALLBACK_SPEAKER)
    return {"default": fallback_speaker}


# ===========================================================================
# HELPER: Speaker Context
# ===========================================================================

def build_speaker_context(audio_bytes: Optional[bytes], source: str, explicit_speaker: Optional[str] = None) -> dict:
    """
    Costruisce il contesto speaker da varie fonti.

    Priority:
    1. Speaker esplicito (es. da Telegram con username)
    2. Voice recognition (da audio)
    3. Fallback a "Sconosciuto"
    """
    if explicit_speaker:
        user = get_user_by_name(explicit_speaker)
        if user:
            return {
                "speaker_id": user.id,
                "speaker_name": user.name,
                "is_admin": user.is_admin,
                "speaker_identified": True,
                "identification_method": "explicit"
            }

    if audio_bytes and source == "AtomS3R":
        return get_speaker_context(audio_bytes)

    # Fallback
    return {
        "speaker_id": None,
        "speaker_name": "Sconosciuto",
        "is_admin": False,
        "speaker_identified": False,
        "identification_method": "none"
    }


# ===========================================================================
# OPENCLAW STUB (Phase 4)
# ===========================================================================

async def forward_to_openclaw(text: str, context: dict, hint: str = "") -> str:
    """
    Forward request to OpenClaw Gateway (Gemini 3 Pro brain).

    Sends the user message with speaker context to OpenClaw's conversation API.
    On timeout or error, falls back to local Qwen quick response.
    """
    import aiohttp

    if not config.OPENCLAW_URL or not config.OPENCLAW_TOKEN:
        logger.warning("OpenClaw not configured, falling back to local response")
        return await get_quick_response(text, context)

    # Build payload for OpenClaw Gateway
    speaker_name = context.get("speaker_name", "Sconosciuto")
    source = context.get("source", "unknown")
    location = context.get("location", "")

    message_text = text
    if hint:
        message_text = f"[hint: {hint}] {text}"

    payload = {
        "message": message_text,
        "context": {
            "speaker": speaker_name,
            "source": source,
            "location": location,
            "speaker_id": context.get("speaker_id"),
            "room": context.get("room", ""),
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {config.OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }
            async with session.post(
                f"{config.OPENCLAW_URL}/api/v1/chat",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=config.OPENCLAW_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response = data.get("response", data.get("message", ""))
                    if response:
                        logger.info(f"OpenClaw response received ({len(response)} chars)")
                        return response
                    logger.warning("OpenClaw returned empty response")
                else:
                    body = await resp.text()
                    logger.error(f"OpenClaw error {resp.status}: {body[:200]}")

    except asyncio.TimeoutError:
        logger.warning(f"OpenClaw timeout ({config.OPENCLAW_TIMEOUT}s), falling back to local")
    except aiohttp.ClientConnectorError:
        logger.warning("OpenClaw unreachable, falling back to local")
        service_status.set_offline("openclaw")
    except Exception as e:
        logger.error(f"OpenClaw forward error: {e}")

    # Fallback: local Qwen quick response
    return await get_quick_response(text, context)


# ===========================================================================
# ENDPOINTS
# ===========================================================================

@app.post("/voice_command")
async def voice_command(request: Request):
    """Riceve audio raw dagli AtomS3R, processa e risponde."""
    data = await request.json()
    audio_bytes = None
    text = None

    if "audio" in data:
        audio_bytes = bytes.fromhex(data["audio"])
        clean_audio = await denoise_audio(audio_bytes)
        stt_start = time.time()
        text = await transcribe_audio(clean_audio)
        admin_metrics.record_stt((time.time() - stt_start) * 1000)
        if not text:
            return {"status": "no_speech_detected"}
    else:
        text = data.get("text")

    if not text:
        return {"status": "error", "message": "No text or audio provided"}

    # Costruisci contesto con speaker identification
    speaker_start = time.time()
    speaker_ctx = build_speaker_context(audio_bytes, "AtomS3R")
    if audio_bytes:
        admin_metrics.record_speaker_id((time.time() - speaker_start) * 1000)

    context = {
        "source": "AtomS3R",
        "room": data.get("room", "Unknown"),
        "mic_id": data.get("mic_id", "unknown"),
        **speaker_ctx
    }

    asyncio.create_task(process_jarvis_logic(text, context))
    return {"status": "processing", "speaker": speaker_ctx.get("speaker_name")}


@app.post("/camera_event")
async def camera_event(request: Request):
    """Riceve gli eventi di riconoscimento da Frigate/DoubleTake."""
    event_data = await request.json()
    await security.handle_camera_event(event_data)
    return {"status": "event_handled"}


@app.post("/telegram_callback")
async def telegram_callback(request: Request):
    """Gestisce i callback dai pulsanti Telegram (approvazioni e location selection)."""
    data = await request.json()
    callback_query = data.get("callback_query")

    if not callback_query:
        return {"status": "ok"}

    cb_data = callback_query.get("data", "")
    from_user = callback_query.get("from", {})
    telegram_id = from_user.get("id")
    tg_username = from_user.get("first_name", "")

    # AUTH: Verifica telegram_id
    user = get_user_by_telegram_id(telegram_id) if telegram_id else None
    if user:
        speaker_ctx = {
            "speaker_id": user.id,
            "speaker_name": user.name,
            "is_admin": user.is_admin,
            "telegram_id": telegram_id
        }
    else:
        # Fallback per backward compatibility
        speaker_ctx = build_speaker_context(None, "Telegram", tg_username)
        speaker_ctx["telegram_id"] = telegram_id

    if "_" not in cb_data or not cb_data.strip():
        return {"status": "invalid_callback"}

    parts = cb_data.split("_")
    if not parts or not parts[0]:
        return {"status": "invalid_callback"}
    action_type = parts[0]

    # Location selection callback: loc_wagmi_abc123
    if action_type == "loc" and len(parts) >= 3:
        location_id = parts[1]
        action_id = parts[2]

        # Recupera azione salvata
        saved_action = get_action(action_id)
        if saved_action and saved_action.get("type") == "location_select":
            delete_action(action_id)

            # Imposta location sticky (speaker_ctx già costruito sopra)
            if speaker_ctx.get("speaker_id"):
                set_user_location(speaker_ctx["speaker_id"], location_id, "telegram_sticky")

            # Recupera dati originali e ri-esegui
            original_text = saved_action.get("original_text", "")
            action_context = saved_action.get("action_context", {})

            loc = get_location(location_id)
            loc_name = loc.name if loc else location_id
            await send_telegram(f"📍 Impostato: {loc_name}")

            # Ri-processa il comando con la location specificata
            context = {
                "source": "Telegram",
                "chat_id": config.TELEGRAM_CHAT_ID,
                "location": location_id,
                **speaker_ctx
            }
            asyncio.create_task(process_jarvis_logic(original_text, context))
        else:
            await send_telegram(f"⚠️ Selezione scaduta, riprova.")

        return {"status": "ok"}

    # Approval callbacks: confirm_abc123, reject_abc123
    if action_type == "confirm" and len(parts) >= 2:
        action_id = parts[1]
        payload = get_action(action_id)
        if payload:
            delete_action(action_id)
            location = payload.get('location', get_default_location_id())
            success, err = await call_hass_service(
                location,
                payload['domain'],
                payload['action'],
                payload['data']
            )
            if success:
                await send_telegram(f"✅ Azione `{action_id}` eseguita con successo.")
                log_event("APPROVAL", f"Azione {action_id} approvata ed eseguita")
            else:
                await send_telegram(f"❌ Azione `{action_id}` fallita: {err}")
        else:
            await send_telegram(f"⚠️ Azione `{action_id}` scaduta o non trovata.")

    elif action_type == "reject" and len(parts) >= 2:
        action_id = parts[1]
        delete_action(action_id)
        await send_telegram(f"🚫 Azione `{action_id}` rifiutata.")
        log_event("APPROVAL", f"Azione {action_id} rifiutata")

    return {"status": "ok"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": time.time()}


@app.get("/health/services")
async def health_services():
    """Health check dettagliato di tutti i servizi esterni."""
    return {
        "orchestrator": "healthy",
        "services": service_status.to_dict(),
        "summary": {name: state.value for name, state in service_status.get_summary().items()},
        "critical_online": service_status.is_critical_online(),
        "timestamp": time.time()
    }


@app.get("/device_status")
async def get_device_status(device_id: str = None, room: str = None):
    """
    Endpoint per AtomS3R per sapere se JARVIS sta parlando.
    """
    # Pulisci stati vecchi (timeout 30 secondi)
    async with speaking_state_lock:
        now = time.time()
        expired = [r for r, s in speaking_state.items() if now - s["started_at"] > 30]
        for r in expired:
            del speaking_state[r]

    is_speaking = False
    target_room = ""

    if room:
        if room in speaking_state:
            is_speaking = True
            target_room = room
    else:
        if speaking_state:
            is_speaking = True
            rooms = list(speaking_state.keys())
            target_room = rooms[0] if rooms else None

    dnd_active = get_global_preference("dnd_mode", "False") == "True"

    return {
        "speaking": is_speaking,
        "target_room": target_room,
        "dnd_active": dnd_active,
        "timestamp": time.time()
    }


@app.post("/device_status")
async def update_device_status(request: Request):
    """
    Endpoint per AtomS3R per notificare il proprio stato DND.
    Riproduce feedback sonoro sullo speaker associato al device.

    Usa la configurazione dal database (voice_devices) per determinare
    lo speaker corretto. Fallback al mapping room_speakers se il device
    non è configurato.
    """
    data = await request.json()
    device_id = data.get("device_id", "unknown").upper().strip()
    room = data.get("room", "unknown").lower()
    dnd = data.get("dnd", False)

    device_dnd_status[device_id] = {
        "room": room,
        "dnd": dnd,
        "updated_at": time.time()
    }

    logger.info(f"📱 Device {device_id} ({room}) DND: {dnd}")

    # Recupera configurazione device dal database
    device_config = get_device_speaker_config(device_id)

    target_player = None
    location = get_default_location_id()

    if device_config:
        # Device configurato - usa lo speaker configurato
        target_player = device_config.get("output_speaker")
        location = device_config.get("location_id", get_default_location_id())
        logger.debug(f"Device {device_id} configured: speaker={target_player}, location={location}")
    else:
        # Fallback: usa mapping legacy room_speakers
        room_speakers = get_room_speakers(location)
        target_player = room_speakers.get(room)
        logger.debug(f"Device {device_id} not configured, using legacy mapping: {target_player}")

    if target_player:
        # DND attivato: suono neutrale (muto)
        # DND disattivato: suono positivo (riattivato)
        sound_type = "neutral" if dnd else "positive"
        asyncio.create_task(play_feedback_sound(sound_type, target_player, location))

    return {"status": "ok"}


@app.get("/room_temperature/{room}")
async def get_room_temperature(room: str):
    """
    Proxy endpoint per ottenere temperatura da HA.
    Cerca sensori con pattern: sensor.temperatura_{room}
    """
    import aiohttp

    entity_id = f"sensor.temperatura_{room}"

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {config.HASS_TOKEN}",
                "Content-Type": "application/json"
            }
            url = f"{config.HASS_URL}/api/states/{entity_id}"

            async with session.get(url, headers=headers, timeout=config.TIMEOUTS["ha_read"]) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    state = data.get("state", "unavailable")

                    if state not in ["unavailable", "unknown"]:
                        return {
                            "temperature": float(state),
                            "unit": data.get("attributes", {}).get("unit_of_measurement", "°C"),
                            "entity_id": entity_id
                        }

                return {"temperature": None, "error": "unavailable", "entity_id": entity_id}

    except Exception as e:
        logger.error(f"Error fetching temperature for {room}: {e}")
        return {"temperature": None, "error": str(e), "entity_id": entity_id}


@app.post("/voice_stream")
async def voice_stream(
    request: Request,
    room: str = Form(None),
    mic_id: str = Form(None),
    device_id: str = Form(None),
    audio: UploadFile = File(None)
):
    """
    Riceve audio in streaming dagli AtomS3R.
    Supporta sia multipart/form-data streaming che JSON legacy.

    Il device_id è il MAC address del dispositivo (formato AABBCCDDEEFF).
    La configurazione (room, location, speaker) viene letta dal database.
    """
    content_type = request.headers.get("content-type", "")

    audio_bytes = None
    room_value = room
    mic_id_value = mic_id
    device_id_value = device_id

    # Prova a leggere device_id dall'header X-Device-ID (nuovo metodo)
    header_device_id = request.headers.get("X-Device-ID")
    if header_device_id:
        device_id_value = header_device_id.upper().strip()

    # Handle multipart form data (streaming)
    if "multipart/form-data" in content_type:
        if audio:
            audio_bytes = await audio.read()
            logger.info(f"Received streaming audio: {len(audio_bytes)} bytes from device {device_id_value}")

    # Fallback to JSON (legacy)
    elif "application/json" in content_type:
        try:
            data = await request.json()
            if "audio" in data:
                audio_bytes = bytes.fromhex(data["audio"])
            room_value = data.get("room", room_value)
            mic_id_value = data.get("mic_id", mic_id_value)
            device_id_value = data.get("device_id", device_id_value) or device_id_value
        except Exception as e:
            logger.error(f"Failed to parse JSON: {e}")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Invalid JSON"}
            )

    if not audio_bytes:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "No audio data received"}
        )

    # Recupera configurazione device dal database
    device_config = None
    if device_id_value:
        device_config = get_device_speaker_config(device_id_value)

    if device_config:
        # Device configurato - usa la sua configurazione
        location = device_config.get("location_id", get_default_location_id())
        room_value = device_config.get("friendly_name", room_value or "Unknown")
        logger.info(f"Device {device_id_value} configured as '{room_value}' in location '{location}'")
    else:
        # Device non configurato o sconosciuto - usa fallback legacy
        location = extract_location_from_device(device_id_value or mic_id_value) or get_default_location_id()
        if not room_value:
            room_value = "Unknown"
        logger.warning(f"Device {device_id_value} not configured, using legacy mode: room={room_value}, location={location}")

    # Process audio
    try:
        clean_audio = await denoise_audio(audio_bytes)
        stt_start = time.time()
        text = await transcribe_audio(clean_audio)
        admin_metrics.record_stt((time.time() - stt_start) * 1000)

        if not text:
            return {"status": "no_speech_detected", "use_local_speaker": False}

        logger.info(f"Transcribed from stream: '{text[:50]}...'")

        # Speaker identification
        speaker_start = time.time()
        speaker_ctx = build_speaker_context(audio_bytes, "AtomS3R")
        admin_metrics.record_speaker_id((time.time() - speaker_start) * 1000)

        context = {
            "source": "AtomS3R",
            "room": room_value,
            "mic_id": mic_id_value or device_id_value or "unknown",
            "device_id": device_id_value or mic_id_value or "unknown",
            "location": location,
            "device_config": device_config,  # Passa la config per la fallback chain
            **speaker_ctx
        }

        # Aggiorna user location (voice)
        if speaker_ctx.get("speaker_id"):
            set_user_location(speaker_ctx["speaker_id"], location, "voice")

        # 3-way pre-routing via Qwen 7B (~100ms)
        pre_route_start = time.time()
        pre_result = await pre_route(text)
        pre_route_ms = (time.time() - pre_route_start) * 1000
        classification = pre_result.get("classification", "ALTRO")
        logger.info(f"Pre-route: {classification} (conf={pre_result.get('confidence', 0):.2f}, {pre_route_ms:.0f}ms)")

        if classification == "DOMOTICA_CERTA":
            # Fast path: local Qwen routing → HA direct (offline capable, <200ms)
            asyncio.create_task(process_jarvis_logic(text, context))
        elif classification == "DOMOTICA_INCERTA":
            # Ambiguous domotics: forward to OpenClaw with hint
            asyncio.create_task(_handle_openclaw_voice(text, context, hint="domotics"))
        else:
            # ALTRO: non-domotics, forward to OpenClaw
            asyncio.create_task(_handle_openclaw_voice(text, context, hint=""))

        return {
            "status": "processing",
            "speaker": speaker_ctx.get("speaker_name"),
            "location": location,
            "room": room_value,
            "transcribed_text": text[:100],
            "pre_route": classification,
            "use_local_speaker": False  # Default, verrà aggiornato dalla risposta finale
        }

    except Exception as e:
        logger.error(f"Error processing stream: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e), "use_local_speaker": True}
        )


# ===========================================================================
# OPENCLAW VOICE HANDLER (for DOMOTICA_INCERTA and ALTRO pre-routes)
# ===========================================================================

async def _handle_openclaw_voice(text: str, context: dict, hint: str = ""):
    """
    Handle voice commands that need OpenClaw (non-certain domotics or general queries).
    Forwards to OpenClaw and delivers response via TTS.
    """
    save_chat_message("user", text, context.get("source", "AtomS3R"),
                      context.get("speaker_id"), context.get("speaker_name", "Sconosciuto"))

    response = await forward_to_openclaw(text, context, hint=hint)

    save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
    await deliver_final_response(response, context, sound_type="neutral")


# ===========================================================================
# CORE LOGIC
# ===========================================================================

async def process_jarvis_logic(text: str, context: dict):
    """Main processing logic per tutti i comandi."""
    source = context.get("source", "unknown")
    speaker_id = context.get("speaker_id")
    speaker_name = context.get("speaker_name", "Sconosciuto")
    is_admin = context.get("is_admin", False)
    chat_id = context.get("chat_id", "default")
    location = context.get("location")  # Può essere None per Telegram
    source_id = f"{source}_{chat_id}_{speaker_id or 'anon'}"

    logger.info(f"Processing: '{text[:50]}...' from {source} (speaker: {speaker_name}, location: {location})")

    # 0. CHECK CRITICAL SERVICES
    if not service_status.is_critical_online():
        logger.error("Router model offline - cannot process request")
        await deliver_final_response(
            "Mi dispiace, il mio cervello è temporaneamente offline. Riprova tra qualche minuto.",
            context,
            sound_type="negative"
        )
        return

    # 1. CHECK CACHE
    cached_response = smart_cache.check(text)
    if cached_response:
        logger.info("Cache hit!")
        admin_metrics.record_cache_hit()
        # Cache hit con suono neutrale (es: "che ore sono?" dalla cache)
        await deliver_final_response(cached_response, context, sound_type="neutral")
        return

    admin_metrics.record_cache_miss()

    # 2. SAFETY CHECK
    is_safe_result, reason = await is_safe(text, source)
    if not is_safe_result:
        await send_telegram(f"⚠️ Comando bloccato: {reason}")
        log_event("SECURITY", f"Comando bloccato da {source}: {reason}", speaker_id, speaker_name)
        return

    # 3. RECUPERO MEMORIA PESATA
    # Recuperiamo abbastanza messaggi dal DB per coprire tutti i casi
    weighted_memory = get_weighted_context(
        current_speaker_id=speaker_id,
        seconds=config.MEMORY_WINDOW_SECONDS,
        max_messages=config.MAX_WEIGHTED_CONTEXT_MESSAGES  # Abbastanza per coprire 40+10+5 con margine
    )

    # Per il ROUTER (Qwen) usiamo limiti ridotti per velocità
    router_memory_prompt = format_weighted_context_for_llm(
        weighted_memory,
        speaker_name,
        high_limit=config.ROUTER_MEMORY_HIGH_PRIORITY,      # 15
        medium_limit=config.ROUTER_MEMORY_MEDIUM_PRIORITY,  # 5
        global_limit=config.ROUTER_MEMORY_GLOBAL            # 3
    )

    # 3b. CONTESTO STRATIFICATO (user + location memory)
    try:
        stratified_context = await build_full_context(
            user_id=speaker_id,
            user_name=speaker_name,
            query=text,
            target_location_id=location,
            context_type="routing"
        )
    except Exception as e:
        logger.warning(f"Stratified context build failed: {e}")
        stratified_context = ""

    # Combina memoria conversazionale + contesto stratificato per il router
    if stratified_context:
        router_memory_prompt = f"{router_memory_prompt}\n{stratified_context}"

    # 4. SALVA INPUT
    save_chat_message("user", text, source, speaker_id, speaker_name)

    # 5. ROUTING (con memoria ridotta per velocità)
    # Inietta status servizi nel contesto per routing intelligente
    service_status_prompt = service_status.get_status_for_prompt(location)

    # Recupera location disponibili per il prompt
    available_locations = [{"id": loc.id, "name": loc.name, "city": loc.city}
                          for loc in get_all_locations(enabled_only=True)]

    router_context = {
        **context,
        "memory": router_memory_prompt,
        "history_length": sum(len(v) for v in weighted_memory.values()),
        "service_status": service_status_prompt if service_status_prompt else "tutti i servizi online",
        "available_locations": available_locations,
        "location": location or "unknown"
    }
    routing_start = time.time()
    router_data = await get_routing(text, router_context)
    admin_metrics.record_routing((time.time() - routing_start) * 1000)

    intent = router_data.get("intent")
    conf = router_data.get("confidence", 0)
    interim_text = router_data.get("interim_response", "Hmm... ci penso...")

    # Recupera soglie confidenza da DB
    conf_high, conf_low = get_confidence_thresholds()

    logger.info(f"Routed: intent={intent}, confidence={conf:.2f}, speaker={speaker_name}")

    # Pubblica evento SSE per dashboard real-time
    try:
        from event_bus import event_bus
        await event_bus.publish("new_request", {
            "text": text[:80],
            "source": source,
            "speaker": speaker_name,
            "intent": intent,
            "confidence": round(conf, 2)
        })
    except Exception:
        pass

    # 6. DISPATCH PER INTENT

    # --- HOME CONTROL ---
    if intent == "HOME_CONTROL" and conf >= conf_high:
        payload = router_data.get("payload", {})
        domain = payload.get("domain", "light")
        action = payload.get("action", "toggle")
        entity = payload.get("entity", "unknown")

        # Risolvi location
        target_location = payload.get("location") or location

        # Per Telegram: risolvi location se mancante
        if source == "Telegram" and not target_location:
            resolved_location, needs_keyboard = await resolve_telegram_location(
                speaker_id, text, router_data
            )
            if needs_keyboard:
                await send_location_keyboard(
                    chat_id,
                    text,
                    {"intent": intent, "payload": payload}
                )
                return  # Aspetta callback
            target_location = resolved_location

        # Se nessuna location determinata, verifica se ci sono location disponibili
        if not target_location:
            available_locations = service_status.get_available_locations()
            if available_locations:
                target_location = available_locations[0]  # Usa prima location disponibile
            else:
                # Nessuna location configurata/disponibile
                response = "Non ho case Home Assistant configurate. Vai nella dashboard per aggiungere una location."
                save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
                await deliver_final_response(response, context, sound_type="negative")
                return

        # Check se Home Assistant per questa location è disponibile
        if not service_status.can_do_home_control(target_location):
            response = service_status.get_offline_message("HOME_CONTROL", target_location)
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="negative")
            return

        entity_id = f"{domain}.{entity.lower().replace(' ', '_')}"
        service_data = {"entity_id": entity_id}

        # L1-L4 security check
        source_channel = "voice" if source == "AtomS3R" else source.lower()
        allowed, sec_reason, domain_level, channel_max = check_security(
            domain, action, source_channel, entity_id=entity_id
        )

        if not allowed:
            if domain_level == SecurityLevel.L3_PROTECTED and channel_max < SecurityLevel.L3_PROTECTED:
                # L3 action from L2 channel → send to JARVIS approval bot
                action_id = str(uuid.uuid4())[:8]
                save_action(action_id, {
                    "domain": domain,
                    "action": action,
                    "data": service_data,
                    "location": target_location
                }, speaker_id)
                await send_telegram_approval(f"Richiesta: {router_data.get('response', action)}", action_id)
                log_event("APPROVAL", f"L3 azione {action_id} proposta: {sec_reason}", speaker_id, speaker_name)
            else:
                # L4 blocked or other denial
                response = f"Azione bloccata per sicurezza: {domain}.{action} non è consentito da {source}."
                log_event("SECURITY", f"BLOCKED: {sec_reason}", speaker_id, speaker_name)
                save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
                await deliver_final_response(response, context, sound_type="negative")
            return
        else:
            hass_start = time.time()
            success, err = await call_hass_service(target_location, domain, action, service_data)
            admin_metrics.record_hass((time.time() - hass_start) * 1000)

            if success:
                response = router_data.get("response", f"Fatto! {action} su {entity}.")
                smart_cache.learn(text, response, intent)
                log_event("HASS", f"[{target_location}] {action} su {entity}", speaker_id, speaker_name)
            else:
                response = f"Problema con {entity}: {err}"
                log_event("HARDWARE_ERROR", f"[{target_location}] Fallito {action} su {entity}: {err}", speaker_id, speaker_name)

            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")

            # Quick feedback per comandi vocali (stile Alexa: suono breve)
            # Legge impostazione dal database (default: True)
            quick_feedback_enabled = get_global_preference("ha_quick_feedback", "True") == "True"
            if source == "AtomS3R" and quick_feedback_enabled:
                room = context.get("room", "salotto").lower()
                room_speakers = get_room_speakers(target_location)
                # Fallback sicuro se room_speakers è vuoto
                if room_speakers:
                    target_player = room_speakers.get(room) or next(iter(room_speakers.values()), config.DEFAULT_FALLBACK_SPEAKER)
                else:
                    target_player = config.DEFAULT_FALLBACK_SPEAKER
                await quick_feedback(success, target_player, err, target_location)
            else:
                # Risposta vocale completa per Telegram o se quick feedback disabilitato
                # Suono positivo se OK, negativo se errore
                sound = "positive" if success else "negative"
                await deliver_final_response(response, context, sound_type=sound)

    # --- SET LOCATION ---
    elif intent == "SET_LOCATION":
        payload = router_data.get("payload", {})
        new_location = payload.get("location")

        if new_location == "reset":
            if speaker_id:
                clear_user_location(speaker_id)
            response = "Ho rimosso la tua posizione. La prossima volta ti chiederò dove sei."
            log_event("LOCATION", f"Reset posizione utente", speaker_id, speaker_name)
        else:
            loc = get_location(new_location)
            if loc and loc.enabled:
                if speaker_id:
                    set_user_location(speaker_id, new_location, "telegram_sticky")
                response = f"Perfetto! Ho impostato {loc.name} come tua posizione attuale."
                log_event("LOCATION", f"Impostata location {new_location}", speaker_id, speaker_name)
            else:
                response = f"Non conosco la location '{new_location}'."

        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        await deliver_final_response(response, context, sound_type="positive")

    # --- GEMINI (reasoning via Gemini API) ---
    elif intent == "GEMINI":
        if not config.GEMINI_ENABLED:
            # Gemini non configurato, forward a OpenClaw
            logger.warning("GEMINI intent but Gemini not enabled, forwarding to OpenClaw")
            response = await forward_to_openclaw(text, context, hint="gemini_fallback")
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")
            return

        await deliver_final_response(interim_text, context)

        # Estrai la domanda dal payload o dal testo
        payload = router_data.get("payload", {})
        question = payload.get("question", text)

        # Rimuovi i trigger "chiedi a gemini" dalla domanda
        for trigger in ["chiedi a gemini", "chiedi a google", "consulta gemini", "domanda per gemini"]:
            question = question.lower().replace(trigger, "").strip()

        # Per REASONING (Gemini) usiamo limiti leggermente più alti del router
        reasoning_memory_prompt = format_weighted_context_for_llm(
            weighted_memory,
            speaker_name,
            high_limit=config.ROUTER_MEMORY_HIGH_PRIORITY + 10,    # 25
            medium_limit=config.ROUTER_MEMORY_MEDIUM_PRIORITY + 3, # 8
            global_limit=config.ROUTER_MEMORY_GLOBAL + 2           # 5
        )

        # Contesto stratificato per Gemini
        try:
            gemini_stratified = await build_full_context(
                user_id=speaker_id,
                user_name=speaker_name,
                query=text,
                target_location_id=location,
                context_type="reasoning"
            )
        except Exception as e:
            logger.warning(f"Gemini stratified context failed: {e}")
            gemini_stratified = ""

        # Costruisci history per Gemini
        gemini_system = reasoning_memory_prompt
        if gemini_stratified:
            gemini_system = f"{gemini_system}\n{gemini_stratified}"

        history = [{"role": "system", "content": gemini_system}]
        for msg in weighted_memory.get("high_priority", [])[-20:]:
            history.append({"role": msg["role"], "content": msg["content"]})

        response = await get_gemini_response(question, history)

        log_event("GEMINI", f"Domanda: {question[:50]}...", speaker_id, speaker_name)
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        await deliver_final_response(response, context, sound_type="neutral")
        return

    # --- VERIFY_WITH_GEMINI (confronto risposta precedente) ---
    elif intent == "VERIFY_WITH_GEMINI":
        if not config.GEMINI_ENABLED:
            response = "Mi dispiace, Gemini non è configurato per la verifica."
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="negative")
            return

        # Recupera l'ultima domanda e risposta dalla memoria
        last_exchange = _get_last_qa_from_memory(weighted_memory)

        if not last_exchange:
            response = "Non ricordo cosa ti ho detto. Puoi ripetere la domanda?"
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")
            return

        await deliver_final_response("Chiedo conferma a Gemini...", context)

        response = await verify_with_gemini(
            original_question=last_exchange["question"],
            previous_response=last_exchange["answer"]
        )

        log_event("VERIFY_GEMINI", f"Verifica risposta precedente", speaker_id, speaker_name)
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        await deliver_final_response(response, context, sound_type="neutral")
        return

    # --- IMAGE_GENERATION (genera immagini con Gemini) ---
    elif intent == "IMAGE_GENERATION":
        if not config.GEMINI_ENABLED:
            response = "Mi dispiace, la generazione immagini richiede Gemini che non è configurato."
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="negative")
            return

        await deliver_final_response("Sto generando l'immagine...", context)

        # Import image generation module
        from image_generation import generate_and_show, parse_image_request, extract_tv_from_room

        # Parsa la richiesta
        payload = router_data.get("payload", {})
        parsed = parse_image_request(text)

        prompt = payload.get("prompt") or parsed.get("prompt") or text
        room = payload.get("room") or parsed.get("room")
        send_tg = payload.get("send_telegram", False) or parsed.get("send_telegram", False)

        # Risolvi location
        target_location = payload.get("location") or location or get_default_location_id()

        # Trova TV dalla stanza (se specificata)
        tv_entity = payload.get("tv_entity")
        if not tv_entity and room:
            tv_entity = extract_tv_from_room(room, target_location)

        # Se nessuna TV specificata ma non deve mandare su Telegram, prova TV default
        if not tv_entity and not send_tg:
            # Default: prova TV soggiorno
            tv_entity = extract_tv_from_room("soggiorno", target_location)
            if not tv_entity:
                # Fallback: manda su Telegram
                send_tg = True

        # Telegram chat_id
        telegram_chat_id = context.get("telegram_id") or context.get("chat_id")

        # Genera e mostra
        success, message, image_url = await generate_and_show(
            prompt=prompt,
            tv_entity=tv_entity,
            location_id=target_location,
            send_telegram=send_tg,
            telegram_chat_id=telegram_chat_id
        )

        if success:
            response = message
            log_event("IMAGE_GEN", f"Generata immagine: {prompt[:50]}...", speaker_id, speaker_name)
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="positive")
        else:
            response = f"Non sono riuscito a generare l'immagine: {message}"
            log_event("IMAGE_GEN_ERROR", f"Errore: {message}", speaker_id, speaker_name)
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="negative")
        return

    # --- SET PREFERENCE ---
    elif intent == "SET_PREFERENCE":
        payload = router_data.get("payload", {})
        raw_key = payload.get("key", "")
        raw_val = payload.get("value", "")

        pref_key, pref_val = normalize_preference(raw_key, raw_val)

        if pref_key in ["dnd_mode", "silent_hour_start", "silent_hour_end"]:
            # Preferenze globali
            set_global_preference(pref_key, pref_val)
            log_event("CONFIG", f"{pref_key} → {pref_val}", speaker_id, speaker_name)
            response = f"Impostato {pref_key.replace('_', ' ')} a {pref_val}."
        else:
            response = "Non ho capito quale preferenza vuoi cambiare."

        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        # Conferma preferenza con suono positivo
        await deliver_final_response(response, context, sound_type="positive")

    # --- AUDIT REPORT ---
    elif intent == "AUDIT_REPORT":
        # Admin vede tutto, altri vedono solo i propri
        logs = get_audit_summary(
            limit=config.MAX_AUDIT_LOGS,
            speaker_id=speaker_id,
            is_admin=is_admin
        )

        if not logs:
            response = "Non ci sono eventi recenti nel registro."
        else:
            logs_str = "\n".join([
                f"[{time.ctime(l['timestamp'])}] {l['category']}: {l['message']}"
                for l in logs[:config.MAX_AUDIT_LOGS_IN_REPORT]
            ])

            report_prompt = f"Eventi casa:\n{logs_str}\n\nUtente ({speaker_name}) chiede: {text}"
            # Usa Gemini se disponibile, altrimenti quick response
            if config.GEMINI_ENABLED:
                response = await get_gemini_response(report_prompt, [])
            else:
                response = await get_quick_response(report_prompt, context)

        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        # Report audit con suono neutrale
        await deliver_final_response(response, context, sound_type="neutral")

    # --- RETRY ---
    elif intent == "RETRY":
        response = router_data.get("response", "Puoi ripetere specificando meglio?")
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        # Richiesta chiarimento con suono neutrale
        await deliver_final_response(response, context, sound_type="neutral")

    # --- LOW CONFIDENCE: forward to OpenClaw ---
    elif conf < conf_low:
        logger.info(f"Low confidence ({conf:.2f} < {conf_low}), forwarding to OpenClaw")
        response = await forward_to_openclaw(text, context, hint=f"low_confidence_{intent}")
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        await deliver_final_response(response, context, sound_type="neutral")

    # --- SIMPLE CHAT / UNKNOWN ---
    else:
        response = router_data.get("response")
        if not response:
            response = await get_quick_response(text, context)

        smart_cache.learn(text, response, intent)
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        # Risposta conversazionale con suono neutrale
        await deliver_final_response(response, context, sound_type="neutral")


async def deliver_final_response(text: str, context: dict, sound_type: str = None):
    """
    Helper per mandare risposta sul canale corretto con suono contestuale.

    Implementa la fallback chain per speaker:
    1. Speaker principale configurato (output_speaker)
    2. Speaker fallback configurato (fallback_speaker)
    3. Telegram utente (se fallback_telegram=True e utente ha Telegram)
    4. Speaker locale AtomS3R (se fallback_local_speaker=True)

    Args:
        text: Testo della risposta
        context: Contesto della richiesta (source, room, location, device_config, etc.)
        sound_type: Tipo di suono introduttivo ("positive", "neutral", "negative", None)
    """
    source = context.get("source", "unknown")
    room = context.get("room", "salotto").lower()
    location = context.get("location", get_default_location_id())
    speaker_name = context.get("speaker_name", "")
    speaker_id = context.get("speaker_id")
    device_config = context.get("device_config")  # Configurazione dal database

    dnd_mode = get_global_preference("dnd_mode", "False") == "True"
    s_start = int(get_global_preference("silent_hour_start", str(config.SILENT_START)))
    s_end = int(get_global_preference("silent_hour_end", str(config.SILENT_END)))
    now_h = datetime.now().hour

    if s_start > s_end:
        is_silent_time = (now_h >= s_start or now_h < s_end)
    else:
        is_silent_time = (s_start <= now_h < s_end)

    if dnd_mode:
        await send_telegram(f"🔕 {text}")
        return

    if is_silent_time and source == "OpenClaw":
        await send_telegram(f"🌙 {text}")
        return

    if source == "Telegram":
        await send_telegram(text)
        return

    # === FALLBACK CHAIN per AtomS3R ===

    # Determina target speaker usando la fallback chain
    target_player = None
    use_local_speaker = False

    if device_config:
        # Device configurato - usa la fallback chain configurata
        output_speaker = device_config.get("output_speaker")
        fallback_speaker = device_config.get("fallback_speaker")
        fallback_telegram = device_config.get("fallback_telegram", True)
        fallback_local = device_config.get("fallback_local_speaker", True)

        # 1. Prova speaker principale
        if output_speaker:
            success = await try_speak(text, output_speaker, location, sound_type)
            if success:
                target_player = output_speaker
                logger.info(f"Audio delivered to primary speaker: {output_speaker}")

        # 2. Fallback speaker
        if not target_player and fallback_speaker:
            success = await try_speak(text, fallback_speaker, location, sound_type)
            if success:
                target_player = fallback_speaker
                logger.info(f"Audio delivered to fallback speaker: {fallback_speaker}")

        # 3. Telegram utente (se abilitato e utente ha telegram)
        if not target_player and fallback_telegram and speaker_id:
            user = get_user_by_id(speaker_id)
            if user and user.telegram_id:
                await send_telegram(f"🔊 {text}", chat_id=user.telegram_id)
                target_player = f"telegram:{user.telegram_id}"
                logger.info(f"Audio delivered to Telegram user: {user.name}")

        # 4. Speaker locale AtomS3R (ultimo fallback)
        if not target_player and fallback_local:
            use_local_speaker = True
            logger.info("All speakers failed, falling back to local AtomS3R speaker")

    else:
        # Legacy mode - usa room_speakers mapping
        room_speakers = get_room_speakers(location)
        if room_speakers:
            target_player = room_speakers.get(room) or next(iter(room_speakers.values()), config.DEFAULT_FALLBACK_SPEAKER)
        else:
            target_player = config.DEFAULT_FALLBACK_SPEAKER

        # Setta stato SPEAKING prima di parlare (per notificare AtomS3R)
        device_id = context.get("device_id", "unknown")
        await set_speaking_state(room, True, device_id)

        # Usa suono contestuale se specificato
        if sound_type:
            await speak_with_sound(text, target_player, sound_type, location)
        else:
            await speak(text, target_player, location)

    # Se abbiamo usato un target_player (non local), gestisci lo speaking state
    if target_player and not target_player.startswith("telegram:") and not use_local_speaker:
        device_id = context.get("device_id", "unknown")
        await set_speaking_state(room, True, device_id)

        # Stima durata TTS: ~N caratteri/secondo + offset secondi per il suono
        estimated_duration = max(config.TTS_MIN_DURATION, len(text) / config.TTS_CHARS_PER_SECOND + config.TTS_SOUND_OFFSET)

        # Schedula pulizia stato speaking dopo la durata stimata
        asyncio.create_task(clear_speaking_state_after_delay(room, estimated_duration))

    # TODO: Se use_local_speaker=True, il chiamante dovrebbe gestire
    # la risposta locale. Per ora logghiamo solo.
    if use_local_speaker:
        logger.warning(f"Local speaker fallback triggered for device {context.get('device_id')}")


async def try_speak(text: str, target_player: str, location: str, sound_type: str = None) -> bool:
    """
    Prova a inviare audio a uno speaker specifico.
    Ritorna True se successo, False se fallisce.
    """
    try:
        if sound_type:
            await speak_with_sound(text, target_player, sound_type, location)
        else:
            await speak(text, target_player, location)
        return True
    except Exception as e:
        logger.warning(f"Failed to speak to {target_player}: {e}")
        return False


def normalize_preference(key: str, value: str) -> tuple[str, str]:
    """Normalizza chiavi e valori preferenze."""
    key = key.lower().strip().replace(" ", "_")
    key = CANONICAL_KEYS.get(key, key)

    if "hour" in key:
        match = re.search(r'\d+', str(value))
        if match:
            return key, match.group()

    if key == "dnd_mode":
        if str(value).lower() in ["true", "on", "attiva", "attivo", "sì", "si", "1"]:
            return key, "True"
        return key, "False"

    return key, str(value)


def _get_last_qa_from_memory(weighted_memory: dict) -> Optional[dict]:
    """
    Estrae l'ultima coppia domanda-risposta dalla memoria.
    Usato per VERIFY_WITH_GEMINI.
    """
    high_priority = weighted_memory.get("high_priority", [])

    last_user_msg = None
    last_assistant_msg = None

    # Cerca dall'ultimo messaggio indietro
    for msg in reversed(high_priority):
        if msg["role"] == "assistant" and not last_assistant_msg:
            last_assistant_msg = msg["content"]
        elif msg["role"] == "user" and not last_user_msg:
            last_user_msg = msg["content"]

        if last_user_msg and last_assistant_msg:
            break

    if last_user_msg and last_assistant_msg:
        return {
            "question": last_user_msg,
            "answer": last_assistant_msg
        }
    return None


# ===========================================================================
# MAIN
# ===========================================================================

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=False, log_level="info")
