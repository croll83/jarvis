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
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from fastapi import FastAPI, Request, UploadFile, File, Form, Query, WebSocket
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
    call_hass_service, call_hass_service_bulk, speak, send_telegram, edit_telegram,
    send_telegram_approval, send_exec_approval, denoise_audio, transcribe_audio,
    quick_feedback, speak_with_sound, play_feedback_sound
)
from ai_engines import (
    is_safe, get_routing, get_quick_response, pre_route,
    normalize_stt_text,
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
from speaker_suppress import suppress_speaker, restore_speaker, get_suppressed_speakers
from image_api import router as image_router
from service_status import service_status, ServiceState
from multi_ha import multi_ha
from memory_jobs import memory_scheduler
from location_memory import load_memory_services_from_db
from context_builder import build_full_context, build_routing_context, build_reasoning_context
from proactive import proactive_check_loop
from vector_store import init_vector_store
from ws_audio_handler import (
    init_vad, get_active_session_count, get_persistent_connection_count,
    trigger_device_listen, get_connected_devices,
)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("JARVIS_MAIN")

# Filtro per nascondere le richieste frequenti dall'access log di uvicorn
# (device_status polling ogni 2s, heartbeat ogni 5min — inquinano il log)
class _QuietDevicePollingFilter(logging.Filter):
    _QUIET_PATHS = ("/device_status", "/heartbeat", "/room_temperature/", "/ws/audio")
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._QUIET_PATHS)

logging.getLogger("uvicorn.access").addFilter(_QuietDevicePollingFilter())

# Tracking stato "speaking" per room (per notificare AtomS3R)
# Struttura: {"salotto": {"speaking": True, "started_at": timestamp, "device_id": "..."}}
speaking_state: dict = {}
speaking_state_lock = asyncio.Lock()

# Pending exec approvals from OpenClaw (approval_id -> full request data)
pending_exec_approvals: dict = {}

# Strong references to background tasks (prevents GC from destroying them)
_background_tasks: set = set()

# Virtual Microphone response store (request_id -> response data, auto-expires)
_vmic_responses: dict = {}


# ===========================================================================
# OPENCLAW GATEWAY OPERATOR (WebSocket client for exec approvals)
# ===========================================================================
# OpenClaw gateway broadcasts exec.approval.requested events to connected
# operator clients. We connect as an operator, receive these events, forward
# them to Telegram with inline buttons, and resolve via exec.approval.resolve.
#
# Gateway WS protocol (JSON-RPC style):
#   Request:  {"type":"req", "id":"<uuid>", "method":"<method>", "params":{...}}
#   Response: {"type":"res", "id":"<uuid>", "ok":true, "payload":{...}}
#   Event:    {"type":"event", "event":"<name>", "payload":{...}}

# Reference WS connection that stays open for resolving approvals
_operator_ws = None

async def openclaw_operator_loop():
    """
    Connects to OpenClaw gateway as a WebSocket operator client.
    Listens for exec.approval.requested events and forwards them
    to Telegram with inline buttons via the JARVIS Approval Bot.
    """
    global _operator_ws
    import websockets
    import json as _json

    ws_url = config.OPENCLAW_WS_URL
    token = config.OPENCLAW_TOKEN

    if not ws_url or not token:
        logger.warning("OPENCLAW_WS_URL or OPENCLAW_TOKEN not set, exec approval operator disabled")
        return

    reconnect_delay = 5

    while True:
        try:
            logger.info(f"Connecting to OpenClaw gateway WS: {ws_url}")
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                # ── Step 1: Wait for connect.challenge from gateway ──
                challenge_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                challenge = _json.loads(challenge_raw)
                challenge_nonce = ""
                challenge_ts = 0

                if challenge.get("type") == "event" and challenge.get("event") == "connect.challenge":
                    challenge_nonce = challenge.get("payload", {}).get("nonce", "")
                    challenge_ts = challenge.get("payload", {}).get("ts", 0)
                    logger.info(f"Received connect.challenge nonce={challenge_nonce[:12]}...")
                else:
                    logger.warning(f"Expected connect.challenge, got: {challenge}")

                # ── Step 2: Send connect request with full protocol v3 params ──
                # client.id MUST be one of the gateway whitelist enum values
                # device object is OPTIONAL — we rely on auth.token instead
                connect_id = str(uuid.uuid4())
                connect_msg = _json.dumps({
                    "type": "req",
                    "id": connect_id,
                    "method": "connect",
                    "params": {
                        "minProtocol": 3,
                        "maxProtocol": 3,
                        "client": {
                            "id": "gateway-client",
                            "version": "1.0.0",
                            "platform": "linux",
                            "mode": "backend"
                        },
                        "role": "operator",
                        "scopes": [
                            "operator.read",
                            "operator.write",
                            "operator.approvals"
                        ],
                        "auth": {
                            "token": token
                        },
                        "locale": "it-IT",
                        "userAgent": "jarvis-orchestrator/1.0.0"
                    }
                })
                await ws.send(connect_msg)
                logger.info("Sent connect request (protocol v3)")

                # ── Step 3: Wait for HelloOk response ──
                hello_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                hello = _json.loads(hello_raw)
                if hello.get("type") == "res" and hello.get("ok"):
                    proto = hello.get("payload", {}).get("protocol", "?")
                    logger.info(f"✅ OpenClaw operator WS connected (proto={proto})")
                else:
                    err = hello.get("error", hello)
                    logger.error(f"OpenClaw WS handshake failed: {err}")
                    await asyncio.sleep(reconnect_delay)
                    continue

                _operator_ws = ws
                reconnect_delay = 5

                # Listen for events
                async for raw_msg in ws:
                    try:
                        msg = _json.loads(raw_msg)
                        msg_type = msg.get("type")

                        if msg_type == "event":
                            event_name = msg.get("event", "")
                            payload = msg.get("payload", {})

                            if event_name in ("exec.approval.requested", "exec.approval.request"):
                                await _handle_exec_approval_event(payload)
                            elif event_name == "exec.approval.resolved":
                                # Cleanup pending map
                                req_id = payload.get("id", "")
                                if req_id and req_id in pending_exec_approvals:
                                    del pending_exec_approvals[req_id]
                                    logger.info(f"Exec approval {req_id[:8]} resolved externally")
                            elif event_name == "tick":
                                pass  # Gateway heartbeat — ignore silently
                            else:
                                logger.debug(f"WS event: {event_name}")

                        elif msg_type == "req":
                            # Server-initiated request — gateway sends exec approvals as req!
                            method = msg.get("method", "")
                            req_id = msg.get("id", "")
                            params = msg.get("params", {})

                            if method in ("exec.approval.request", "exec.approval.requested"):
                                # Gateway sends approval as a req and expects a res with the decision.
                                # We store the WS req_id so we can respond later when the user presses a button.
                                logger.info(f"Exec approval req received: ws_req_id={req_id}, params keys={list(params.keys())}")
                                # Merge req_id into params so we can respond later
                                params["_ws_req_id"] = req_id
                                await _handle_exec_approval_event(params)
                            else:
                                # Other server requests (e.g. ping/tick) — respond ok
                                logger.debug(f"WS req: method={method} id={req_id}")
                                if req_id:
                                    await ws.send(_json.dumps({
                                        "type": "res",
                                        "id": req_id,
                                        "ok": True,
                                        "payload": {}
                                    }))

                        elif msg_type == "res":
                            # Response to a request we sent (e.g. resolve)
                            logger.debug(f"WS response: {msg}")

                    except Exception as e:
                        logger.error(f"Error processing OpenClaw WS message: {e}")

        except asyncio.CancelledError:
            logger.info("OpenClaw operator loop cancelled")
            return
        except Exception as e:
            logger.warning(f"OpenClaw operator WS disconnected: {e}, reconnecting in {reconnect_delay}s")
            _operator_ws = None
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


async def _handle_exec_approval_event(payload: dict):
    """Handle an exec.approval.requested event from OpenClaw gateway."""
    # Payload structure: {id, request: {command, cwd, agentId, ...}, createdAtMs, expiresAtMs}
    logger.debug(f"Exec approval payload keys: {list(payload.keys())}")

    approval_id = payload.get("id", "")
    ws_req_id = payload.pop("_ws_req_id", None)

    # The command details are nested inside the "request" object
    request = payload.get("request", {})
    command = request.get("command", payload.get("command", "unknown"))
    cwd = request.get("cwd", payload.get("cwd", ""))
    agent = request.get("agentId", request.get("agent", payload.get("agentId", "")))

    if not approval_id:
        logger.error(f"Exec approval event missing id, payload: {payload}")
        return

    # Store in pending map for callback resolution
    pending_exec_approvals[approval_id] = {
        "data": payload,
        "ws_req_id": ws_req_id,
        "timestamp": time.time()
    }
    logger.info(f"Exec approval requested: {approval_id[:8]} cmd={command[:80]}")

    # Send Telegram message with inline buttons
    await send_exec_approval(
        approval_id=approval_id,
        command=command,
        cwd=cwd,
        agent=agent
    )


async def resolve_exec_approval(approval_id: str, decision: str):
    """
    Resolve an exec approval via the OpenClaw gateway WebSocket.
    decision: "allow-once" | "allow-always" | "deny"

    The gateway may send approvals either as:
      1. A "req" frame (expects a "res" response with the decision)
      2. An "event" frame (resolve via a new "exec.approval.resolve" req)

    We try method 1 first (respond to original req), then fall back to method 2.
    """
    import json as _json

    ws = _operator_ws
    if not ws or ws.closed:
        logger.error(f"Cannot resolve exec approval {approval_id[:8]}: WS not connected")
        return False

    pending = pending_exec_approvals.get(approval_id, {})
    ws_req_id = pending.get("ws_req_id")

    try:
        if ws_req_id:
            # Method 1: Respond to the gateway's original req frame
            resolve_msg = _json.dumps({
                "type": "res",
                "id": ws_req_id,
                "ok": True,
                "payload": {
                    "id": approval_id,
                    "decision": decision
                }
            })
            await ws.send(resolve_msg)
            logger.info(f"Exec approval {approval_id[:8]} resolved via res to req {ws_req_id[:8]}: {decision}")
        else:
            # Method 2: Send a new req to resolve
            req_id = str(uuid.uuid4())
            resolve_msg = _json.dumps({
                "type": "req",
                "id": req_id,
                "method": "exec.approval.resolve",
                "params": {
                    "id": approval_id,
                    "decision": decision
                }
            })
            await ws.send(resolve_msg)
            logger.info(f"Exec approval {approval_id[:8]} resolved via req: {decision}")

        # Cleanup pending
        if approval_id in pending_exec_approvals:
            del pending_exec_approvals[approval_id]

        return True
    except Exception as e:
        logger.error(f"Error resolving exec approval {approval_id[:8]}: {e}")
        return False

# ===========================================================================
# APPROVAL BOT: WEBHOOK (preferred) or POLLING (fallback)
# ===========================================================================
# If TELEGRAM_WEBHOOK_URL is set, the bot registers a webhook on startup and
# all updates are received via POST /telegram_webhook.
# If not set, falls back to getUpdates long-polling (legacy mode).

async def _tg_bot_api(method: str, payload: dict = None) -> dict:
    """Helper: call Telegram Bot API method."""
    import aiohttp
    bot_token = config.JARVIS_APPROVAL_BOT_TOKEN
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload or {},
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json()
    except Exception as e:
        logger.warning(f"Telegram Bot API {method} error: {e}")
        return {"ok": False, "error": str(e)}


async def _handle_approval_update(update: dict):
    """Process a single Telegram update (message or callback_query).

    This is the unified handler used by both webhook and polling modes.
    """
    bot_token = config.JARVIS_APPROVAL_BOT_TOKEN

    # ── Handle text messages (/location) ──
    message = update.get("message")
    if message:
        text_msg = (message.get("text") or "").strip()
        msg_chat_id = message.get("chat", {}).get("id")
        from_user_msg = message.get("from", {})
        tg_id_msg = from_user_msg.get("id")
        logger.info(f"Approval bot message: '{text_msg}' from tg_id={tg_id_msg}")

        cmd = text_msg.lower().split("@")[0]

        # /location — show inline keyboard for location selection
        if cmd == "/location" and msg_chat_id:
            user_msg = get_user_by_telegram_id(tg_id_msg) if tg_id_msg else None
            if not user_msg:
                logger.warning(f"Approval bot /location: tg_id {tg_id_msg} not linked to any user")
                return

            locations = get_all_locations(enabled_only=True)
            user_loc = get_user_location(user_msg.id)
            current_loc = user_loc.location_id if user_loc else None

            buttons = []
            for loc in locations:
                prefix = "✅ " if loc.id == current_loc else "🏠 "
                buttons.append({"text": f"{prefix}{loc.name}", "callback_data": f"setloc_{loc.id}"})

            await _tg_bot_api("sendMessage", {
                "chat_id": msg_chat_id,
                "text": f"📍 Location attuale: *{current_loc or 'non impostata'}*\nSeleziona la tua posizione:",
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": [buttons]}
            })


        return

    # ── Handle callback queries (inline button presses) ──
    callback_query = update.get("callback_query")
    if not callback_query:
        return

    cb_data = callback_query.get("data", "")
    cb_id = callback_query.get("id", "")
    logger.info(f"Approval bot callback: {cb_data}")

    # Answer the callback to remove the loading spinner
    await _tg_bot_api("answerCallbackQuery", {"callback_query_id": cb_id})

    # ── setloc_ callback: set user location ──
    if cb_data.startswith("setloc_"):
        loc_id = cb_data[7:]
        from_user_cb = callback_query.get("from", {})
        tg_id_cb = from_user_cb.get("id")
        user_cb = get_user_by_telegram_id(tg_id_cb) if tg_id_cb else None

        if user_cb and loc_id:
            set_user_location(user_cb.id, loc_id, "telegram_command")
            loc = get_location(loc_id)
            loc_name = loc.name if loc else loc_id

            msg = callback_query.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            message_id = msg.get("message_id")
            if chat_id and message_id:
                await _tg_bot_api("editMessageText", {
                    "chat_id": chat_id, "message_id": message_id,
                    "text": f"📍 Location impostata: *{loc_name}*",
                    "parse_mode": "Markdown"
                })

            logger.info(f"User {user_cb.name} location set to {loc_id} via /location command")
        return

    # ── Exec approval callbacks: execonce_slug, execalways_slug, execdeny_slug ──
    # ── Action approval callbacks: confirm_id, reject_id ──
    # ── Location selection callbacks: loc_locid_actionid ──
    if "_" not in cb_data:
        return

    parts = cb_data.split("_", 1)
    action_type = parts[0]
    slug = parts[1] if len(parts) > 1 else ""

    # Exec approvals (OpenClaw)
    exec_decision_map = {
        "execonce": "allow-once",
        "execalways": "allow-always",
        "execdeny": "deny"
    }

    if action_type in exec_decision_map and slug:
        full_id = _find_exec_approval_by_slug(slug)
        decision = exec_decision_map[action_type]

        if full_id:
            success = await resolve_exec_approval(full_id, decision)
            label = {"execonce": "✅ Once", "execalways": "🔓 Always", "execdeny": "❌ Deny"}[action_type]
            msg = callback_query.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            message_id = msg.get("message_id")
            if chat_id and message_id:
                original_text = msg.get("text", "")
                status = f"\n\n→ {label}" if success else "\n\n→ ⚠️ Error"
                await _tg_bot_api("editMessageText", {
                    "chat_id": chat_id, "message_id": message_id,
                    "text": original_text + status
                })

            logger.info(f"Exec approval {slug} resolved: {decision} (success={success})")
        else:
            logger.warning(f"Exec approval {slug} not found in pending (expired?)")
        return

    # L2/L3/L4 action approvals (confirm/reject)
    if action_type == "confirm" and slug:
        action_id = slug
        payload = get_action(action_id)
        if payload:
            delete_action(action_id)
            location = payload.get('location', get_default_location_id())
            success, err = await call_hass_service(
                location, payload['domain'], payload['action'], payload['data']
            )
            msg = callback_query.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            message_id = msg.get("message_id")
            if success:
                if chat_id and message_id:
                    original_text = msg.get("text", "")
                    await _tg_bot_api("editMessageText", {
                        "chat_id": chat_id, "message_id": message_id,
                        "text": original_text + "\n\n→ ✅ Eseguita"
                    })
                log_event("APPROVAL", f"Azione {action_id} approvata ed eseguita")
            else:
                if chat_id and message_id:
                    original_text = msg.get("text", "")
                    await _tg_bot_api("editMessageText", {
                        "chat_id": chat_id, "message_id": message_id,
                        "text": original_text + f"\n\n→ ❌ Fallita: {err}"
                    })
        else:
            await send_telegram(f"⚠️ Azione `{action_id}` scaduta o non trovata.")
        return

    if action_type == "reject" and slug:
        action_id = slug
        delete_action(action_id)
        msg = callback_query.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        if chat_id and message_id:
            original_text = msg.get("text", "")
            await _tg_bot_api("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": original_text + "\n\n→ 🚫 Rifiutata"
            })
        log_event("APPROVAL", f"Azione {action_id} rifiutata")
        return

    # Location selection callback from old /telegram_callback: loc_wagmi_abc123
    if action_type == "loc":
        sub_parts = cb_data.split("_")
        if len(sub_parts) >= 3:
            location_id = sub_parts[1]
            action_id_loc = sub_parts[2]
            from_user_cb = callback_query.get("from", {})
            tg_id_cb = from_user_cb.get("id")
            user_cb = get_user_by_telegram_id(tg_id_cb) if tg_id_cb else None

            saved_action = get_action(action_id_loc)
            if saved_action and saved_action.get("type") == "location_select":
                delete_action(action_id_loc)
                if user_cb and user_cb.id:
                    set_user_location(user_cb.id, location_id, "telegram_sticky")

                original_text = saved_action.get("original_text", "")
                action_context = saved_action.get("action_context", {})

                loc = get_location(location_id)
                loc_name = loc.name if loc else location_id
                await send_telegram(f"📍 Impostato: {loc_name}")

                context = {
                    "source": "Telegram",
                    "chat_id": config.JARVIS_APPROVAL_CHAT_ID,
                    "location": location_id,
                    **({"speaker_id": user_cb.id, "speaker_name": user_cb.name,
                        "is_admin": user_cb.is_admin, "telegram_id": tg_id_cb} if user_cb else
                       build_speaker_context(None, "Telegram", ""))
                }
                asyncio.create_task(process_jarvis_logic(original_text, context))
            else:
                await send_telegram(f"⚠️ Selezione scaduta, riprova.")
        return

    logger.debug(f"Unhandled approval callback: {cb_data}")


async def approval_bot_setup():
    """Setup approval bot: register webhook or start polling loop."""
    bot_token = config.JARVIS_APPROVAL_BOT_TOKEN
    if not bot_token:
        logger.warning("JARVIS_APPROVAL_BOT_TOKEN not set, approval bot disabled")
        return

    webhook_url = config.TELEGRAM_WEBHOOK_URL
    if webhook_url:
        # ── WEBHOOK MODE ──
        # Register the webhook with Telegram so updates are POSTed to us.
        result = await _tg_bot_api("setWebhook", {
            "url": webhook_url,
            "allowed_updates": ["callback_query", "message"],
            "drop_pending_updates": False
        })
        if result.get("ok"):
            logger.info(f"✅ Telegram webhook registered: {webhook_url}")
        else:
            logger.error(f"❌ Failed to register Telegram webhook: {result}")
            # Fall back to polling
            logger.info("Falling back to getUpdates polling...")
            await _approval_bot_polling_fallback()
    else:
        # ── POLLING MODE (legacy) ──
        logger.info("TELEGRAM_WEBHOOK_URL not set, using getUpdates polling")
        await _approval_bot_polling_fallback()


async def _approval_bot_polling_fallback():
    """Legacy polling mode using getUpdates."""
    import json as _json

    bot_token = config.JARVIS_APPROVAL_BOT_TOKEN
    if not bot_token:
        return

    # Delete any existing webhook so getUpdates works
    await _tg_bot_api("deleteWebhook")

    offset = 0
    poll_timeout = 30

    while True:
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            params = {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": _json.dumps(["callback_query", "message"])
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=poll_timeout + 10)
                ) as resp:
                    data = await resp.json()

            if not data.get("ok"):
                logger.warning(f"Approval bot poll error: {data}")
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    await _handle_approval_update(update)
                except Exception as e:
                    logger.error(f"Error handling approval update: {e}")

        except asyncio.CancelledError:
            logger.info("Approval bot polling cancelled")
            return
        except Exception as e:
            logger.warning(f"Approval bot polling error: {e}")
            await asyncio.sleep(5)


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

    # Avvia task periodici (salva riferimenti forti per evitare GC)
    def _keep(t):
        _background_tasks.add(t)
        t.add_done_callback(_background_tasks.discard)

    _keep(asyncio.create_task(periodic_cleanup()))
    _keep(asyncio.create_task(warmup_models()))
    _keep(asyncio.create_task(periodic_health_check()))

    # Carica memory services e avvia scheduler memoria
    load_memory_services_from_db()
    _keep(asyncio.create_task(memory_scheduler()))
    _keep(asyncio.create_task(proactive_check_loop()))

    # OpenClaw gateway operator (exec approval buttons via WS)
    _keep(asyncio.create_task(openclaw_operator_loop()))

    # Approval Bot: webhook (preferred) or polling fallback
    _keep(asyncio.create_task(approval_bot_setup()))

    # Pre-load Silero VAD model for WS audio reception
    try:
        init_vad()
    except Exception as e:
        logger.error(f"Silero VAD init failed (WS audio disabled): {e}")

    # Live session timeout monitor
    _keep(asyncio.create_task(live_session_monitor()))

    logger.info("✅ JARVIS Core ready!")
    yield
    logger.info("👋 JARVIS Core shutting down...")


app = FastAPI(title="Jarvis Core Orchestrator", lifespan=lifespan)

# ===========================================================================
# DEVICE AUTH MIDDLEWARE (Bearer token per AtomS3R e altri device firmware)
# ===========================================================================
# Protegge gli endpoint usati dal firmware. Se DEVICE_API_TOKEN è vuoto,
# l'autenticazione è disabilitata (retrocompatibilità).
DEVICE_AUTH_PATHS = {
    "/voice_command", "/voice_stream", "/device_config", "/device_status", "/heartbeat",
    "/device/config", "/device/heartbeat",
    "/room_temperature", "/speaker/suppress", "/speaker/restore", "/speaker/suppressed",
    # Note: /ws/audio WebSocket auth is handled inside the endpoint (query params)
    # because FastAPI HTTP middleware does not intercept WebSocket connections.
}

@app.middleware("http")
async def device_auth_middleware(request: Request, call_next):
    # Controlla solo se il token è configurato
    if config.DEVICE_API_TOKEN:
        # Verifica se il path richiede autenticazione device
        path = request.url.path
        # Match esatto o path che inizia con un prefix noto (es. /room_temperature/salotto)
        needs_auth = any(
            path == p or path.startswith(p + "/")
            for p in DEVICE_AUTH_PATHS
        )
        if needs_auth:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse(status_code=401, content={"error": "Missing Bearer token"})
            token = auth_header[7:]  # Rimuovi "Bearer "
            if token != config.DEVICE_API_TOKEN:
                logger.warning(f"Invalid device token from {request.client.host} on {path}")
                return JSONResponse(status_code=403, content={"error": "Invalid token"})
    return await call_next(request)

# Registra routers
app.include_router(user_router)
app.include_router(web_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(device_router)
app.include_router(image_router)
app.include_router(tools_router)


# ===========================================================================
# VIRTUAL MICROPHONE — Polling endpoint per risposta
# ===========================================================================

@app.get("/api/admin/vmic-response")
async def get_vmic_response(request_id: str = Query(...)):
    """Polling endpoint per il Virtual Microphone della dashboard."""
    data = _vmic_responses.get(request_id)
    if data:
        return data
    return JSONResponse(status_code=202, content={"response": None})


# ===========================================================================
# MIDDLEWARE — Access Logging per sicurezza
# ===========================================================================

# Path da escludere dal logging (troppo frequenti / non rilevanti)
_ACCESS_LOG_SKIP_PREFIXES = ("/assets/", "/health", "/favicon", "/api/admin/vmic-response")

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

        # Cleanup cast files
        try:
            from media_cast import cleanup_cast_files
            await cleanup_cast_files()
        except Exception as e:
            logger.warning(f"Cast cleanup failed: {e}")

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
    """Pulisce lo stato speaking dopo un delay (tempo stimato TTS). Legacy fallback."""
    if delay_seconds is None:
        delay_seconds = config.INTERVALS["tts_clear_delay"]
    await asyncio.sleep(delay_seconds)
    async with speaking_state_lock:
        if room in speaking_state:
            if time.time() - speaking_state[room]["started_at"] >= delay_seconds - 0.5:
                del speaking_state[room]
                logger.info(f"🔇 Speaking state auto-cleared for room: {room}")


# ===========================================================================
# LIVE SESSION — Continuous conversation mode
# ===========================================================================
# In live session, the device stays in a persistent conversation loop:
# - No wakeword between turns (trigger_listen after every TTS)
# - No speaker ID (locked to the user who activated the session)
# - No Qwen routing (everything goes straight to OpenClaw)
# - Concise prompt forces short, direct responses
# - Session ends via voice command, button (speaker_stop), or timeout

@dataclass
class LiveSession:
    device_id: str
    user_id: Optional[str]         # speaker_id from initial recognition (None if unidentified)
    speaker_name: str              # speaker name locked at activation
    location_id: str
    room: str
    media_player_id: Optional[str]  # None when using internal speaker
    use_internal_speaker: bool
    openclaw_session_user: str     # stable OpenClaw session key
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    turn_count: int = 0
    max_inactivity_s: float = 300.0   # 5 min silence → auto-close
    max_duration_s: float = 900.0     # 15 min absolute cap


_live_sessions: Dict[str, LiveSession] = {}


@dataclass
class PendingLiveSession:
    """Pre-session negotiation state when speaker is not recognized."""
    device_id: str
    device_config: Optional[dict]
    speaker_ctx: dict
    location: str
    room: str
    created_at: float = field(default_factory=time.time)
    attempts: int = 0
    max_attempts: int = 2
    timeout_s: float = 30.0


_pending_live_sessions: Dict[str, PendingLiveSession] = {}

# Keyword sets for pending session confirmation
_CONFIRM_YES = {"sì", "si", "ok", "vai", "certo", "procedi", "avvia", "yes", "sí"}
_CONFIRM_NO = {"no", "annulla", "lascia", "niente", "stop", "esci"}
_RETRY_ID = {"sono", "riconoscimi", "riconoscere", "prova", "riprova", "identifica", "identificami"}

# TTS instructions for live session: ultra-concise, no fluff
_LIVE_SESSION_TTS_INSTRUCTIONS = (
    "CRITICAL: You are in a LIVE VOICE SESSION — a real-time conversation like a phone call. "
    "Rules: 1) Be EXTREMELY concise — max 1-2 short sentences per reply. "
    "2) No filler words, no pleasantries, no 'certo', no 'ecco'. Go straight to the point. "
    "3) Use natural spoken Italian with clear punctuation. "
    "4) NO markdown, NO bullet points, NO asterisks, NO special characters. "
    "5) If you need to ask a clarification, ask ONE short question. "
    "6) The user CANNOT interrupt you once you start speaking, so keep it SHORT. "
    "7) Sound warm but efficient — think quick radio exchange, not essay."
)

# Activation: keyword-based (all keywords must be present, order-independent)
# Handles: "avvia una sessione live", "avvia la live session", "inizia sessione live", etc.
_LIVE_SESSION_ACTIVATE_KEYWORDS = [
    {"sessione", "live"},  # "avvia una sessione live", "sessione live per favore"
    {"live", "session"},   # "avvia una live session", "start live session"
]
_LIVE_SESSION_ACTIVATE_VERBS = {
    "avvia", "avviare", "inizia", "iniziare", "apri", "aprire",
    "attiva", "attivare", "start", "parti", "partire",
}

# Deactivation: keyword-based (same approach)
_LIVE_SESSION_DEACTIVATE_KEYWORDS = [
    {"termina", "sessione"},
    {"fine", "sessione"},
    {"chiudi", "sessione"},
    {"stop", "sessione"},
    {"esci", "sessione"},
    {"end", "session"},
    {"stop", "session"},
]


def _is_live_session_activation(text: str) -> bool:
    """Check if transcribed text is a live session activation command.

    Matches when text contains an activation verb AND a keyword set.
    E.g.: 'avvia una sessione live' → verb 'avvia' + keywords {'sessione', 'live'} → True
    """
    words = set(text.lower().split())
    has_verb = bool(words & _LIVE_SESSION_ACTIVATE_VERBS)
    has_keywords = any(kw_set <= words for kw_set in _LIVE_SESSION_ACTIVATE_KEYWORDS)
    return has_verb and has_keywords


def _is_live_session_deactivation(text: str) -> bool:
    """Check if transcribed text is a live session deactivation command."""
    words = set(text.lower().split())
    return any(kw_set <= words for kw_set in _LIVE_SESSION_DEACTIVATE_KEYWORDS)


async def _activate_live_session(
    device_id: str,
    device_config: Optional[dict],
    speaker_ctx: dict,
    location: str,
    room: str,
):
    """Resolve speaker and start live session. Used by both keyword fast-path and Qwen routing.

    If speaker is recognized → immediate start with personalized greeting.
    If not recognized → enter pending state, ask for confirmation.
    """
    _use_internal = device_config.get("use_internal_speaker", False) if device_config else False
    target_speaker = None
    if not _use_internal:
        if device_config:
            target_speaker = device_config.get("output_speaker")
        if not target_speaker:
            room_speakers_map = get_room_speakers(location)
            target_speaker = room_speakers_map.get(room, None)

    if not (target_speaker or _use_internal):
        logger.warning(f"Live session: no speaker found for {device_id}, cannot activate")
        from ws_audio_handler import notify_tts_done
        await notify_tts_done(device_id)
        return

    # Speaker recognized → immediate start with greeting
    if speaker_ctx.get("speaker_identified"):
        name = speaker_ctx.get("speaker_name", "")
        greeting = f"Ciao {name}, sessione live attivata."
        await start_live_session(
            device_id=device_id,
            speaker_id=speaker_ctx.get("speaker_id"),
            speaker_name=name,
            location_id=location,
            room=room,
            media_player_id=target_speaker,
            use_internal_speaker=_use_internal,
            greeting=greeting,
        )
        return

    # Speaker NOT recognized → pending state, ask for confirmation
    pending = PendingLiveSession(
        device_id=device_id,
        device_config=device_config,
        speaker_ctx=speaker_ctx,
        location=location,
        room=room,
    )
    _pending_live_sessions[device_id] = pending
    logger.info(f"🎙️ Live session PENDING: speaker not recognized for {device_id}, asking confirmation")

    # TTS question + trigger listen for response
    question = "Non ti ho riconosciuto. Vuoi avviare una sessione live anonima?"
    if _use_internal:
        from internal_tts import speak_to_device
        await speak_to_device(question, device_id)
        await asyncio.sleep(0.15)
        await trigger_device_listen(device_id, silent=True)
    else:
        await speak(question, target_speaker, location)
        schedule_post_tts(
            media_player_id=target_speaker,
            location_id=location,
            room=room,
            device_id=device_id,
            is_multi_turn=True,
            text_length=len(question),
        )


async def _handle_pending_live_session(device_id: str, audio_bytes: bytes):
    """Handle audio response during pre-live-session speaker verification.

    Called from _process_ws_audio() when device has a pending live session.
    Processes the user's yes/no/re-id response.
    """
    pending = _pending_live_sessions.get(device_id)
    if not pending:
        return

    # Check timeout
    if time.time() - pending.created_at > pending.timeout_s:
        logger.info(f"🎙️ Pending live session TIMEOUT for {device_id}")
        _pending_live_sessions.pop(device_id, None)
        from ws_audio_handler import notify_tts_done
        await notify_tts_done(device_id)
        return

    # Audio normalize → STT
    import numpy as _np_pend
    _raw = _np_pend.frombuffer(audio_bytes, dtype=_np_pend.int16).astype(_np_pend.float32)
    _peak = _np_pend.max(_np_pend.abs(_raw))
    if _peak > 0:
        _gain = min(28000.0 / _peak, 20.0)
        _raw = (_raw * _gain).clip(-32768, 32767)
    clean_audio = _raw.astype(_np_pend.int16).tobytes()

    text = await transcribe_audio(clean_audio)
    if not text or not text.strip():
        logger.info(f"🎙️ Pending live session: no speech, re-triggering listen")
        await trigger_device_listen(device_id, silent=True)
        return

    logger.info(f"🎙️ Pending live session response from {device_id}: '{text}'")
    words = set(text.lower().split())

    _use_internal = pending.device_config.get("use_internal_speaker", False) if pending.device_config else False

    # Helper: send TTS + trigger listen for this pending session
    async def _pending_tts_and_listen(msg: str):
        if _use_internal:
            from internal_tts import speak_to_device
            await speak_to_device(msg, device_id)
            await asyncio.sleep(0.15)
            await trigger_device_listen(device_id, silent=True)
        else:
            target = pending.device_config.get("output_speaker") if pending.device_config else None
            if not target:
                room_speakers_map = get_room_speakers(pending.location)
                target = room_speakers_map.get(pending.room)
            if target:
                await speak(msg, target, pending.location)
                schedule_post_tts(
                    media_player_id=target, location_id=pending.location,
                    room=pending.room, device_id=device_id,
                    is_multi_turn=True, text_length=len(msg),
                )

    # Helper: resolve speaker and start session
    async def _start_with_ctx(ctx: dict, greet: Optional[str] = None):
        _pending_live_sessions.pop(device_id, None)
        target = pending.device_config.get("output_speaker") if pending.device_config else None
        if not target and not _use_internal:
            room_speakers_map = get_room_speakers(pending.location)
            target = room_speakers_map.get(pending.room)
        await start_live_session(
            device_id=device_id,
            speaker_id=ctx.get("speaker_id"),
            speaker_name=ctx.get("speaker_name", "Anonimo"),
            location_id=pending.location,
            room=pending.room,
            media_player_id=target,
            use_internal_speaker=_use_internal,
            greeting=greet,
        )

    # Check for re-identification request ("sono Marco", "riconoscimi", "prova")
    if words & _RETRY_ID:
        pending.attempts += 1
        logger.info(f"🎙️ Pending live session: re-ID attempt #{pending.attempts}")

        # Run speaker ID on THIS audio (the user just spoke to identify themselves)
        loop = asyncio.get_running_loop()
        new_ctx = await loop.run_in_executor(None, build_speaker_context, audio_bytes, "AtomS3R")

        if new_ctx.get("speaker_identified"):
            name = new_ctx.get("speaker_name", "")
            logger.info(f"🎙️ Re-ID successful: {name}")
            await _start_with_ctx(new_ctx, f"Ciao {name}, sessione live attivata.")
        elif pending.attempts >= pending.max_attempts:
            logger.info(f"🎙️ Re-ID failed after {pending.attempts} attempts, starting anonymous")
            await _start_with_ctx(pending.speaker_ctx, "Non riesco a riconoscerti. Sessione avviata come anonimo.")
        else:
            await _pending_tts_and_listen("Ancora non ti riconosco. Prova di nuovo, oppure di' sì per una sessione anonima.")
        return

    # Check for "no" / reject
    if words & _CONFIRM_NO:
        logger.info(f"🎙️ Pending live session REJECTED by user for {device_id}")
        _pending_live_sessions.pop(device_id, None)
        if _use_internal:
            from internal_tts import speak_to_device
            await speak_to_device("Ok, annullato.", device_id)
            await asyncio.sleep(0.15)
        from ws_audio_handler import notify_tts_done
        await notify_tts_done(device_id)
        return

    # "sì" or any other response → start anonymous session
    logger.info(f"🎙️ Pending live session ACCEPTED (anonymous) for {device_id}")
    await _start_with_ctx(pending.speaker_ctx, "Sessione live anonima attivata.")


async def start_live_session(
    device_id: str,
    speaker_id: Optional[str],
    speaker_name: str,
    location_id: str,
    room: str,
    media_player_id: Optional[str],
    use_internal_speaker: bool = False,
    greeting: Optional[str] = None,
) -> LiveSession:
    """Create and register a new live session for a device."""
    # Clean up any pending session for this device
    _pending_live_sessions.pop(device_id, None)

    # Build stable session key (same logic as _handle_openclaw_voice)
    if speaker_id:
        session_user = f"live-speaker-{speaker_id}"
    else:
        session_user = f"live-device-{room.lower()}"

    session = LiveSession(
        device_id=device_id,
        user_id=speaker_id,
        speaker_name=speaker_name,
        location_id=location_id,
        room=room,
        media_player_id=media_player_id,
        use_internal_speaker=use_internal_speaker,
        openclaw_session_user=session_user,
    )
    _live_sessions[device_id] = session

    logger.info(f"🎙️ Live session STARTED: device={device_id}, room={room}, "
                f"speaker={speaker_name}, session_user={session_user}, "
                f"internal_speaker={use_internal_speaker}")

    # Notify device + wakeword server relay
    from ws_audio_handler import notify_live_session_start
    await notify_live_session_start(device_id)

    # Confirm via TTS
    confirm_msg = greeting or "Sessione live attivata. Parla pure."
    if use_internal_speaker:
        from internal_tts import speak_to_device
        success, duration = await speak_to_device(confirm_msg, device_id)
        # Small delay for audio flush, then trigger first listen
        await asyncio.sleep(0.15)
        await trigger_device_listen(device_id, silent=True)
    else:
        await speak(confirm_msg, media_player_id, location_id)
        text_length = len(confirm_msg)
        schedule_post_tts(
            media_player_id=media_player_id,
            location_id=location_id,
            room=room,
            device_id=device_id,
            is_multi_turn=True,
            text_length=text_length,
        )

    return session


async def end_live_session(device_id: str, reason: str = "unknown"):
    """End a live session and notify the user."""
    session = _live_sessions.pop(device_id, None)
    if not session:
        return

    duration = time.time() - session.started_at

    # Notify device + wakeword server relay
    from ws_audio_handler import notify_live_session_end
    await notify_live_session_end(device_id)

    # Notify user based on reason
    goodbye_messages = {
        "voice_command": "Sessione live terminata.",
        "inactivity_timeout": "Sessione live terminata per inattività.",
        "max_duration": "Sessione live terminata, tempo massimo raggiunto.",
        "button_stop": "Sessione live terminata.",
    }
    msg = goodbye_messages.get(reason, "Sessione live terminata.")

    try:
        if session.use_internal_speaker:
            from internal_tts import speak_to_device
            success, _tts_dur = await speak_to_device(msg, device_id)
            await asyncio.sleep(0.15)
            from ws_audio_handler import notify_tts_done
            await notify_tts_done(device_id)
        else:
            await speak(msg, session.media_player_id, session.location_id)
            # Send tts_done after goodbye message to close relay and go IDLE
            text_length = len(msg)
            schedule_post_tts(
                media_player_id=session.media_player_id,
                location_id=session.location_id,
                room=session.room,
                device_id=device_id,
                is_multi_turn=False,  # Not multi-turn: session is over, go IDLE
                text_length=text_length,
            )
    except Exception as e:
        logger.error(f"Live session goodbye TTS failed: {e}")

    logger.info(f"🎙️ Live session ENDED: device={device_id}, reason={reason}, "
                f"duration={duration:.0f}s, turns={session.turn_count}")


async def handle_live_session_turn(device_id: str, audio_bytes: bytes):
    """
    Process one turn of a live session.
    Simplified pipeline: audio normalize → STT → deactivation check → OpenClaw → TTS → trigger_listen.
    No speaker ID, no Qwen routing.
    """
    session = _live_sessions.get(device_id)
    if not session:
        return

    session.last_activity = time.time()
    session.turn_count += 1

    logger.info(f"🎙️ Live session turn #{session.turn_count} from {device_id}")

    # 1. Audio normalization (same as normal pipeline)
    import numpy as _np_live
    _raw = _np_live.frombuffer(audio_bytes, dtype=_np_live.int16).astype(_np_live.float32)
    _peak = _np_live.max(_np_live.abs(_raw))
    if _peak > 0:
        _target_peak = 28000.0
        _gain = min(_target_peak / _peak, 20.0)
        _raw = (_raw * _gain).clip(-32768, 32767)
    clean_audio = _raw.astype(_np_live.int16).tobytes()

    # 2. STT only (no speaker ID, no Qwen normalize)
    text = await transcribe_audio(clean_audio)

    if not text or not text.strip():
        logger.info(f"🎙️ Live session: no speech detected, re-triggering listen")
        # Re-trigger listen (user may have been silent or audio was noise)
        await trigger_device_listen(device_id, silent=True)
        return

    logger.info(f"🎙️ Live session turn #{session.turn_count}: '{text[:120]}'")

    # 3. Check deactivation
    if _is_live_session_deactivation(text):
        await end_live_session(device_id, reason="voice_command")
        return

    # 4. Save chat message (with locked speaker identity)
    save_chat_message("user", text, "AtomS3R", session.user_id, session.speaker_name)

    # 5. Forward to OpenClaw (no routing, direct)
    context = {
        "source": "AtomS3R",
        "room": session.room,
        "device_id": device_id,
        "location": session.location_id,
        "speaker_id": session.user_id,
        "speaker_name": session.speaker_name,
        "speaker_identified": session.user_id is not None,
        "device_config": get_device_speaker_config(device_id),
    }

    # Build streaming TTS callback
    if session.use_internal_speaker:
        from internal_tts import speak_to_device
        async def _live_tts_chunk(chunk: str, is_first: bool):
            logger.info(f"🎙️ Live TTS chunk internal ({len(chunk)} chars): {chunk[:80]}...")
            try:
                success, duration = await speak_to_device(chunk, device_id)
            except Exception as e:
                logger.error(f"Live TTS chunk error: {e}")
    else:
        async def _live_tts_chunk(chunk: str, is_first: bool):
            logger.info(f"🎙️ Live TTS chunk ({len(chunk)} chars): {chunk[:80]}...")
            try:
                await speak(chunk, session.media_player_id, session.location_id)
            except Exception as e:
                logger.error(f"Live TTS chunk error: {e}")

    # Use live session TTS instructions (ultra-concise)
    # We call forward_to_openclaw with the live session context
    response, response_id = await forward_to_openclaw(
        text, context, hint="live_session",
        stream_tts_callback=_live_tts_chunk,
        session_user=session.openclaw_session_user,
    )

    # 6. Save assistant response
    save_chat_message("assistant", response, "JARVIS", None, "Jarvis")

    # 7. Post-TTS: always trigger_listen (multi-turn always on in live session)
    if response:
        if session.use_internal_speaker:
            # Internal speaker: small delay for audio flush, then re-trigger listen
            await asyncio.sleep(0.15)
            await trigger_device_listen(device_id, silent=True)
            logger.info(f"🎙️ Live session: internal TTS done ({len(response)} chars), "
                        f"triggered listen on {device_id}")
        else:
            await set_speaking_state(session.room, True, device_id)
            schedule_post_tts(
                media_player_id=session.media_player_id,
                location_id=session.location_id,
                room=session.room,
                device_id=device_id,
                is_multi_turn=True,  # Always re-trigger listen in live session
                text_length=len(response),
            )
            logger.info(f"🎙️ Live session: TTS scheduled ({len(response)} chars), "
                        f"will trigger_listen after completion")
    else:
        # Empty response — still re-trigger listen
        await trigger_device_listen(device_id, silent=True)


async def live_session_monitor():
    """Background task: check live session and pending session timeouts every 10s."""
    while True:
        await asyncio.sleep(10)
        now = time.time()

        # Active live sessions
        for device_id, session in list(_live_sessions.items()):
            if now - session.last_activity > session.max_inactivity_s:
                logger.info(f"🎙️ Live session timeout (inactivity): {device_id}")
                await end_live_session(device_id, reason="inactivity_timeout")
            elif now - session.started_at > session.max_duration_s:
                logger.info(f"🎙️ Live session timeout (max duration): {device_id}")
                await end_live_session(device_id, reason="max_duration")

        # Pending live sessions (speaker verification timeout)
        for device_id, pending in list(_pending_live_sessions.items()):
            if now - pending.created_at > pending.timeout_s:
                logger.info(f"🎙️ Pending live session timeout: {device_id}")
                _pending_live_sessions.pop(device_id, None)
                from ws_audio_handler import notify_tts_done
                await notify_tts_done(device_id)


def get_live_session(device_id: str) -> Optional[LiveSession]:
    """Get live session for a device, or None."""
    return _live_sessions.get(device_id)


# ===========================================================================
# HELPER: TTS Completion Polling (replaces duration estimates)
# ===========================================================================

# Pending post-TTS tasks per device (for cancellation on speaker_stop)
_pending_tts_tasks: Dict[str, asyncio.Task] = {}


async def wait_for_tts_completion(
    media_player_id: str,
    location_id: str,
    text_length: int = 0,
    max_wait: float = 90.0,
    poll_interval: float = 0.2,
) -> float:
    """
    Wait for Alexa TTS playback to finish.

    Hybrid approach:
    - First tries polling the media_player entity state (future-proof: works if
      alexa_media_player integration is patched to report TTS as "playing")
    - If no "playing" state is detected within 3s, falls back to a text-length
      estimate calibrated empirically (TTS Calibration 2026-02-25, R²=0.9977)

    Returns actual time waited (seconds).
    """
    # Calibrated coefficients (tts-calibration 2026-02-25, echo_dot_garage, 78 phrases)
    # duration_s = A * chars + B  (R²=0.9977, P95 error=1.27s)
    TTS_COEFF_A = 0.063051        # seconds per character (~15.9 chars/s)
    TTS_COEFF_B = -0.071          # intercept
    TTS_SAFETY_MARGIN = 1.10      # +10% buffer
    TTS_NOTIFY_LATENCY = 1.982    # notify → speech start (measured)
    MIN_WAIT = 3.0
    if text_length > 0:
        speech_est = (TTS_COEFF_A * text_length + TTS_COEFF_B) * TTS_SAFETY_MARGIN
        estimated = max(MIN_WAIT, speech_est + TTS_NOTIFY_LATENCY)
    else:
        estimated = MIN_WAIT

    start = time.time()
    was_playing = False
    consecutive_not_playing = 0
    polling_gave_up = False

    while (time.time() - start) < max_wait:
        try:
            state_data = await multi_ha.get_state(location_id, media_player_id)
            if state_data:
                current_state = (state_data.get("state") or "").lower()
                if current_state == "playing":
                    was_playing = True
                    consecutive_not_playing = 0
                elif was_playing:
                    consecutive_not_playing += 1
                    if consecutive_not_playing >= 2:
                        # Two consecutive non-playing polls → TTS done
                        break
                elif not was_playing and (time.time() - start) > 3.0:
                    # Polling can't see TTS state (expected with current alexa_media_player)
                    # Fall back to time estimate
                    polling_gave_up = True
                    break
        except Exception as e:
            logger.debug(f"TTS poll error for {media_player_id}: {e}")

        await asyncio.sleep(poll_interval)

    if polling_gave_up:
        # Wait remaining estimated time (subtract time already spent polling)
        remaining = estimated - (time.time() - start)
        if remaining > 0:
            logger.info(f"🔊 TTS polling: no state change detected, using estimate "
                        f"({estimated:.1f}s for {text_length} chars, waiting {remaining:.1f}s more)")
            await asyncio.sleep(remaining)

    duration = time.time() - start
    logger.info(f"🔊 TTS completion: {media_player_id} after {duration:.1f}s "
                f"(polling={'detected' if was_playing else 'fallback_estimate'})")
    return duration


async def _post_tts_handler(
    media_player_id: str,
    location_id: str,
    room: str,
    device_id: str,
    is_multi_turn: bool = False,
    text_length: int = 0,
):
    """
    Background task: after TTS is sent to Alexa, wait for playback to finish, then:
    1. Clear speaking state
    2. Send tts_done to device (BUSY → IDLE)
    3. If multi-turn: trigger device to listen again
    """
    try:
        duration = await wait_for_tts_completion(media_player_id, location_id, text_length=text_length)

        # Clear speaking state
        async with speaking_state_lock:
            if room in speaking_state:
                del speaking_state[room]
                logger.info(f"🔇 Speaking state cleared (TTS done after {duration:.1f}s)")

        if is_multi_turn and device_id and device_id != "unknown":
            # Multi-turn: trigger listen directly (skip tts_done to keep relay open)
            # Device transitions BUSY → LISTENING without going through IDLE
            success = await trigger_device_listen(device_id, silent=True)
            if success:
                logger.info(f"🔄 Multi-turn: triggered listen on {device_id} "
                            f"(after {duration:.1f}s TTS)")
            else:
                logger.warning(f"🔄 Multi-turn: failed to trigger {device_id}")
        elif device_id and device_id != "unknown":
            # Not multi-turn: notify device TTS is done (BUSY → IDLE, closes relay)
            from ws_audio_handler import notify_tts_done
            await notify_tts_done(device_id)

    except asyncio.CancelledError:
        logger.info(f"Post-TTS handler cancelled for device {device_id} (speaker_stop?)")
    except Exception as e:
        logger.error(f"Post-TTS handler error for device {device_id}: {e}")
    finally:
        _pending_tts_tasks.pop(device_id, None)


def schedule_post_tts(
    media_player_id: str,
    location_id: str,
    room: str,
    device_id: str,
    is_multi_turn: bool = False,
    text_length: int = 0,
):
    """
    Schedule a post-TTS handler that polls Alexa state and handles completion.
    Cancels any existing handler for this device.
    """
    # Cancel existing task for this device
    existing = _pending_tts_tasks.get(device_id)
    if existing and not existing.done():
        existing.cancel()
        logger.debug(f"Cancelled previous post-TTS task for {device_id}")

    task = asyncio.create_task(
        _post_tts_handler(media_player_id, location_id, room, device_id, is_multi_turn, text_length)
    )
    _pending_tts_tasks[device_id] = task
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def cancel_pending_tts_task(device_id: str):
    """Cancel pending post-TTS handler for a device (called on speaker_stop)."""
    task = _pending_tts_tasks.pop(device_id, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"Cancelled post-TTS task for {device_id} (speaker_stop)")


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

async def forward_to_openclaw(text: str, context: dict, hint: str = "",
                              stream_tts_callback=None,
                              session_user: str = None) -> tuple:
    """
    Forward request to OpenClaw Gateway via OpenResponses API (POST /v1/responses).

    Uses SSE streaming (stream: True) to avoid hard timeout on long multi-turn
    conversations. As long as OpenClaw sends SSE events (deltas, tool calls, etc.),
    the connection stays alive. Timeout triggers only on prolonged silence.

    If stream_tts_callback is provided, sentences are delivered progressively
    to TTS as they complete during streaming (sentence boundaries: .!?\n).

    On timeout or error, falls back to local Qwen quick response.

    Args:
        text: User input text
        context: Request context dict
        hint: Optional hint for OpenClaw (e.g. "domotics")
        stream_tts_callback: Optional async callable(chunk: str, is_first: bool) -> None
                             Called for each complete sentence during streaming.
        session_user: Optional stable user identifier for OpenClaw multi-turn sessions.
                      When provided, OpenClaw maintains conversation history across calls.

    Returns:
        tuple: (response_text: str, response_id: str | None)
    """
    import aiohttp
    import json as _json

    if not config.OPENCLAW_URL or not config.OPENCLAW_TOKEN:
        logger.warning("OpenClaw not configured, falling back to local response")
        return await get_quick_response(text, context), None

    # Build message with context for OpenClaw
    speaker_name = context.get("speaker_name", "Sconosciuto")
    speaker_id = context.get("speaker_id")
    speaker_identified = context.get("speaker_identified", False)
    source = context.get("source", "unknown")
    location = context.get("location", "")
    room = context.get("room", "")

    # Compose context-enriched message
    context_parts = []
    if speaker_name and speaker_name != "Sconosciuto":
        context_parts.append(f"user: {speaker_name}")
    elif not speaker_identified:
        context_parts.append("user: non identificato")
    if location:
        context_parts.append(f"location: {location}")
    if room:
        context_parts.append(f"room: {room}")
    if source:
        context_parts.append(f"source: {source}")
    if hint:
        context_parts.append(f"hint: {hint}")

    if context_parts:
        message_text = f"[{', '.join(context_parts)}] {text}"
    else:
        message_text = text

    # OpenResponses API format: POST /v1/responses
    # Inietta istruzione TTS: live session usa prompt ultra-conciso,
    # normale usa prompt conversazionale standard
    if hint == "live_session":
        tts_instructions = _LIVE_SESSION_TTS_INSTRUCTIONS
    elif source in ("AtomS3R", "VirtualMic"):
        device_cfg = context.get("device_config") or {}
        is_internal = device_cfg.get("use_internal_speaker", False)
        tts_target = "the device's built-in speaker (Edge TTS)" if is_internal else "a voice assistant (Alexa TTS)"
        tts_instructions = (
            f"IMPORTANT: This response will be read aloud via {tts_target}. "
            "Format accordingly: use natural spoken Italian, no markdown, no bullet points, "
            "no asterisks, no emojis, no special characters. Use short sentences with clear "
            "punctuation (commas, periods, exclamation marks, question marks) for natural speech "
            "rhythm. Add emphasis and expressiveness: use exclamation marks for enthusiasm, "
            "ellipsis for suspense or pauses, rhetorical questions to engage. Vary sentence "
            "length and tone — mix short punchy phrases with longer flowing ones. Sound warm, "
            "lively and human, not robotic or flat. Be concise but conversational — max 3-4 "
            "sentences unless the topic requires more."
        )
    else:
        tts_instructions = None

    payload = {
        "input": message_text,
        "model": "openclaw:main",
        "stream": True,
    }
    # Multi-turn session: 'user' param enables stable session routing in OpenClaw
    if session_user:
        payload["user"] = str(session_user)
    # User metadata per OpenClaw (identità parlante)
    if speaker_id or (speaker_name and speaker_name != "Sconosciuto"):
        payload["metadata"] = {
            "user_id": str(speaker_id) if speaker_id is not None else None,
            "user_name": str(speaker_name) if speaker_name else None,
            "identified": str(speaker_identified),
        }
    if tts_instructions:
        payload["instructions"] = tts_instructions

    try:
        timeout = aiohttp.ClientTimeout(
            total=config.OPENCLAW_TIMEOUT_TOTAL,   # 300s max totale
            sock_read=config.OPENCLAW_TIMEOUT_READ  # 90s max silenzio tra chunk
        )
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {config.OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }
            async with session.post(
                f"{config.OPENCLAW_URL}/v1/responses",
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"OpenClaw error {resp.status}: {body[:200]}")
                    return await get_quick_response(text, context), None

                # Parse SSE stream
                accumulated_text = ""  # Tutto il testo ricevuto finora
                pending_chunk = ""     # Buffer per sentence splitting (solo con callback)
                event_count = 0
                final_status = None
                response_id = None     # OpenClaw response ID for session tracking
                is_first_chunk = True  # Per sound_type solo sul primo chunk
                chunks_sent = 0

                # Reset chunk counter per streaming TTS
                if stream_tts_callback:
                    _flush_tts_sentences.chunks_sent = 0

                # Minimum chunk size per evitare frasi troppo corte
                MIN_CHUNK_LEN = 20

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()

                    # Fine stream
                    if line == "data: [DONE]":
                        break

                    # Solo righe "data:" contengono payload JSON
                    if not line.startswith("data: "):
                        continue

                    try:
                        event = _json.loads(line[6:])
                    except _json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")
                    event_count += 1

                    # Accumula testo dai delta incrementali
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta", "")
                        if delta:
                            accumulated_text += delta

                            # Streaming TTS: split at sentence boundaries
                            if stream_tts_callback:
                                pending_chunk += delta
                                pending_chunk = await _flush_tts_sentences(
                                    pending_chunk, stream_tts_callback,
                                    is_first_chunk, MIN_CHUNK_LEN,
                                    flush_all=False
                                )
                                if chunks_sent != _flush_tts_sentences.chunks_sent:
                                    is_first_chunk = False
                                    chunks_sent = _flush_tts_sentences.chunks_sent

                    # Testo completo — più affidabile dei delta accumulati
                    elif event_type == "response.output_text.done":
                        done_text = event.get("text", "")
                        if done_text:
                            accumulated_text = done_text

                    # Risposta completata — estrai testo se non accumulato
                    elif event_type == "response.completed":
                        resp_data = event.get("response", {})
                        final_status = resp_data.get("status")
                        response_id = resp_data.get("id")
                        if not accumulated_text:
                            accumulated_text = _extract_openclaw_response(resp_data)

                    # Errore
                    elif event_type == "response.failed":
                        error = event.get("response", {}).get("error", {})
                        logger.error(f"OpenClaw stream failed: {error.get('code', '?')}: {error.get('message', '?')}")
                        return await get_quick_response(text, context), None

                # Flush remaining pending chunk (coda che non termina con .!?\n)
                if stream_tts_callback and pending_chunk.strip():
                    await _flush_tts_sentences(
                        pending_chunk, stream_tts_callback,
                        is_first_chunk, min_len=0,
                        flush_all=True
                    )

                if accumulated_text:
                    logger.info(
                        f"OpenClaw stream response ({len(accumulated_text)} chars, "
                        f"{event_count} events, status={final_status}, "
                        f"resp_id={response_id[:20] + '...' if response_id and len(response_id) > 20 else response_id}, "
                        f"tts_chunks={_flush_tts_sentences.chunks_sent if stream_tts_callback else 'n/a'})"
                    )
                    return accumulated_text, response_id

                logger.warning(f"OpenClaw stream ended with no text ({event_count} events, status={final_status})")

    except asyncio.TimeoutError:
        logger.warning(
            f"OpenClaw stream timeout (total={config.OPENCLAW_TIMEOUT_TOTAL}s, "
            f"read={config.OPENCLAW_TIMEOUT_READ}s)"
        )
        return "Mi dispiace, l'operazione sta richiedendo più tempo del previsto. Riprova tra poco.", None
    except aiohttp.ClientConnectorError:
        logger.warning("OpenClaw unreachable, falling back to local")
        service_status.set_offline("openclaw")
    except Exception as e:
        logger.error(f"OpenClaw stream error: {e}")

    # Fallback: local Qwen quick response
    return await get_quick_response(text, context), None


# Sentence boundary regex: split at ". " "! " "? " or "\n" (keeping the delimiter)
_SENTENCE_BOUNDARY_RE = re.compile(r'(?<=[.!?])\s+|(?<=\n)')


async def _flush_tts_sentences(pending: str, callback, is_first: bool,
                                min_len: int = 20, flush_all: bool = False) -> str:
    """
    Split pending text at sentence boundaries and send complete sentences to TTS callback.

    Returns the remaining (unsent) text that doesn't end at a sentence boundary yet.
    Tracks total chunks sent via _flush_tts_sentences.chunks_sent attribute.

    Args:
        pending: Buffer of accumulated text not yet sent to TTS
        callback: async callable(chunk: str, is_first: bool) -> None
        is_first: Whether this is the first chunk (for intro sound)
        min_len: Minimum chunk length before sending (avoids very short TTS)
        flush_all: If True, send everything remaining (end of stream)
    """
    if flush_all:
        # End of stream: send whatever is left
        chunk = pending.strip()
        if chunk:
            try:
                await callback(chunk, is_first)
                _flush_tts_sentences.chunks_sent += 1
            except Exception as e:
                logger.error(f"TTS stream callback error (flush): {e}")
        return ""

    # Split at sentence boundaries
    parts = _SENTENCE_BOUNDARY_RE.split(pending)

    if len(parts) <= 1:
        # No sentence boundary found yet, keep buffering
        return pending

    # Send all complete sentences (everything except the last part which is incomplete)
    for i in range(len(parts) - 1):
        chunk = parts[i].strip()
        if not chunk:
            continue
        if len(chunk) < min_len and i < len(parts) - 2:
            # Chunk too short and not the last complete sentence — merge with next
            parts[i + 1] = parts[i] + " " + parts[i + 1]
            continue
        try:
            await callback(chunk, is_first)
            _flush_tts_sentences.chunks_sent += 1
            is_first = False
        except Exception as e:
            logger.error(f"TTS stream callback error: {e}")

    # Return the remaining incomplete sentence
    return parts[-1]

# Initialize chunk counter
_flush_tts_sentences.chunks_sent = 0


def _extract_openclaw_response(data: dict) -> str:
    """
    Extract text from OpenResponses API response format.

    Response structure:
    {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "..."}]
            }
        ]
    }
    """
    output = data.get("output", [])
    texts = []
    for item in output:
        if item.get("type") == "message" and item.get("role") == "assistant":
            content = item.get("content", [])
            for part in content:
                if part.get("type") == "output_text" and part.get("text"):
                    texts.append(part["text"])
    return "\n\n".join(texts) if texts else ""


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
        text = await normalize_stt_text(text)
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


@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    """Receives Telegram Bot updates via webhook (messages + callback_query).

    This is the unified handler for ALL Telegram approval bot interactions.
    Telegram sends POST with a single Update object.
    """
    try:
        update = await request.json()
    except Exception:
        return {"status": "bad_request"}

    try:
        await _handle_approval_update(update)
    except Exception as e:
        logger.error(f"Error handling telegram webhook update: {e}")

    # Telegram expects 200 OK quickly; actual processing is done above
    return {"status": "ok"}


@app.post("/telegram_callback")
async def telegram_callback(request: Request):
    """Legacy alias — redirects to unified webhook handler."""
    return await telegram_webhook(request)


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


# ===========================================================================
# DEVICE TRIGGER LISTEN (per multi-turn e enrollment remoto)
# ===========================================================================

@app.post("/device/{device_id}/trigger_listen")
async def api_trigger_listen(device_id: str, request: Request):
    """
    REST API to trigger a device to start listening via persistent WebSocket.
    Used by: dashboard for remote enrollment, test/debug.

    Body (optional JSON):
      {"silent": false}  -- false for enrollment (plays wake sound on device)
                            true for multi-turn follow-up (default, no sound)
    """
    silent = True
    try:
        body = await request.json()
        silent = body.get("silent", True)
    except Exception:
        pass  # No body or invalid JSON — use default silent=True

    device_id = device_id.upper().strip()
    success = await trigger_device_listen(device_id, silent=silent)

    if success:
        return {"status": "ok", "device_id": device_id, "silent": silent}
    else:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": f"Device {device_id} not connected via persistent WS"}
        )


@app.get("/devices/connected")
async def api_connected_devices():
    """List currently connected devices via persistent WebSocket."""
    devices = await get_connected_devices()
    return {"devices": devices, "count": len(devices)}


# ===========================================================================
# SPEAKER SUPPRESS/RESTORE (per wake word noise reduction)
# ===========================================================================

@app.post("/speaker/suppress")
async def speaker_suppress_endpoint(request: Request):
    """
    Endpoint chiamato dall'AtomS3R appena rileva la wake word.
    Abbassa il volume dello speaker Echo associato al device al 10%.

    Flusso:
    1. Lookup device_id → output_speaker + location_id
    2. Controlla se lo speaker sta riproducendo (se idle → skip)
    3. Salva volume corrente, imposta 10%
    4. Avvia safety timeout (30s auto-restore)

    Il restore avviene automaticamente nel /voice_stream dopo lo STT,
    oppure può essere chiamato esplicitamente via /speaker/restore.
    """
    data = await request.json()
    device_id = data.get("device_id", "").upper().strip()

    if not device_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "device_id required"})

    result = await suppress_speaker(device_id)
    return result


@app.post("/speaker/restore")
async def speaker_restore_endpoint(request: Request):
    """
    Endpoint per ripristinare manualmente il volume dello speaker.
    Normalmente il restore è automatico (dopo STT o safety timeout),
    ma questo endpoint serve come fallback esplicito.
    """
    data = await request.json()
    device_id = data.get("device_id", "").upper().strip()

    if not device_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "device_id required"})

    result = await restore_speaker(device_id)
    return result


@app.get("/speaker/suppressed")
async def speaker_suppressed_status():
    """Debug endpoint: mostra tutti gli speaker attualmente soppressi."""
    return get_suppressed_speakers()


# Temperature caching strategy:
# - WEATHER entities: cache the VALUE for 12h (temperature changes slowly)
# - SENSOR entities: cache only the ENTITY_ID for 24h (entity name doesn't change),
#   but always read the live value via lightweight GET /api/states/<entity_id>
#   (sensor values can change rapidly — fire, heating, etc.)

# Weather value cache: (room, location) -> {"result": dict, "timestamp": float}
_temp_weather_cache: Dict[str, dict] = {}
_TEMP_WEATHER_CACHE_TTL = 3600     # 1 ora

# Entity resolution cache: (room, location) -> {"entity_id": str, "source": str, "loc": str, "timestamp": float}
# Avoids expensive bulk fetch to resolve which entity matches the room name
_temp_entity_cache: Dict[str, dict] = {}
_TEMP_ENTITY_CACHE_TTL = 86400     # 24 ore


@app.get("/room_temperature/{room}")
async def get_room_temperature(room: str, location_id: str = None):
    """
    Endpoint per ottenere la temperatura di una stanza da Home Assistant.

    Ricerca intelligente con caching:
    1. Weather: se in cache (12h TTL), ritorna il valore cachato
    2. Sensor: se entity già risolta (24h TTL), legge SOLO quell'entity (GET singolo, no bulk)
    3. Fallback: bulk fetch per risolvere l'entity (solo alla prima richiesta)

    Args:
        room: nome della stanza (friendly_name dal device)
        location_id: opzionale, filtra per location specifica
    """
    room_lower = room.lower().strip()
    now = time.time()

    # Determina le location da cercare
    if location_id:
        location_ids = [location_id]
    else:
        location_ids = multi_ha.get_location_ids()

    # --- CHECK ENTITY RESOLUTION CACHE ---
    for loc_id in location_ids:
        cache_key = f"{room_lower}:{loc_id}"
        entity_cached = _temp_entity_cache.get(cache_key)
        if not entity_cached or now - entity_cached["timestamp"] >= _TEMP_ENTITY_CACHE_TTL:
            continue

        entity_id = entity_cached["entity_id"]
        source_type = entity_cached["source"]

        # WEATHER: check value cache first (12h TTL)
        if source_type == "weather":
            weather_cached = _temp_weather_cache.get(cache_key)
            if weather_cached and now - weather_cached["timestamp"] < _TEMP_WEATHER_CACHE_TTL:
                logger.debug(f"Temperature weather cache hit for '{room}': {weather_cached['result']['temperature']}")
                return weather_cached["result"]
            # Weather cache expired — re-fetch the single entity
            try:
                state_data = await multi_ha.get_state(loc_id, entity_id)
                if state_data:
                    attrs = state_data.get("attributes", {})
                    weather_temp = attrs.get("temperature")
                    if weather_temp is not None:
                        result = {
                            "temperature": float(weather_temp),
                            "unit": attrs.get("temperature_unit", "°C"),
                            "entity_id": entity_id,
                            "source": "weather",
                            "location_id": loc_id
                        }
                        _temp_weather_cache[cache_key] = {"result": result, "timestamp": now}
                        logger.debug(f"Temperature for '{room}': {result['temperature']}°C from weather {entity_id}")
                        return result
            except Exception as e:
                logger.debug(f"Weather entity fetch failed for {entity_id}: {e}")
                _temp_entity_cache.pop(cache_key, None)

        # SENSOR: always read live value via lightweight single-entity GET
        elif source_type == "sensor":
            try:
                state_data = await multi_ha.get_state(loc_id, entity_id)
                if state_data:
                    state_val = state_data.get("state", "unavailable")
                    if state_val not in ("unavailable", "unknown", ""):
                        attrs = state_data.get("attributes", {})
                        uom = attrs.get("unit_of_measurement", "°C")
                        result = {
                            "temperature": float(state_val),
                            "unit": uom if uom else "°C",
                            "entity_id": entity_id,
                            "source": "sensor",
                            "location_id": loc_id
                        }
                        logger.debug(f"Temperature for '{room}': {result['temperature']}{result['unit']} from sensor {entity_id}")
                        return result
            except Exception as e:
                logger.debug(f"Sensor entity fetch failed for {entity_id}: {e}")
                _temp_entity_cache.pop(cache_key, None)

    # --- FULL SEARCH (bulk fetch — only when entity not yet resolved) ---
    for loc_id in location_ids:
        cache_key = f"{room_lower}:{loc_id}"
        try:
            all_states = await multi_ha.get_states_bulk(loc_id)
            if not all_states:
                continue

            # --- STRATEGIA 1: Cerca sensore temperatura con nome che matcha la stanza ---
            best_match = None
            best_score = 0
            best_entity_id = None

            for entity_id, state_data in all_states.items():
                if not entity_id.startswith("sensor."):
                    continue

                state_val = state_data.get("state", "unavailable")
                if state_val in ("unavailable", "unknown", ""):
                    continue

                attrs = state_data.get("attributes", {})
                uom = attrs.get("unit_of_measurement", "")
                device_class = attrs.get("device_class", "")

                # Deve essere un sensore di temperatura
                if device_class != "temperature" and uom not in ("°C", "°F", "C", "F"):
                    continue

                # Controlla match nel nome entity o friendly_name
                eid_lower = entity_id.lower()
                friendly = attrs.get("friendly_name", "").lower()

                score = 0
                if room_lower in eid_lower:
                    score = 10
                if room_lower in friendly:
                    score = max(score, 8)
                if any(word in eid_lower for word in room_lower.split()):
                    score = max(score, 5)
                if any(word in friendly for word in room_lower.split()):
                    score = max(score, 4)

                if score > best_score:
                    best_score = score
                    best_entity_id = entity_id
                    try:
                        best_match = {
                            "temperature": float(state_val),
                            "unit": uom if uom else "°C",
                            "entity_id": entity_id,
                            "source": "sensor",
                            "location_id": loc_id
                        }
                    except (ValueError, TypeError):
                        pass

            if best_match:
                # Cache entity resolution (24h) — valore letto live ogni volta
                _temp_entity_cache[cache_key] = {
                    "entity_id": best_entity_id,
                    "source": "sensor",
                    "timestamp": now
                }
                logger.info(f"Temperature for '{room}': {best_match['temperature']}{best_match['unit']} from {best_match['entity_id']} (entity resolved, cached 24h)")
                return best_match

            # --- STRATEGIA 2: Fallback su weather entity ---
            for entity_id, state_data in all_states.items():
                if not entity_id.startswith("weather."):
                    continue

                attrs = state_data.get("attributes", {})
                weather_temp = attrs.get("temperature")

                if weather_temp is not None:
                    try:
                        temp_float = float(weather_temp)
                        result = {
                            "temperature": temp_float,
                            "unit": attrs.get("temperature_unit", "°C"),
                            "entity_id": entity_id,
                            "source": "weather",
                            "location_id": loc_id
                        }
                        # Cache entity resolution (24h) + weather value (12h)
                        _temp_entity_cache[cache_key] = {
                            "entity_id": entity_id,
                            "source": "weather",
                            "timestamp": now
                        }
                        _temp_weather_cache[cache_key] = {"result": result, "timestamp": now}
                        logger.info(f"Temperature for '{room}': {temp_float}°C from weather {entity_id} (entity resolved, value cached 12h)")
                        return result
                    except (ValueError, TypeError):
                        continue

        except Exception as e:
            logger.error(f"Error fetching temperature for '{room}' from location '{loc_id}': {e}")
            continue

    logger.warning(f"No temperature found for room '{room}' in any location")
    return {"temperature": None, "error": f"no_sensor_found_for_{room}"}


# ===========================================================================
# WS AUDIO — WebSocket endpoint per ricezione audio Opus
# ===========================================================================

@app.websocket("/ws/audio")
async def ws_audio(websocket: WebSocket):
    """
    Persistent WebSocket for AtomS3R devices: control + audio on single channel.

    Protocol:
      - Device connects and stays connected (persistent)
      - JSON text frames for control: hello, audio_start, state, trigger_listen
      - Binary frames for Opus audio during active speech sessions
      - Server can trigger device listening via trigger_listen command
      - Backward compatible: legacy ephemeral protocol still works

    Auth via query params: ?device_id=XX&token=YY
    """
    device_id = websocket.query_params.get("device_id", "").upper().strip()
    token = websocket.query_params.get("token", "")

    from ws_audio_handler import ws_audio_endpoint
    await ws_audio_endpoint(
        websocket=websocket,
        device_id=device_id,
        token=token,
        on_speech_complete=_process_ws_audio,
    )


async def _process_ws_audio(device_id: str, audio_bytes: bytes):
    """
    Callback da WsAudioSession a fine speech.
    Stessa pipeline di /voice_stream: device config → denoise → STT →
    restore speaker → speaker ID → pre-route → dispatch.

    audio_bytes: PCM 16-bit mono 16 kHz
    """
    logger.info(f"WS speech received from {device_id}: {len(audio_bytes)} bytes")

    # ── PENDING LIVE SESSION (speaker verification) ──────────────────────
    if device_id in _pending_live_sessions:
        logger.info(f"🎙️ Pending live session for {device_id} — handling verification response")
        await _handle_pending_live_session(device_id, audio_bytes)
        return

    # ── LIVE SESSION BYPASS ──────────────────────────────────────────────
    # If device is in a live session, skip speaker ID, routing, normalize.
    # Only do: audio normalize → STT → deactivation check → OpenClaw
    if device_id in _live_sessions:
        logger.info(f"🎙️ Live session active for {device_id} — using simplified pipeline")
        await handle_live_session_turn(device_id, audio_bytes)
        return

    # Recupera configurazione device
    device_config = get_device_speaker_config(device_id)
    if device_config:
        location = device_config.get("location_id", get_default_location_id())
        room_value = device_config.get("friendly_name", "Unknown")
        logger.info(f"WS device {device_id} configured as '{room_value}' in '{location}'")
    else:
        location = extract_location_from_device(device_id) or get_default_location_id()
        room_value = "Unknown"
        logger.warning(f"WS device {device_id} not configured, using legacy mode")

    try:
        # Normalizzazione audio (il mic ES8311 ha output basso ~RMS 0.02)
        import numpy as _np2
        _raw = _np2.frombuffer(audio_bytes, dtype=_np2.int16).astype(_np2.float32)
        _peak = _np2.max(_np2.abs(_raw))
        if _peak > 0:
            _target_peak = 28000.0  # ~85% di 32768, lascia headroom
            _gain = min(_target_peak / _peak, 20.0)  # max 20x gain (~26dB)
            _raw = (_raw * _gain).clip(-32768, 32767)
            logger.info(f"Audio normalized: peak={_peak:.0f} gain={_gain:.1f}x ({20*_np2.log10(_gain):.1f}dB)")
        clean_audio = _raw.astype(_np2.int16).tobytes()

        # ── Auto-enrollment: cattura campioni se un utente ha enrollment in corso ──
        # Se enrollment attivo → cattura campione e SKIPPA il routing (non è un comando)
        try:
            from voice_recognition import voice_recognizer
            import asyncio as _aio_enroll
            active_enrollments = voice_recognizer.get_all_active_enrollments()
            if active_enrollments:
                _enroll_loop = _aio_enroll.get_running_loop()
                for enroll_uid in active_enrollments:
                    # Esegui in thread pool (CPU-bound: resemblyzer embedding)
                    result = await _enroll_loop.run_in_executor(
                        None,
                        voice_recognizer.quick_enroll_from_session,
                        enroll_uid, clean_audio, 16000
                    )
                    if result.get("status") == "sample_added":
                        logger.info(
                            f"🎤 Auto-enrollment: user {enroll_uid} sample "
                            f"{result['samples_collected']}/{result['samples_required']} "
                            f"({result.get('duration', '?')}s) from {device_id}"
                        )
                    elif result.get("auto_completed"):
                        logger.info(
                            f"🎤 Auto-enrollment COMPLETE: user {enroll_uid} "
                            f"({result['samples_collected']} samples) from {device_id}"
                        )
                    elif result.get("status") == "skipped":
                        logger.debug(f"Auto-enrollment: skipped short audio for user {enroll_uid}")
                # Enrollment attivo → audio catturato, skippa routing normale
                logger.info(f"🎤 Enrollment mode active — skipping normal voice routing for {device_id}")
                return
        except Exception as e:
            logger.error(f"Auto-enrollment error: {e}")

        # ── STT + Speaker ID + Restore in PARALLELO ──────────────────────
        # Speaker ID (resemblyzer) è CPU-bound (~seconds), lo eseguiamo
        # in un thread pool per non bloccare l'event loop, in parallelo con STT.
        import asyncio as _aio
        loop = _aio.get_running_loop()

        stt_start = time.time()

        # Lancia STT, Speaker ID e Restore concorrentemente
        stt_task = _aio.ensure_future(transcribe_audio(clean_audio))
        speaker_task = loop.run_in_executor(
            None,  # default ThreadPoolExecutor
            build_speaker_context, audio_bytes, "AtomS3R"
        )
        restore_task = _aio.ensure_future(restore_speaker(device_id))

        # Attendi STT (critica per procedere)
        text = await stt_task
        admin_metrics.record_stt((time.time() - stt_start) * 1000)

        # Gestisci restore (non bloccante)
        try:
            restore_result = await restore_task
            if restore_result.get("status") == "restored":
                logger.info(f"Auto-restore volume per {device_id}: "
                            f"{restore_result.get('original_volume', '?')}")
        except Exception as e:
            logger.error(f"Auto-restore fallito per {device_id}: {e}")

        if not text:
            logger.info(f"WS: no speech detected from {device_id}")
            # Cancella speaker task se non serve
            speaker_task.cancel()
            return

        logger.info(f"WS transcribed: '{text[:120]}...' from {device_id}")

        # ── STT Normalization (Qwen corregge errori di trascrizione) ──
        norm_start = time.time()
        text = await normalize_stt_text(text)
        norm_ms = (time.time() - norm_start) * 1000
        if norm_ms > 50:  # log solo se significativo
            logger.info(f"STT normalize: {norm_ms:.0f}ms")

        # Attendi Speaker ID (potrebbe essere già finito se STT era lenta)
        speaker_start = time.time()
        speaker_ctx = await speaker_task
        speaker_elapsed = (time.time() - stt_start) * 1000  # tempo totale dall'inizio
        admin_metrics.record_speaker_id(speaker_elapsed)

        context = {
            "source": "AtomS3R",
            "room": room_value,
            "mic_id": device_id,
            "device_id": device_id,
            "location": location,
            "device_config": device_config,
            **speaker_ctx,
        }

        # Aggiorna user location
        if speaker_ctx.get("speaker_id"):
            set_user_location(speaker_ctx["speaker_id"], location, "voice")

        # ── LIVE SESSION ACTIVATION CHECK (fast-path keyword) ────────────
        # Quick keyword check BEFORE Qwen to save ~100ms latency.
        # Qwen also classifies SESSIONE_LIVE as fallback if keywords miss.
        if _is_live_session_activation(text):
            logger.info(f"🎙️ Live session activation (keyword match) from {device_id}: '{text}'")
            await _activate_live_session(device_id, device_config, speaker_ctx, location, room_value)
            return

        # Pre-routing via Qwen 7B
        pre_route_start = time.time()
        pre_result = await pre_route(text)
        pre_route_ms = (time.time() - pre_route_start) * 1000
        classification = pre_result.get("classification", "ALTRO")
        logger.info(f"WS pre-route: {classification} (conf={pre_result.get('confidence', 0):.2f}, {pre_route_ms:.0f}ms)")

        if classification == "SESSIONE_LIVE":
            logger.info(f"🎙️ Live session activation (Qwen) from {device_id}: '{text}'")
            await _activate_live_session(device_id, device_config, speaker_ctx, location, room_value)
        elif classification == "DOMOTICA_CERTA":
            await process_jarvis_logic(text, context)
        elif classification == "DOMOTICA_INCERTA":
            await _handle_openclaw_voice(text, context, hint="domotics")
        else:
            await _handle_openclaw_voice(text, context, hint="")

    except Exception as e:
        logger.error(f"Error processing WS audio from {device_id}: {e}")


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

    # Virtual Microphone: request_id per tracciare la risposta
    vmic_request_id = request.headers.get("X-Request-ID")

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
    is_virtual_mic = device_id_value == "VIRTUALMICBROWSER"

    if is_virtual_mic:
        # Virtual Microphone: build config from request headers
        vmic_speaker = request.headers.get("X-Output-Speaker", "")
        vmic_location = request.headers.get("X-Location", "")
        if vmic_speaker and vmic_location:
            device_config = {
                "location_id": vmic_location,
                "friendly_name": room_value or "VirtualMic",
                "output_speaker": vmic_speaker,
                "fallback_speaker": None,
                "fallback_telegram": True,
                "fallback_local_speaker": False,
            }
    elif device_id_value:
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

        # ── AUTO-RESTORE speaker volume dopo STT ──
        # Il firmware ha chiamato /speaker/suppress alla wake word,
        # ora che la registrazione è finita, ripristiniamo il volume
        # prima di qualsiasi altra operazione (routing, TTS, ecc.)
        if device_id_value and not is_virtual_mic:
            try:
                restore_result = await restore_speaker(device_id_value)
                if restore_result.get("status") == "restored":
                    logger.info(f"🔊 Auto-restore volume per {device_id_value}: "
                                f"{restore_result.get('original_volume', '?')}")
            except Exception as e:
                logger.error(f"Auto-restore fallito per {device_id_value}: {e}")

        if not text:
            return {"status": "no_speech_detected", "use_local_speaker": False}

        logger.info(f"Transcribed from stream: '{text[:120]}...'")

        # ── STT Normalization ──
        text = await normalize_stt_text(text)

        # Speaker identification
        speaker_start = time.time()
        speaker_ctx = build_speaker_context(audio_bytes, "AtomS3R")
        admin_metrics.record_speaker_id((time.time() - speaker_start) * 1000)

        context = {
            "source": "VirtualMic" if is_virtual_mic else "AtomS3R",
            "room": room_value,
            "mic_id": mic_id_value or device_id_value or "unknown",
            "device_id": device_id_value or mic_id_value or "unknown",
            "location": location,
            "device_config": device_config,  # Passa la config per la fallback chain
            **speaker_ctx
        }

        # Virtual Microphone: traccia request_id per la risposta
        if vmic_request_id:
            context["vmic_request_id"] = vmic_request_id
            context["_vmic_start_time"] = time.time()

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
            "transcribed_text": text,
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
# MULTI-TURN FOLLOW-UP DETECTION
# ===========================================================================

def _needs_followup(response_text: str) -> bool:
    """
    Check if the response warrants a follow-up listen trigger.

    A response needs follow-up if it ends with a question mark,
    indicating OpenClaw is asking the user something (multi-turn).

    Simple regex is the right choice: OpenClaw produces well-punctuated Italian,
    and false positives (rhetorical questions) are harmless (device just times out).

    Strips markdown artifacts (**, *, _) and whitespace before checking,
    so "vuoi sapere altro?**" or "dimmi pure? " still match.
    """
    if not response_text:
        return False
    import re
    cleaned = re.sub(r'[\s*_`#]+$', '', response_text)
    return cleaned.endswith('?')


# ===========================================================================
# OPENCLAW VOICE HANDLER (for DOMOTICA_INCERTA and ALTRO pre-routes)
# ===========================================================================

async def _handle_openclaw_voice(text: str, context: dict, hint: str = ""):
    """
    Handle voice commands that need OpenClaw (non-certain domotics or general queries).

    For AtomS3R/VirtualMic sources: uses streaming TTS — sentences are spoken as they
    arrive from OpenClaw SSE, without waiting for the full response. This dramatically
    reduces perceived latency.

    For other sources (Telegram, etc.): waits for full response, then delivers.

    Multi-turn: uses OpenClaw 'user' parameter for stable session routing.
    If the speaker is identified, session key = "speaker-{speaker_id}".
    Otherwise, session key = "device-{device_id}" to group anonymous interactions per device.
    """
    context["_user_text"] = text  # Per VirtualMic response tracking
    context["_intent"] = f"OPENCLAW:{hint}" if hint else "OPENCLAW"

    # Build stable session user key for OpenClaw multi-turn
    speaker_id = context.get("speaker_id")
    device_cfg = context.get("device_config")
    room = (device_cfg.get("room") if device_cfg else None) or context.get("room", "")
    if speaker_id:
        session_user = f"speaker-{speaker_id}"
    elif room:
        session_user = f"device-{room.lower()}"
    else:
        session_user = f"source-{context.get('source', 'unknown')}"
    logger.info(f"OpenClaw multi-turn session_user={session_user}")

    save_chat_message("user", text, context.get("source", "AtomS3R"),
                      context.get("speaker_id"), context.get("speaker_name", "Sconosciuto"))

    source = context.get("source", "")
    location = context.get("location", get_default_location_id())

    # ── Check DND / silent hours — if active, skip streaming TTS and send to Telegram ──
    dnd_mode = get_global_preference("dnd_mode", "False") == "True"
    s_start = int(get_global_preference("silent_hour_start", str(config.SILENT_START)))
    s_end = int(get_global_preference("silent_hour_end", str(config.SILENT_END)))
    now_h = datetime.now().hour
    if s_start > s_end:
        is_silent_time = (now_h >= s_start or now_h < s_end)
    else:
        is_silent_time = (s_start <= now_h < s_end)

    # Resolve internal speaker early — bypasses DND/silent hours
    # (lo speaker interno non disturba nessuno, risponde solo a chi ha premuto il bottone)
    device_cfg = context.get("device_config")
    use_internal_speaker = device_cfg.get("use_internal_speaker", False) if device_cfg else False

    use_streaming_tts = (
        source in ("AtomS3R", "VirtualMic")
        and (use_internal_speaker or (not dnd_mode and not is_silent_time))
    )

    if use_streaming_tts:
        # ── Streaming TTS path ──
        # Resolve target speaker for TTS delivery
        if device_cfg:
            target_speaker = device_cfg.get("output_speaker") if not use_internal_speaker else None
            loc = device_cfg.get("location_id", location)
        else:
            loc = location
            room_speakers_map = get_room_speakers(loc)
            target_speaker = room_speakers_map.get(context.get("room", ""), None)

        if not target_speaker and not use_internal_speaker:
            # Can't stream without a speaker — fallback to non-streaming
            logger.warning("Streaming TTS: no target speaker found, falling back to non-streaming")
            use_streaming_tts = False

    if use_streaming_tts:
        if not use_internal_speaker:
            # Feedback audio immediato: suono "thinking" (solo per speaker HA)
            asyncio.create_task(play_feedback_sound("neutral", target_speaker, loc))

        # Reset chunk counter per questa sessione
        _flush_tts_sentences.chunks_sent = 0

        # Accumula durata TTS per internal speaker (per post-TTS timing)
        _internal_tts_total_duration = 0.0

        # Build streaming TTS callback
        if use_internal_speaker:
            from internal_tts import speak_to_device
            _internal_device_id = context.get("device_id", "unknown")

            async def _stream_tts_chunk(chunk: str, is_first: bool):
                """Deliver a sentence chunk to device internal speaker via Opus streaming."""
                nonlocal _internal_tts_total_duration
                logger.info(f"🔊 Internal TTS chunk ({len(chunk)} chars, first={is_first}): {chunk[:80]}...")
                try:
                    success, duration = await speak_to_device(chunk, _internal_device_id)
                    if success:
                        _internal_tts_total_duration += duration
                    else:
                        logger.error(f"Internal TTS chunk delivery failed for {_internal_device_id}")
                except Exception as e:
                    logger.error(f"Internal TTS chunk delivery error: {e}")
        else:
            async def _stream_tts_chunk(chunk: str, is_first: bool):
                """Deliver a sentence chunk to TTS speaker immediately."""
                logger.info(f"🔊 TTS stream chunk ({len(chunk)} chars, first={is_first}): {chunk[:80]}...")
                try:
                    # No intro sound on chunks — the "thinking" beep was already played above
                    await speak(chunk, target_speaker, loc)
                except Exception as e:
                    logger.error(f"TTS stream chunk delivery error: {e}")

        # Forward to OpenClaw with streaming callback + multi-turn session
        response, response_id = await forward_to_openclaw(
            text, context, hint=hint,
            stream_tts_callback=_stream_tts_chunk,
            session_user=session_user
        )
        if response_id:
            logger.debug(f"OpenClaw response_id={response_id[:30]}... for session={session_user}")

        # Log full response text for debugging
        if response:
            logger.info(f"📝 AI response ({len(response)} chars): {response[:500]}{'...' if len(response) > 500 else ''}")

        # Post-streaming: save chat, update speaking state, VirtualMic tracking
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")

        # Speaking state + post-TTS handler
        room = context.get("room", "salotto").lower()
        device_id = context.get("device_id", "unknown")
        is_multi_turn = _needs_followup(response) and source == "AtomS3R"

        if use_internal_speaker and device_id and device_id != "unknown":
            # Internal speaker: niente polling HA, gestione diretta
            # speak_to_device() invia frame Opus in modo sincrono — il device
            # riproduce man mano. Dopo l'ultimo frame serve solo un piccolo
            # delay per il flush del buffer DMA (~150ms).
            await asyncio.sleep(0.15)

            if is_multi_turn:
                success = await trigger_device_listen(device_id, silent=True)
                if success:
                    logger.info(f"🔄 Multi-turn (internal speaker): triggered listen on {device_id} "
                                f"(after {_internal_tts_total_duration:.1f}s TTS)")
            else:
                from ws_audio_handler import notify_tts_done
                await notify_tts_done(device_id)
                logger.info(f"🔊 Internal TTS done for {device_id} ({_internal_tts_total_duration:.1f}s)")
        elif target_speaker and not target_speaker.startswith("telegram:"):
            await set_speaking_state(room, True, device_id)
            # Schedule polling-based post-TTS handler (replaces estimate-based approach)
            schedule_post_tts(
                media_player_id=target_speaker,
                location_id=loc,
                room=room,
                device_id=device_id,
                is_multi_turn=is_multi_turn,
                text_length=len(response) if response else 0,
            )
            if is_multi_turn:
                logger.info(f"🔄 Multi-turn: waiting for TTS on {target_speaker} "
                            f"(device={device_id}, {len(response)} chars)")

        # VirtualMic response tracking
        vmic_req_id = context.get("vmic_request_id")
        if vmic_req_id:
            duration_ms = int((time.time() - context.get("_vmic_start_time", time.time())) * 1000)
            vmic_data = {
                "request_id": vmic_req_id,
                "response": response,
                "speaker_name": context.get("speaker_name", ""),
                "speaker_target": target_speaker or "",
                "intent": context.get("_intent", ""),
                "duration_ms": duration_ms,
                "user_text": context.get("_user_text", ""),
            }
            _vmic_responses[vmic_req_id] = vmic_data
            async def _cleanup_vmic(rid):
                await asyncio.sleep(60)
                _vmic_responses.pop(rid, None)
            asyncio.create_task(_cleanup_vmic(vmic_req_id))
            try:
                from event_bus import event_bus
                await event_bus.publish("voice_response", vmic_data)
            except Exception:
                pass

        # If no chunks were sent (e.g. very short response or error), deliver full response
        if _flush_tts_sentences.chunks_sent == 0 and response:
            logger.info("TTS stream: no chunks sent during streaming, delivering full response")
            await deliver_final_response(response, context, sound_type="neutral")

    else:
        # ── Non-streaming path (Telegram, DND, silent hours, no speaker) ──
        # Feedback audio immediato (solo se voice source con speaker)
        if source in ("AtomS3R", "VirtualMic"):
            device_cfg = context.get("device_config")
            if device_cfg:
                fb_speaker = device_cfg.get("output_speaker")
                fb_loc = device_cfg.get("location_id", location)
            else:
                fb_loc = location
                room_speakers_map = get_room_speakers(fb_loc)
                fb_speaker = room_speakers_map.get(context.get("room", ""), None)
            if fb_speaker:
                asyncio.create_task(play_feedback_sound("neutral", fb_speaker, fb_loc))

        response, _ = await forward_to_openclaw(text, context, hint=hint, session_user=session_user)

        # Log full response text for debugging
        if response:
            logger.info(f"📝 AI response ({len(response)} chars): {response[:500]}{'...' if len(response) > 500 else ''}")

        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        await deliver_final_response(response, context, sound_type="neutral")

# ===========================================================================
# ENTITY RESOLUTION FOR VOICE (single entity, room, zone, floor, all)
# ===========================================================================

def _extract_target_from_user_text(user_text: str, location_id: str) -> Optional[str]:
    """
    Estrae il target (stanza, zona, piano, "tutto") dal testo originale dell'utente,
    confrontandolo con le room/zone/area reali nel database.

    Approccio: carica tutti i nomi reali dal DB e cerca quale appare nel testo utente.
    Questo è molto più affidabile di fidarsi dell'entity name che Qwen restituisce.

    Returns:
        Il nome del target trovato (es. "Cucina", "Zona Giorno") oppure None.
    """
    from database import get_entity_map_locations

    text_lower = re.sub(r'[,\.\!\?\;\:\-]', ' ', user_text.strip().lower())
    text_lower = re.sub(r'\s+', ' ', text_lower).strip()

    # Check wildcard prima ("tutto", "tutte", "tutti", "ovunque")
    wildcard_tokens = {"tutto", "tutti", "tutte", "tutta la casa", "ovunque", "dappertutto"}
    for wt in wildcard_tokens:
        if wt in text_lower:
            return wt

    # Carica room/zone/area reali dal DB
    locations = get_entity_map_locations(location_id)
    if not locations:
        return None

    # Ordina per lunghezza decrescente (match più specifico prima)
    # es. "Zona Giorno" deve matchare prima di "Giorno"
    all_names = sorted(locations, key=len, reverse=True)

    for name in all_names:
        if name.lower() in text_lower:
            return name

    return None


def _resolve_home_control_target(
    location_id: str, domain: str, entity_name: str,
    room_hint: str = None, user_text: str = None
) -> dict:
    """
    Risolve il target di un comando HOME_CONTROL voice.

    Strategia a cascata (con tripla fonte):
    A. TESTO UTENTE: estrae room/zona/piano dal testo originale (più affidabile)
    B. ENTITY QWEN: prova match esatto del friendly name che Qwen restituisce
    C. ROOM HINT: usa la stanza del microfono come ultimo resort

    In tutti i casi, non verifica stato on/off — esegue direttamente l'azione.

    Returns:
        {
            "mode": "single" | "bulk",
            "entity_ids": [str],
            "description": str,
            "match_type": str,
        }
    """
    from database import resolve_entity_id, discover_entities_for_voice

    def _make_bulk_result(discovered_list, source_label):
        entity_ids = [e["entity_id"] for e in discovered_list]
        match_type = discovered_list[0]["match_type"]
        rooms = sorted(set(e["room"] for e in discovered_list if e.get("room")))
        rooms_str = ", ".join(rooms) if rooms else source_label
        logger.info(
            f"Entity resolution [{source_label}]: {match_type} match → "
            f"{len(entity_ids)} {domain} entities in [{rooms_str}]"
        )
        return {
            "mode": "bulk",
            "entity_ids": entity_ids,
            "description": f"{len(entity_ids)} {domain} in {rooms_str}",
            "match_type": match_type,
        }

    def _make_clarify_result(discovered_list, source_label):
        """Ambiguous match: multiple entities, ask user to clarify."""
        entity_ids = [e["entity_id"] for e in discovered_list]
        names = [e.get("entity_name") or e.get("friendly_name", "?") for e in discovered_list[:8]]
        rooms = sorted(set(e["room"] for e in discovered_list if e.get("room")))
        rooms_str = ", ".join(rooms) if rooms else source_label
        logger.info(
            f"Entity resolution [{source_label}]: AMBIGUOUS → "
            f"{len(entity_ids)} {domain} entities in [{rooms_str}], asking clarification"
        )
        return {
            "mode": "clarify",
            "entity_ids": entity_ids,
            "entity_names": names,
            "description": f"ambiguous in {rooms_str}",
            "match_type": "ambiguous",
        }

    # ── A. TESTO UTENTE: estrai target direttamente dal testo originale ──
    if user_text:
        extracted = _extract_target_from_user_text(user_text, location_id)
        if extracted:
            discovered = discover_entities_for_voice(location_id, extracted, domain=domain)
            if discovered:
                return _make_bulk_result(discovered, f"user_text:'{extracted}'")

    # ── B. ENTITY QWEN: prova quello che Qwen ha restituito ──
    # B1. Match esatto friendly name → entity singola
    exact_id = resolve_entity_id(
        location_id=location_id,
        friendly_name=entity_name,
        entity_type=domain,
        room=room_hint
    )
    if exact_id:
        logger.info(f"Entity resolution [qwen_exact]: '{entity_name}' → {exact_id}")
        return {
            "mode": "single",
            "entity_ids": [exact_id],
            "description": entity_name,
            "match_type": "exact",
        }

    # B2. Prova entity_name come target di discovery (room/zona/piano)
    discovered = discover_entities_for_voice(location_id, entity_name, domain=domain)
    if discovered:
        if len(discovered) == 1:
            return _make_bulk_result(discovered, f"qwen_entity:'{entity_name}'")
        # Multiple entities: ambiguous → ask clarification
        return _make_clarify_result(discovered, f"qwen_entity:'{entity_name}'")

    # B3. Estrai parole non-generiche dall'entity_name di Qwen
    if " " in entity_name:
        generic_words = {
            "luce", "luci", "lampada", "lampade", "led", "la", "le", "il", "i",
            "del", "della", "dei", "delle", "di", "in", "box",
            "tapparella", "tapparelle", "tenda", "tende",
            "sensore", "sensori", "interruttore", "interruttori",
            "presa", "prese", "switch", "tutte", "tutti", "tutto",
        }
        # Pulisci punteggiatura e split
        cleaned = re.sub(r'[,\.\!\?\;\:\-]', ' ', entity_name)
        words = [w.strip() for w in cleaned.split() if w.strip()]
        for word in words:
            if word.lower() in generic_words or len(word) < 3:
                continue
            discovered = discover_entities_for_voice(location_id, word, domain=domain)
            if discovered:
                if len(discovered) == 1:
                    return _make_bulk_result(discovered, f"qwen_word:'{word}'")
                # Multiple entities: ambiguous → ask clarification
                return _make_clarify_result(discovered, f"qwen_word:'{word}'")

    # ── C. ROOM HINT: stanza del microfono come ultimo resort ──
    if room_hint and room_hint.lower() not in ("unknown", "sconosciuto"):
        discovered = discover_entities_for_voice(location_id, room_hint, domain=domain)
        if discovered:
            if len(discovered) == 1:
                return _make_bulk_result(discovered, f"room_hint:'{room_hint}'")
            # Multiple entities in room: ambiguous → ask clarification
            return _make_clarify_result(discovered, f"room_hint:'{room_hint}'")

    # ── D. FALLBACK SINTETICO (backwards compatible) ──
    fallback_id = f"{domain}.{entity_name.lower().replace(' ', '_')}"
    logger.warning(f"Entity resolution: no match for '{entity_name}' (user_text='{user_text}'), fallback → {fallback_id}")
    return {
        "mode": "single",
        "entity_ids": [fallback_id],
        "description": entity_name,
        "match_type": "fallback",
    }


# ===========================================================================
# ENTITY QUERY HELPER (for SIMPLE_CHAT multi-turn with api_call)
# ===========================================================================

async def _execute_entity_query(payload: dict, location: str, context: dict) -> str | None:
    """
    Execute entity_discover/entity_bulk query from router payload.
    Returns a human-readable summary string, or None on failure.
    """
    try:
        from database import _get_conn

        params = payload.get("params", {})
        target_location = params.get("location_id") or location or get_default_location_id()

        # Build DB query to find matching entities
        conn = _get_conn()
        c = conn.cursor()
        query = """
            SELECT entity_id, entity_name, entity_type, room, device_name, area, zone
            FROM entity_maps
            WHERE location_id = ? AND entity_id IS NOT NULL
        """
        q_params: list = [target_location]

        if params.get("domain"):
            query += " AND entity_type = ?"
            q_params.append(params["domain"])

        if params.get("room"):
            query += " AND LOWER(room) LIKE LOWER(?)"
            q_params.append(f"%{params['room']}%")

        if params.get("zone"):
            query += " AND LOWER(area) LIKE LOWER(?)"
            q_params.append(f"%{params['zone']}%")

        if params.get("floor"):
            query += " AND LOWER(zone) LIKE LOWER(?)"
            q_params.append(f"%{params['floor']}%")

        if params.get("search"):
            query += " AND (LOWER(entity_name) LIKE LOWER(?) OR LOWER(entity_id) LIKE LOWER(?))"
            q_params.append(f"%{params['search']}%")
            q_params.append(f"%{params['search']}%")

        query += " ORDER BY room, entity_type, entity_name LIMIT 200"
        c.execute(query, q_params)
        rows = c.fetchall()
        conn.close()

        if not rows:
            return "Non ho trovato dispositivi corrispondenti."

        # Collect entity_ids and fetch live states from HA
        discovered = []
        for row in rows:
            discovered.append({
                "entity_id": row["entity_id"],
                "friendly_name": row["entity_name"],
                "domain": row["entity_type"],
                "room": row["room"],
            })

        entity_ids = [e["entity_id"] for e in discovered]
        states = await multi_ha.get_states_bulk(target_location, entity_ids)

        # Build human-readable summary
        domain = params.get("domain", "entity")
        domain_label = {
            "light": "luci", "cover": "tapparelle", "climate": "termostati",
            "media_player": "media player", "sensor": "sensori",
            "binary_sensor": "sensori binari", "switch": "switch",
            "fan": "ventilatori", "vacuum": "aspirapolvere",
        }.get(domain, domain)

        scope = params.get("room") or params.get("zone") or params.get("floor") or "tutta la casa"

        if domain in ("sensor", "binary_sensor"):
            parts = []
            for ent in discovered:
                live = states.get(ent["entity_id"], {})
                attrs = live.get("attributes", {})
                unit = attrs.get("unit_of_measurement", "")
                state = live.get("state", "sconosciuto")
                val = f"{ent['friendly_name']}: {state}{' ' + unit if unit else ''}"
                parts.append(val)
            return f"Ho trovato {len(discovered)} {domain_label} in {scope}: " + ", ".join(parts[:15])

        on_states = {"on", "heat", "cool", "auto", "open", "playing"}
        on_items = []
        for ent in discovered:
            live = states.get(ent["entity_id"], {})
            if live.get("state", "").lower() in on_states:
                on_items.append(ent["friendly_name"])

        total = len(discovered)
        on_count = len(on_items)

        if on_count == 0:
            return f"Tutte le {total} {domain_label} in {scope} sono spente."
        elif on_count == total:
            return f"Tutte le {total} {domain_label} in {scope} sono accese: {', '.join(on_items[:15])}."
        else:
            return (f"{on_count} {domain_label} accese su {total} in {scope}: "
                    f"{', '.join(on_items[:15])}.")

    except Exception as e:
        logger.error(f"Entity query from SIMPLE_CHAT failed: {e}")
        return None


# ===========================================================================
# CORE LOGIC
# ===========================================================================

async def process_jarvis_logic(text: str, context: dict):
    """Main processing logic per tutti i comandi."""
    context["_user_text"] = text  # Per VirtualMic response tracking
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
    context["_intent"] = intent  # Per VirtualMic response tracking

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
        domain_raw = payload.get("domain", "light")
        action = payload.get("action", "toggle")
        entity_raw = payload.get("entity", "unknown")

        # Normalizza domain — Qwen può restituire "light|switch|media_player" o lista
        VALID_DOMAINS = {"light", "switch", "cover", "climate", "lock", "fan",
                         "media_player", "sensor", "binary_sensor", "camera",
                         "automation", "scene", "script", "input_boolean"}
        if isinstance(domain_raw, list):
            domain = domain_raw[0] if domain_raw else "light"
        else:
            domain = str(domain_raw).strip().lower()
        # Se contiene pipe o non è un dominio valido HA → None (discovery trova tutto)
        if "|" in domain or domain not in VALID_DOMAINS:
            logger.info(f"Domain '{domain_raw}' non valido o multi-domain, usando discovery senza filtro dominio")
            domain = None

        # Normalizza entity — Qwen può restituire lista o stringa con pipe
        if isinstance(entity_raw, list):
            entity = entity_raw[0] if entity_raw else "unknown"
            logger.info(f"Qwen returned entity as list ({len(entity_raw)} items), using first: '{entity}'")
        elif isinstance(entity_raw, str) and "|" in entity_raw:
            entity = entity_raw.split("|")[0].strip()
            logger.info(f"Qwen returned entity with pipes, using first: '{entity}'")
        else:
            entity = str(entity_raw) if entity_raw else "unknown"

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

        # Entity resolution: singola entity, stanza, zona, piano o "tutto"
        # Passa il testo originale dell'utente per estrazione diretta (più affidabile di entity Qwen)
        room_hint = context.get("room")
        target = _resolve_home_control_target(
            target_location, domain, entity, room_hint, user_text=text
        )

        # ── CLARIFICATION: ambiguous entity → ask user to specify ──
        if target["mode"] == "clarify":
            names_list = ", ".join(target.get("entity_names", [])[:6])
            more = len(target.get("entity_names", [])) - 6
            if more > 0:
                names_list += f" e altri {more}"
            response = f"Non sono sicuro a quale ti riferisci. Ho trovato: {names_list}. Quale intendi?"
            logger.info(f"HOME_CONTROL clarification: {len(target['entity_ids'])} candidates → asking user")
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context)
            # Multi-turn: override post-TTS handler with multi-turn=True for clarification
            if source in ("AtomS3R", "VirtualMic"):
                cl_device_id = context.get("device_id")
                cl_device_cfg = context.get("device_config")
                if cl_device_id and cl_device_cfg:
                    cl_speaker = cl_device_cfg.get("output_speaker")
                    cl_loc = cl_device_cfg.get("location_id", context.get("location"))
                    cl_room = context.get("room", "").lower()
                    if cl_speaker and cl_loc:
                        schedule_post_tts(
                            media_player_id=cl_speaker,
                            location_id=cl_loc,
                            room=cl_room,
                            device_id=cl_device_id,
                            is_multi_turn=True,
                            text_length=len(response) if response else 0,
                        )
                        logger.info(f"🔄 Clarification follow-up: waiting for TTS on {cl_speaker}")
            return

        # L1-L4 security check (domain-level, come entity_bulk)
        source_channel = "voice" if source in ("AtomS3R", "VirtualMic") else source.lower()
        # Per security check usiamo il primo entity_id; domain potrebbe essere None per multi-domain
        sec_domain = (domain or target["entity_ids"][0].split(".")[0]) if target["entity_ids"] else "light"
        allowed, sec_reason, domain_level, channel_max = check_security(
            sec_domain, action, source_channel, entity_id=target["entity_ids"][0]
        )

        if not allowed:
            if domain_level == SecurityLevel.L3_PROTECTED and channel_max < SecurityLevel.L3_PROTECTED:
                # L3 action from L2 channel → send to JARVIS approval bot
                action_id = str(uuid.uuid4())[:8]
                save_action(action_id, {
                    "domain": sec_domain,
                    "action": action,
                    "data": {"entity_id": target["entity_ids"]},
                    "location": target_location
                }, speaker_id)
                await send_telegram_approval(f"Richiesta: {router_data.get('response', action)}", action_id)
                log_event("APPROVAL", f"L3 azione {action_id} proposta: {sec_reason}", speaker_id, speaker_name)
            else:
                # L4 blocked or other denial
                response = f"Azione bloccata per sicurezza: {sec_domain}.{action} non è consentito da {source}."
                log_event("SECURITY", f"BLOCKED: {sec_reason}", speaker_id, speaker_name)
                save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
                await deliver_final_response(response, context, sound_type="negative")
            return
        else:
            hass_start = time.time()

            # Helper: mappa action generico → action specifico per dominio
            def _map_action_for_domain(base_action: str, entity_domain: str) -> str:
                """Converte action generico nell'action corretto per il dominio."""
                if entity_domain == "cover":
                    return {"turn_on": "open_cover", "turn_off": "close_cover",
                            "toggle": "toggle"}.get(base_action, base_action)
                if entity_domain == "lock":
                    return {"turn_on": "unlock", "turn_off": "lock",
                            "toggle": "toggle"}.get(base_action, base_action)
                # light, switch, media_player, fan, etc. → turn_on/turn_off/toggle funzionano
                return base_action

            if target["mode"] == "bulk" and len(target["entity_ids"]) > 1:
                # Raggruppa entity per dominio (dal prefisso entity_id)
                from collections import defaultdict
                domain_groups = defaultdict(list)
                for eid in target["entity_ids"]:
                    eid_domain = eid.split(".")[0] if "." in eid else "light"
                    domain_groups[eid_domain].append(eid)

                # Esegui per ogni gruppo di dominio
                total_ok = 0
                total_fail = 0
                errors = []
                for grp_domain, grp_ids in domain_groups.items():
                    grp_action = _map_action_for_domain(action, grp_domain)
                    grp_success, grp_err = await call_hass_service_bulk(
                        target_location, grp_domain, grp_action, grp_ids
                    )
                    if grp_success:
                        total_ok += len(grp_ids)
                        logger.info(f"[{target_location}] BULK {grp_action} su {len(grp_ids)} {grp_domain} entities OK")
                    else:
                        total_fail += len(grp_ids)
                        errors.append(f"{grp_domain}: {grp_err}")
                        logger.warning(f"[{target_location}] BULK {grp_action} su {grp_domain} FAILED: {grp_err}")

                success = total_ok > 0
                err = "; ".join(errors) if errors else None
                entity_desc = target["description"]
                log_detail = (
                    f"[{target_location}] BULK {action} su {entity_desc} "
                    f"({total_ok} ok, {total_fail} fail, {len(domain_groups)} domains)"
                )
            else:
                # Single entity
                entity_id = target["entity_ids"][0]
                eid_domain = entity_id.split(".")[0] if "." in entity_id else (domain or "light")
                mapped_action = _map_action_for_domain(action, eid_domain)
                service_data = {"entity_id": entity_id}
                success, err = await call_hass_service(target_location, eid_domain, mapped_action, service_data)
                entity_desc = target["description"]
                log_detail = f"[{target_location}] {mapped_action} su {entity_desc} ({entity_id})"

            admin_metrics.record_hass((time.time() - hass_start) * 1000)

            if success:
                action_verb = {
                    "turn_on": "acceso", "turn_off": "spento", "toggle": "cambiato",
                    "open_cover": "aperto", "close_cover": "chiuso", "stop_cover": "fermato",
                }.get(action, action)
                if target["mode"] == "bulk" and len(target["entity_ids"]) > 1:
                    response = f"Fatto! Ho {action_verb} {target['description']}."
                else:
                    response = f"Fatto! {action_verb}: {entity_desc}."
                smart_cache.learn(text, response, intent)
                log_event("HASS", log_detail, speaker_id, speaker_name)
            else:
                response = f"Problema con {entity_desc}: {err}"
                log_event("HARDWARE_ERROR", f"Fallito {log_detail}: {err}", speaker_id, speaker_name)

            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")

            # Quick feedback: suono breve per comandi vocali (AtomS3R + VirtualMic)
            # Solo Telegram riceve TTS completo, o in caso di errore
            quick_feedback_enabled = get_global_preference("ha_quick_feedback", "True") == "True"
            if source in ("AtomS3R", "VirtualMic") and quick_feedback_enabled:
                # VirtualMic: manda testo alla dashboard (no TTS)
                if source == "VirtualMic":
                    vmic_req_id = context.get("vmic_request_id")
                    if vmic_req_id:
                        # Salva risposta per polling dashboard (senza TTS)
                        duration_ms = int((time.time() - context.get("_vmic_start_time", time.time())) * 1000)
                        vmic_data = {
                            "request_id": vmic_req_id,
                            "response": response,
                            "speaker_name": context.get("speaker_name", "Sconosciuto"),
                            "speaker_target": "",
                            "intent": "HOME_CONTROL",
                            "duration_ms": duration_ms,
                            "user_text": context.get("_user_text", text),
                        }
                        _vmic_responses[vmic_req_id] = vmic_data
                        async def _cleanup_vmic_hc(rid):
                            await asyncio.sleep(60)
                            _vmic_responses.pop(rid, None)
                        asyncio.create_task(_cleanup_vmic_hc(vmic_req_id))
                        try:
                            from event_bus import event_bus
                            await event_bus.publish("voice_response", vmic_data)
                        except Exception:
                            pass
                        logger.info(f"VirtualMic HOME_CONTROL response stored for {vmic_req_id} (no TTS)")
                else:
                    # AtomS3R: suono breve dallo speaker della stanza
                    room = context.get("room", "salotto").lower()
                    room_speakers = get_room_speakers(target_location)
                    if room_speakers:
                        target_player = room_speakers.get(room) or next(iter(room_speakers.values()), config.DEFAULT_FALLBACK_SPEAKER)
                    else:
                        target_player = config.DEFAULT_FALLBACK_SPEAKER
                    await quick_feedback(success, target_player, err, target_location)
            else:
                # Telegram o quick feedback disabilitato: risposta TTS completa
                # Solo in caso di errore forza il TTS, altrimenti suono
                if not success:
                    await deliver_final_response(response, context, sound_type="negative")
                else:
                    sound = "positive"
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
            response, _ = await forward_to_openclaw(text, context, hint="gemini_fallback")
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
        response, _ = await forward_to_openclaw(text, context, hint=f"low_confidence_{intent}")
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        await deliver_final_response(response, context, sound_type="neutral")

    # --- SIMPLE CHAT / UNKNOWN ---
    else:
        payload = router_data.get("payload", {})
        api_call = payload.get("api_call") if isinstance(payload, dict) else None

        # Multi-turn: if router indicated an api_call (entity_discover/entity_bulk),
        # execute it to get live data from Home Assistant
        if api_call in ("entity_discover", "entity_bulk"):
            logger.info(f"SIMPLE_CHAT multi-turn: executing {api_call} with params={payload.get('params', {})}")
            query_response = await _execute_entity_query(payload, location, context)
            if query_response:
                response = query_response
            else:
                # Query failed, use router's interim response or quick response
                response = router_data.get("response")
                if not response:
                    response = await get_quick_response(text, context)
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

    # Helper: immediately notify device that response is done (no audio to wait for)
    async def _immediate_tts_done():
        dev_id = context.get("device_id")
        if dev_id and dev_id != "unknown":
            try:
                from ws_audio_handler import notify_tts_done
                await notify_tts_done(dev_id)
            except Exception:
                pass

    if dnd_mode:
        await send_telegram(f"🔕 {text}")
        await _immediate_tts_done()
        return

    if is_silent_time and source == "OpenClaw":
        await send_telegram(f"🌙 {text}")
        await _immediate_tts_done()
        return

    if source == "Telegram":
        await send_telegram(text)
        await _immediate_tts_done()
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
        use_internal = device_config.get("use_internal_speaker", False)

        # 0. Internal speaker: TTS diretto al device
        if use_internal:
            dev_id = context.get("device_id")
            if dev_id and dev_id != "unknown":
                from internal_tts import speak_to_device
                success, duration = await speak_to_device(text, dev_id)
                if success:
                    logger.info(f"Audio delivered to internal speaker: {dev_id} ({duration:.1f}s)")
                    await asyncio.sleep(0.15)  # flush DMA buffer
                    # Multi-turn: se la risposta finisce con ?, riapri ascolto
                    if _needs_followup(text) and source == "AtomS3R":
                        await trigger_device_listen(dev_id, silent=True)
                        logger.info(f"🔄 Multi-turn (internal speaker fallback): triggered listen on {dev_id}")
                    else:
                        from ws_audio_handler import notify_tts_done
                        await notify_tts_done(dev_id)
                    return
                else:
                    logger.error(f"Internal speaker TTS failed for {dev_id}, trying fallbacks")

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

        # Schedule post-TTS handler (estimate-based, future-proofed for polling)
        schedule_post_tts(
            media_player_id=target_player,
            location_id=location,
            room=room,
            device_id=device_id,
            is_multi_turn=False,
            text_length=len(text) if text else 0,
        )

    # Fallback a speaker locale del device
    if use_local_speaker:
        dev_id = context.get("device_id")
        if dev_id and dev_id != "unknown":
            from internal_tts import speak_to_device
            success, duration = await speak_to_device(text, dev_id)
            if success:
                logger.info(f"Fallback to local speaker for {dev_id} ({duration:.1f}s)")
                await asyncio.sleep(0.1)
                from ws_audio_handler import notify_tts_done
                await notify_tts_done(dev_id)
            else:
                logger.error(f"Local speaker fallback also failed for {dev_id}")
        else:
            logger.warning(f"Local speaker fallback triggered but no device_id available")

    # Virtual Microphone: salva risposta e notifica dashboard via SSE
    vmic_req_id = context.get("vmic_request_id")
    if vmic_req_id:
        duration_ms = int((time.time() - context.get("_vmic_start_time", time.time())) * 1000)
        vmic_data = {
            "request_id": vmic_req_id,
            "response": text,
            "speaker_name": speaker_name,
            "speaker_target": target_player or "",
            "intent": context.get("_intent", ""),
            "duration_ms": duration_ms,
            "user_text": context.get("_user_text", ""),
        }
        _vmic_responses[vmic_req_id] = vmic_data
        # Auto-cleanup dopo 60s
        async def _cleanup_vmic(rid):
            await asyncio.sleep(60)
            _vmic_responses.pop(rid, None)
        asyncio.create_task(_cleanup_vmic(vmic_req_id))
        # SSE push
        try:
            from event_bus import event_bus
            await event_bus.publish("voice_response", vmic_data)
        except Exception:
            pass
        logger.info(f"VirtualMic response stored for request {vmic_req_id}")


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


def _find_exec_approval_by_slug(slug: str) -> Optional[str]:
    """Find the full approval ID from pending_exec_approvals by its slug prefix."""
    # Direct match (slug == full id, or slug is first 8 chars)
    if slug in pending_exec_approvals:
        return slug
    for full_id in pending_exec_approvals:
        if full_id.startswith(slug) or full_id[:8] == slug:
            return full_id
    return None


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
