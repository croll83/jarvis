"""
JARVIS Core Orchestrator
- Voice command processing con speaker identification
- Weighted memory context (differenziato per router vs reasoning)
- Intent routing con fallback a AI Agent per comandi avanzati
"""

import asyncio
import time
import re
import uuid
import logging
from datetime import datetime
import zoneinfo
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
    save_chat_message, update_chat_meta, get_recent_turns, format_recent_turns_for_llm,
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
    get_default_location_id,
    # Routing continuity
    save_last_intent, get_last_intent,
)
from integrations import (
    call_hass_service, call_hass_service_bulk, speak, send_telegram, edit_telegram,
    send_telegram_approval, send_exec_approval, denoise_audio, transcribe_audio,
    quick_feedback, speak_with_sound, play_feedback_sound
)
from ai_engines import (
    is_safe, get_routing, get_quick_response, pre_route,
    normalize_stt_text,
    is_ai_agent_intent,
    is_dirty_audio,
    is_image_generation_intent
)
from security import SecurityManager, should_require_approval, get_approval_priority
from security_levels import check_security, needs_approval as needs_l3_approval, SecurityLevel, get_security_summary
from tools_api import router as tools_router
from voice_recognition import voice_recognizer, get_speaker_context
from user_api import router as user_router, web_router
from admin_api import router as admin_router, metrics as admin_metrics
from log_collector import router as log_router, init_log_db, haos_log_puller, log_prune_task
from auth_api import router as auth_router
from device_api import router as device_router, get_device_speaker_config
from speaker_suppress import suppress_speaker, restore_speaker, get_suppressed_speakers
from image_api import router as image_router
from service_status import service_status, ServiceState
from multi_ha import multi_ha
from memory_jobs import memory_scheduler
from location_memory import load_memory_services_from_db
# build_full_context/build_reasoning_context restano in context_builder.py
# per AI Agent reasoning e tools_api — qui non servono più per routing
from proactive import proactive_check_loop
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
    _QUIET_PATHS = ("/device_status", "/heartbeat", "/room_temperature/", "/ws/audio", "/speaker/volume_change", "/api/admin/logs")
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._QUIET_PATHS)

logging.getLogger("uvicorn.access").addFilter(_QuietDevicePollingFilter())

# Silenzia client HTTP verbosi (httpx logga ogni request a INFO — pull HAOS spamma)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Tracking stato "speaking" per room (per notificare voice devices)
# Struttura: {"salotto": {"speaking": True, "started_at": timestamp, "device_id": "..."}}
speaking_state: dict = {}
speaking_state_lock = asyncio.Lock()

# Pending exec approvals from AI Agent (approval_id -> full request data)
pending_exec_approvals: dict = {}

# Strong references to background tasks (prevents GC from destroying them)
_background_tasks: set = set()

# Virtual Microphone response store (request_id -> response data, auto-expires)
_vmic_responses: dict = {}


# ===========================================================================
# AI_AGENT GATEWAY OPERATOR (WebSocket client for exec approvals)
# ===========================================================================
# AI Agent gateway broadcasts exec.approval.requested events to connected
# operator clients. We connect as an operator, receive these events, forward
# them to Telegram with inline buttons, and resolve via exec.approval.resolve.
#
# Gateway WS protocol (JSON-RPC style):
#   Request:  {"type":"req", "id":"<uuid>", "method":"<method>", "params":{...}}
#   Response: {"type":"res", "id":"<uuid>", "ok":true, "payload":{...}}
#   Event:    {"type":"event", "event":"<name>", "payload":{...}}

# Reference WS connection that stays open for resolving approvals
_operator_ws = None

async def ai_agent_operator_loop():
    """
    Connects to AI Agent gateway as a WebSocket operator client.
    Listens for exec.approval.requested events and forwards them
    to Telegram with inline buttons via the JARVIS Approval Bot.
    """
    global _operator_ws
    import websockets
    import json as _json

    ws_url = config.AI_AGENT_WS_URL
    token = config.AI_AGENT_TOKEN

    if not ws_url or not token:
        logger.warning("AI_AGENT_WS_URL or AI_AGENT_TOKEN not set, exec approval operator disabled")
        return

    reconnect_delay = 5

    while True:
        try:
            logger.info(f"Connecting to AI Agent gateway WS: {ws_url}")
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
                    logger.info(f"✅ AI Agent operator WS connected (proto={proto})")
                else:
                    err = hello.get("error", hello)
                    logger.error(f"AI Agent WS handshake failed: {err}")
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
                        logger.error(f"Error processing AI Agent WS message: {e}")

        except asyncio.CancelledError:
            logger.info("AI Agent operator loop cancelled")
            return
        except Exception as e:
            logger.warning(f"AI Agent operator WS disconnected: {e}, reconnecting in {reconnect_delay}s")
            _operator_ws = None
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


async def _handle_exec_approval_event(payload: dict):
    """Handle an exec.approval.requested event from AI Agent gateway."""
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
    Resolve an exec approval via the AI Agent gateway WebSocket.
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


async def _process_telegram_text(text: str, context: dict):
    """Processa un messaggio di testo Telegram attraverso la pipeline JARVIS."""
    try:
        # ── AI_AGENT FOLLOW-UP BYPASS (Telegram) ──────────────────────
        _tg_chat_id = context.get("chat_id", "default")
        _tg_key = f"tg_{_tg_chat_id}"
        if _tg_key in _ai_agent_followup:
            followup = _ai_agent_followup[_tg_key]
            if time.time() - followup["timestamp"] < 300:  # 5 min per Telegram
                logger.info(f"🔄 Telegram AI Agent follow-up active — bypassing routing")
                _fu_session = followup["session_user"]
                response, _ = await forward_to_ai_agent(text, followup["context"], session_user=_fu_session)
                if response:
                    save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
                    await deliver_final_response(response, context, sound_type="neutral")
                    # Continua follow-up o termina
                    if _needs_followup(response):
                        followup["timestamp"] = time.time()
                        logger.info(f"🔄 Telegram follow-up continues for {_tg_key}")
                    else:
                        _ai_agent_followup.pop(_tg_key, None)
                        logger.info(f"🔄 Telegram follow-up ended for {_tg_key}")
                return
            else:
                logger.info(f"🔄 Telegram follow-up expired for {_tg_key}")
                _ai_agent_followup.pop(_tg_key, None)

        if config.SKIP_PRE_ROUTE:
            # Routing unificato: salta pre-route, vai diretto al routing completo
            await process_jarvis_logic(text, context)
        else:
            # Legacy: pre-route 3-way → dispatch
            pre_result = await pre_route(text)
            classification = pre_result.get("classification", "ALTRO")
            logger.info(f"Telegram pre-route: {classification} "
                        f"(conf={pre_result.get('confidence', 0):.2f})")

            if classification == "DOMOTICA_CERTA":
                await process_jarvis_logic(text, context)
            elif classification == "DOMOTICA_INCERTA":
                await _handle_ai_agent_voice(text, context, hint="domotics")
            else:
                await _handle_ai_agent_voice(text, context, hint="")
    except Exception as e:
        logger.error(f"Error processing Telegram text: {e}", exc_info=True)
        await _tg_bot_api("sendMessage", {
            "chat_id": context.get("chat_id"),
            "text": "❌ Errore durante l'elaborazione del messaggio."
        })


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

        # ── Messaggi di testo generici → pipeline JARVIS ──
        elif text_msg and not cmd.startswith("/") and msg_chat_id:
            user_msg = get_user_by_telegram_id(tg_id_msg) if tg_id_msg else None
            if not user_msg:
                await _tg_bot_api("sendMessage", {
                    "chat_id": msg_chat_id,
                    "text": "⚠️ Il tuo account Telegram non è collegato a nessun utente JARVIS."
                })
                return

            user_loc = get_user_location(user_msg.id)
            location = user_loc.location_id if user_loc else get_default_location_id()

            context = {
                "source": "Telegram",
                "chat_id": msg_chat_id,
                "location": location,
                "room": "Unknown",
                "speaker_id": user_msg.id,
                "speaker_name": user_msg.name,
                "is_admin": user_msg.is_admin,
                "telegram_id": tg_id_msg,
            }

            asyncio.create_task(_process_telegram_text(text_msg, context))

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

    # Exec approvals (AI Agent)
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

    # Avvia task periodici (salva riferimenti forti per evitare GC)
    def _keep(t):
        _background_tasks.add(t)
        t.add_done_callback(_background_tasks.discard)

    _keep(asyncio.create_task(periodic_cleanup()))
    _keep(asyncio.create_task(warmup_models()))
    _keep(asyncio.create_task(periodic_health_check()))
    _keep(asyncio.create_task(_warm_semantic_indices()))
    _keep(asyncio.create_task(_semantic_refresh_loop()))

    # Carica memory services e avvia scheduler memoria
    load_memory_services_from_db()
    _keep(asyncio.create_task(memory_scheduler()))
    _keep(asyncio.create_task(proactive_check_loop()))

    # AI Agent gateway operator (exec approval buttons via WS)
    _keep(asyncio.create_task(ai_agent_operator_loop()))

    # Approval Bot: webhook (preferred) or polling fallback
    _keep(asyncio.create_task(approval_bot_setup()))

    # Pre-load Silero VAD model for WS audio reception
    try:
        init_vad()
    except Exception as e:
        logger.error(f"Silero VAD init failed (WS audio disabled): {e}")

    # Log collector
    init_log_db()
    _keep(asyncio.create_task(haos_log_puller()))
    _keep(asyncio.create_task(log_prune_task()))

    # Live session timeout monitor
    _keep(asyncio.create_task(live_session_monitor()))

    logger.info("✅ JARVIS Core ready!")
    yield
    logger.info("👋 JARVIS Core shutting down...")


app = FastAPI(title="Jarvis Core Orchestrator", lifespan=lifespan)

# ===========================================================================
# DEVICE AUTH MIDDLEWARE (Bearer token per voice devices e altri firmware)
# ===========================================================================
# Protegge gli endpoint usati dal firmware. Se DEVICE_API_TOKEN è vuoto,
# l'autenticazione è disabilitata (retrocompatibilità).
DEVICE_AUTH_PATHS = {
    "/voice_command", "/voice_stream", "/device_config", "/device_status", "/heartbeat",
    "/device/config", "/device/heartbeat",
    "/room_temperature", "/speaker/suppress", "/speaker/restore", "/speaker/suppressed",
    "/speaker/volume_change",
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
app.include_router(log_router)


# ===========================================================================
# RECENT TURNS API — Used by ontology-bridge plugin for context injection
# ===========================================================================

@app.get("/api/recent_turns")
async def api_recent_turns(
    max_turns: int = Query(default=3, ge=1, le=10),
    max_seconds: int = Query(default=900, ge=60, le=3600),
):
    """Returns recent conversation turns (for ontology-bridge plugin)."""
    turns = get_recent_turns(max_turns=max_turns, max_seconds=max_seconds)
    return turns


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
        await get_routing("accendi la luce del soggiorno", {"source": "warmup"})
        logger.info("✅ Router model ready")
    except Exception as e:
        logger.warning(f"Model warmup failed: {e}")


async def _warm_semantic_indices():
    """Pre-build the semantic discovery index for every enabled location in the
    background, so the first user query never pays the cold embed cost."""
    try:
        await asyncio.sleep(8)  # let the embedder/DB settle after boot
        import semantic_discovery
        from database import get_all_locations
        for loc in get_all_locations(enabled_only=True):
            await semantic_discovery.warm(loc.id)
    except Exception as e:
        logger.warning(f"Semantic index warm failed: {e}")


async def _semantic_refresh_loop():
    """Hourly background refresh of the semantic index for every enabled location.

    Incremental: re-embeds only entities whose descriptor changed, so a run with
    no entity changes is ~free. Keeps the index in sync with HA without ever
    blocking a user query."""
    while True:
        try:
            await asyncio.sleep(3600)  # ogni ora
            import semantic_discovery
            from database import get_all_locations
            for loc in get_all_locations(enabled_only=True):
                await semantic_discovery.warm(loc.id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Semantic index periodic refresh failed: {e}")


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
# HELPER: Speaking State (per voice devices)
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
# - No Qwen routing (everything goes straight to AI Agent)
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
    ai_agent_session_user: str     # stable AI Agent session key
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

# Lightweight multi-turn AI Agent tracker.
# Quando AI Agent chiede chiarimenti (?), il prossimo voice/telegram input bypassa routing.
# Key: device_id (voice) o "tg_{chat_id}" (Telegram)
_ai_agent_followup: Dict[str, dict] = {}
# Set di device_id per cui trigger_device_listen è stato chiamato per un follow-up.
# Distingue "mic riattivato automaticamente" da "utente ha premuto bottone".
_ai_agent_followup_triggered: set = set()

# Keyword sets for pending session confirmation
_CONFIRM_YES = {"sì", "si", "ok", "vai", "certo", "procedi", "avvia", "yes", "sí"}
_CONFIRM_NO = {"no", "annulla", "lascia", "niente", "stop", "esci"}
_RETRY_ID = {"sono", "riconoscimi", "riconoscere", "prova", "riprova", "identifica", "identificami"}

# Voice session instructions are now in AI Agent SOUL.md.
# The orchestrator sends a structured [VOICE_SESSION] block in the `instructions`
# field of /v1/responses, and SOUL.md defines per-hint behavior rules.

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


def _normalize_words(text: str) -> set:
    """Estrae parole da testo strippando punteggiatura (Whisper aggiunge '....' ecc.)."""
    import re as _re
    return set(_re.sub(r"[^\w\s]", "", text.lower()).split())


def _is_live_session_activation(text: str) -> bool:
    """Check if transcribed text is a live session activation command.

    Matches when text contains an activation verb AND a keyword set.
    E.g.: 'avvia una sessione live' → verb 'avvia' + keywords {'sessione', 'live'} → True
    """
    words = _normalize_words(text)
    has_verb = bool(words & _LIVE_SESSION_ACTIVATE_VERBS)
    has_keywords = any(kw_set <= words for kw_set in _LIVE_SESSION_ACTIVATE_KEYWORDS)
    return has_verb and has_keywords


def _is_live_session_deactivation(text: str) -> bool:
    """Check if transcribed text is a live session deactivation command."""
    words = _normalize_words(text)
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

    # Build stable session key (same logic as _handle_ai_agent_voice).
    # Uses speaker name for identified users, room for anonymous.
    if speaker_name and speaker_name != "Sconosciuto":
        session_user = speaker_name.lower()
    else:
        session_user = room.lower()

    session = LiveSession(
        device_id=device_id,
        user_id=speaker_id,
        speaker_name=speaker_name,
        location_id=location_id,
        room=room,
        media_player_id=media_player_id,
        use_internal_speaker=use_internal_speaker,
        ai_agent_session_user=session_user,
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
    Simplified pipeline: audio normalize → STT → deactivation check → AI Agent → TTS → trigger_listen.
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
    _device_cfg = get_device_speaker_config(device_id)
    _device_type = _device_cfg.get("device_type", "AtomS3R") if _device_cfg else "AtomS3R"
    save_chat_message("user", text, _device_type, session.user_id, session.speaker_name)

    # 5. Forward to AI Agent (no routing, direct)
    context = {
        "source": _device_type,
        "room": session.room,
        "device_id": device_id,
        "location": session.location_id,
        "speaker_id": session.user_id,
        "speaker_name": session.speaker_name,
        "speaker_identified": session.user_id is not None,
        "device_config": _device_cfg,
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
    # We call forward_to_ai_agent with the live session context
    response, response_id = await forward_to_ai_agent(
        text, context, hint="live_session",
        stream_tts_callback=_live_tts_chunk,
        session_user=session.ai_agent_session_user,
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
                _ai_agent_followup_triggered.add(device_id)
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
    1. Prima cerca nel DB voice_devices (path principale per NabuVoice e device configurati)
    2. Fallback: parsing legacy atoms3r_wagmi_salotto -> wagmi
    """
    if not device_id:
        return None

    # Path principale: DB lookup
    device_config = get_device_speaker_config(device_id.upper().strip())
    if device_config and device_config.get("location_id"):
        return device_config["location_id"]

    # Fallback legacy: atoms3r_location_room format
    parts = device_id.lower().split('_')
    if len(parts) >= 2:
        potential_location = parts[1]
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

    if audio_bytes and source in config.VOICE_SOURCES:
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
# AI_AGENT STUB (Phase 4)
# ===========================================================================

async def forward_to_ai_agent(text: str, context: dict, hint: str = "",
                              stream_tts_callback=None,
                              session_user: str = None) -> tuple:
    """
    Forward request to AI Agent via Chat Completions API (POST /v1/chat/completions).

    Uses SSE streaming to avoid hard timeout on long multi-turn conversations.
    As long as AI Agent sends SSE events (deltas), the connection stays alive.
    Timeout triggers only on prolonged silence.

    If stream_tts_callback is provided, sentences are delivered progressively
    to TTS as they complete during streaming (sentence boundaries: .!?\n).

    On timeout or error, falls back to local Qwen quick response.

    Args:
        text: User input text
        context: Request context dict
        hint: Optional hint for AI Agent (e.g. "domotics")
        stream_tts_callback: Optional async callable(chunk: str, is_first: bool) -> None
                             Called for each complete sentence during streaming.
        session_user: Optional stable user identifier for AI Agent multi-turn sessions.
                      When provided, AI Agent maintains conversation history across calls.

    Returns:
        tuple: (response_text: str, response_id: str | None)
    """
    import aiohttp
    import json as _json

    if not config.AI_AGENT_URL:
        logger.warning("AI Agent not configured, falling back to local response")
        return await get_quick_response(text, context), None

    # Build message with context for AI Agent
    speaker_name = context.get("speaker_name", "Sconosciuto")
    speaker_id = context.get("speaker_id")
    speaker_identified = context.get("speaker_identified", False)
    source = context.get("source", "unknown")
    location = context.get("location", "")
    room = context.get("room", "")

    # Build [VOICE_SESSION] structured block for AI Agent system message.
    # SOUL.md on AI Agent defines per-hint behavior (live_session, domotics, etc.)
    # and TTS formatting rules. We only pass the structured context here.
    speaker_payload = _json.dumps({
        "user_id": speaker_id if speaker_id is not None else "None",
        "user_name": speaker_name if speaker_name else "Sconosciuto",
        "identified": speaker_identified,
    }, ensure_ascii=False)

    voice_instructions = (
        f"[VOICE_SESSION]\n"
        f"hint: {hint or 'unknown'}\n"
        f"source: {source or 'unknown'}\n"
        f"speaker: {speaker_payload}\n"
        f"datetime: {datetime.now(zoneinfo.ZoneInfo('Europe/Rome')).strftime('%Y-%m-%d %H:%M %Z')}"
    )

    # Add location/room context if available
    if location or room:
        loc_parts = []
        if location:
            loc_parts.append(f"location: {location}")
        if room:
            loc_parts.append(f"room: {room}")
        voice_instructions += "\n" + "\n".join(loc_parts)

    payload = {
        "model": "hermes-agent",
        "messages": [
            {"role": "system", "content": voice_instructions},
            {"role": "user", "content": text},
        ],
        "stream": True,
    }

    try:
        timeout = aiohttp.ClientTimeout(
            total=config.AI_AGENT_TIMEOUT_TOTAL,   # 300s max totale
            sock_read=config.AI_AGENT_TIMEOUT_READ  # 90s max silenzio tra chunk
        )
        async with aiohttp.ClientSession() as session:
            headers = {"Content-Type": "application/json"}
            if config.AI_AGENT_TOKEN:
                headers["Authorization"] = f"Bearer {config.AI_AGENT_TOKEN}"
            # Multi-turn session via Hermes session header
            if session_user:
                headers["X-Hermes-Session-Id"] = str(session_user)

            async with session.post(
                f"{config.AI_AGENT_URL}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"AI Agent error {resp.status}: {body[:200]}")
                    return await get_quick_response(text, context), None

                # Parse SSE stream (Chat Completions format)
                accumulated_text = ""
                pending_chunk = ""
                chunk_count = 0
                is_first_chunk = True
                chunks_sent = 0

                # Reset chunk counter per streaming TTS
                if stream_tts_callback:
                    _flush_tts_sentences.chunks_sent = 0

                MIN_CHUNK_LEN = 20

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()

                    if line == "data: [DONE]":
                        break

                    if not line.startswith("data: "):
                        continue

                    try:
                        event = _json.loads(line[6:])
                    except _json.JSONDecodeError:
                        continue

                    chunk_count += 1

                    # Chat Completions delta format
                    choices = event.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    finish_reason = choice.get("finish_reason")

                    if content:
                        accumulated_text += content

                        if stream_tts_callback:
                            pending_chunk += content
                            pending_chunk = await _flush_tts_sentences(
                                pending_chunk, stream_tts_callback,
                                is_first_chunk, MIN_CHUNK_LEN,
                                flush_all=False
                            )
                            if chunks_sent != _flush_tts_sentences.chunks_sent:
                                is_first_chunk = False
                                chunks_sent = _flush_tts_sentences.chunks_sent

                    if finish_reason == "stop":
                        break

                # Flush remaining pending chunk
                if stream_tts_callback and pending_chunk.strip():
                    await _flush_tts_sentences(
                        pending_chunk, stream_tts_callback,
                        is_first_chunk, min_len=0,
                        flush_all=True
                    )

                if accumulated_text:
                    logger.info(
                        f"AI Agent response ({len(accumulated_text)} chars, "
                        f"{chunk_count} chunks, "
                        f"tts_chunks={_flush_tts_sentences.chunks_sent if stream_tts_callback else 'n/a'})"
                    )
                    return accumulated_text, None

                logger.warning(f"AI Agent stream ended with no text ({chunk_count} chunks)")

    except asyncio.TimeoutError:
        logger.warning(
            f"AI Agent stream timeout (total={config.AI_AGENT_TIMEOUT_TOTAL}s, "
            f"read={config.AI_AGENT_TIMEOUT_READ}s)"
        )
        return "Mi dispiace, l'operazione sta richiedendo più tempo del previsto. Riprova tra poco.", None
    except aiohttp.ClientConnectorError:
        logger.warning("AI Agent unreachable, falling back to local")
        service_status.set_offline("ai_agent")
    except Exception as e:
        logger.error(f"AI Agent stream error: {e}")

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



# ===========================================================================
# ENDPOINTS
# ===========================================================================

@app.post("/voice_command")
async def voice_command(request: Request):
    """Riceve audio raw dai voice devices, processa e risponde."""
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
    Endpoint per voice devices per sapere se JARVIS sta parlando.
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
    Endpoint per voice devices per notificare il proprio stato DND.
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
    Endpoint chiamato dal voice device appena rileva la wake word.
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


@app.post("/speaker/volume_change")
async def speaker_volume_change_endpoint(request: Request):
    """
    Endpoint per volume change da voice device (rotary encoder).
    Proxied dal wakeword-server via Tailscale.  Debounced server-side.
    Body: {"device_id": "AABBCCDDEEFF", "direction": "up|down"}
    """
    from ws_audio_handler import _handle_volume_change

    data = await request.json()
    device_id = data.get("device_id", "").upper().strip()
    direction = data.get("direction", "up").lower().strip()

    if not device_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "device_id required"})

    if direction not in ("up", "down"):
        return JSONResponse(status_code=400, content={"status": "error", "message": "direction must be 'up' or 'down'"})

    try:
        await _handle_volume_change(device_id, direction)
        return {"status": "ok", "device_id": device_id, "direction": direction}
    except Exception as e:
        logger.error(f"volume_change endpoint failed for {device_id}: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


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

# Room-temperature sensors WHITELIST:
# /room_temperature reads HA's raw /api/states (NOT the entity map), so it sees
# ALL device_class=temperature entities — meters (~55°C), heat-pump probes, pool,
# setpoints, outdoor. Guessing what to EXCLUDE is fragile: a new device/meter
# tomorrow would silently pollute room readings. Instead we explicitly ALLOW only
# known room-temperature sensor families. Unrelated devices are ignored by default;
# to support a new room-sensor family, add its pattern below.
_ROOM_TEMP_PATTERNS = (
    re.compile(r"^sensor\.setecna_.*_zone_\d+_temperature$"),  # Setecna per-zone room temp
)
_TEMP_ROOM_MIN_C = 0.0
_TEMP_ROOM_MAX_C = 50.0


def _room_temp_value(entity_id: str, state_val) -> Optional[float]:
    """Return the temp (°C) if entity is a WHITELISTED room sensor, else None.

    Only sensors matching _ROOM_TEMP_PATTERNS count as room temperature, so
    name-matching and whole-house averaging never pick up equipment/setpoint
    sensors — and adding new unrelated devices can't break it.
    """
    eid = entity_id.lower()
    if not any(p.match(eid) for p in _ROOM_TEMP_PATTERNS):
        return None
    try:
        t = float(state_val)
    except (ValueError, TypeError):
        return None
    if not (_TEMP_ROOM_MIN_C <= t <= _TEMP_ROOM_MAX_C):
        return None
    return t


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

            # Raccogli i sensori-STANZA in whitelist (solo zone Setecna, non meter/impianto)
            room_sensors = []  # (entity_id, friendly_lower, temp, unit)
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

                # Solo sensori-stanza in whitelist (zone Setecna); scarta il resto
                temp = _room_temp_value(entity_id, state_val)
                if temp is None:
                    continue

                friendly = attrs.get("friendly_name", "").lower()
                room_sensors.append((entity_id, friendly, temp, uom if uom else "°C"))

            # --- STRATEGIA 1: sensore-stanza il cui nome matcha la stanza richiesta ---
            best_match = None
            best_score = 0
            best_entity_id = None

            for entity_id, friendly, temp, unit in room_sensors:
                eid_lower = entity_id.lower()

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
                    best_match = {
                        "temperature": temp,
                        "unit": unit,
                        "entity_id": entity_id,
                        "source": "sensor",
                        "location_id": loc_id
                    }

            if best_match:
                # Cache entity resolution (24h) — valore letto live ogni volta
                _temp_entity_cache[cache_key] = {
                    "entity_id": best_entity_id,
                    "source": "sensor",
                    "timestamp": now
                }
                logger.info(f"Temperature for '{room}': {best_match['temperature']}{best_match['unit']} from {best_match['entity_id']} (entity resolved, cached 24h)")
                return best_match

            # --- STRATEGIA 2: device mobile/generico (es. "Casa" portatile) → MEDIA stanze ---
            # Nessun match esatto ma esistono sensori-stanza reali: ritorna la media.
            if room_sensors:
                avg = round(sum(t for _, _, t, _ in room_sensors) / len(room_sensors), 1)
                logger.info(f"Temperature for '{room}': avg {avg}°C over {len(room_sensors)} room sensors in '{loc_id}'")
                return {
                    "temperature": avg,
                    "unit": "°C",
                    "entity_id": "__average__",
                    "source": "average",
                    "location_id": loc_id,
                    "sensor_count": len(room_sensors),
                }

            # --- STRATEGIA 3: nessun sensore-stanza → fallback meteo (es. albani20) ---
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
    Persistent WebSocket for voice devices (AtomS3R, NabuVoice): control + audio on single channel.

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
    # Only do: audio normalize → STT → deactivation check → AI Agent
    if device_id in _live_sessions:
        logger.info(f"🎙️ Live session active for {device_id} — using simplified pipeline")
        await handle_live_session_turn(device_id, audio_bytes)
        return

    # ── AI_AGENT FOLLOW-UP BYPASS ─────────────────────────────────────
    # Se AI Agent ha chiesto chiarimenti, il prossimo input bypassa routing
    # e va diretto a AI Agent. Speaker ID: se anonimo al turno 1, rifa
    # speaker ID; se già riconosciuto, usa identità bloccata.
    # SOLO se il mic è stato riattivato da trigger_device_listen (non bottone fisico).
    if device_id in _ai_agent_followup:
        followup = _ai_agent_followup[device_id]
        if device_id not in _ai_agent_followup_triggered:
            # Bottone fisico premuto → reset follow-up, routing normale
            logger.info(f"🔄 AI Agent follow-up reset by button press on {device_id}")
            _ai_agent_followup.pop(device_id, None)
        elif time.time() - followup["timestamp"] >= 120:  # 2 min timeout
            logger.info(f"🔄 AI Agent follow-up expired for {device_id}")
            _ai_agent_followup.pop(device_id, None)
            _ai_agent_followup_triggered.discard(device_id)
        else:
            _ai_agent_followup_triggered.discard(device_id)  # Consumato
            logger.info(f"🔄 AI Agent follow-up active for {device_id} — bypassing routing")
            try:
                # Audio normalization
                import numpy as _np_fu
                _raw_fu = _np_fu.frombuffer(audio_bytes, dtype=_np_fu.int16).astype(_np_fu.float32)
                _peak_fu = _np_fu.max(_np_fu.abs(_raw_fu))
                if _peak_fu > 0:
                    _gain_fu = min(28000.0 / _peak_fu, 20.0)
                    _raw_fu = (_raw_fu * _gain_fu).clip(-32768, 32767)
                clean_audio_fu = _raw_fu.astype(_np_fu.int16).tobytes()

                # STT (always needed)
                import asyncio as _aio_fu
                loop_fu = _aio_fu.get_running_loop()
                stt_task_fu = _aio_fu.ensure_future(transcribe_audio(clean_audio_fu))

                # Speaker ID: only if anonymous at turn 1
                speaker_task_fu = None
                if followup["speaker_id"] is None:
                    _ws_dt_fu = followup.get("device_config", {}).get("device_type", "AtomS3R") if followup.get("device_config") else "AtomS3R"
                    speaker_task_fu = loop_fu.run_in_executor(
                        None, build_speaker_context, audio_bytes, _ws_dt_fu
                    )

                text_fu = await stt_task_fu
                if not text_fu or not text_fu.strip():
                    if speaker_task_fu:
                        speaker_task_fu.cancel()
                    await trigger_device_listen(device_id, silent=True)
                    return
                if is_dirty_audio(text_fu):
                    if speaker_task_fu:
                        speaker_task_fu.cancel()
                    await trigger_device_listen(device_id, silent=True)
                    return

                # STT normalization
                text_fu = await normalize_stt_text(text_fu)
                logger.info(f"🔄 Follow-up transcribed: '{text_fu[:80]}' → sending to AI Agent")

                # Update speaker if re-identified
                fu_speaker_id = followup["speaker_id"]
                fu_speaker_name = followup["speaker_name"]
                fu_speaker_identified = fu_speaker_id is not None
                if speaker_task_fu:
                    speaker_ctx_fu = await speaker_task_fu
                    if speaker_ctx_fu.get("speaker_identified"):
                        # Riconosciuto! Blocca identità per turni successivi
                        fu_speaker_id = speaker_ctx_fu["speaker_id"]
                        fu_speaker_name = speaker_ctx_fu["speaker_name"]
                        fu_speaker_identified = True
                        followup["speaker_id"] = fu_speaker_id
                        followup["speaker_name"] = fu_speaker_name
                        logger.info(f"🔄 Follow-up: speaker identified as {fu_speaker_name}")

                context_fu = {
                    "source": followup["source"],
                    "room": followup["room"],
                    "mic_id": device_id,
                    "device_id": device_id,
                    "location": followup["location"],
                    "device_config": followup["device_config"],
                    "speaker_id": fu_speaker_id,
                    "speaker_name": fu_speaker_name,
                    "speaker_identified": fu_speaker_identified,
                    "is_admin": False,
                }
                # Check admin status if identified
                if fu_speaker_id:
                    from database import get_user_by_id
                    _fu_user = get_user_by_id(fu_speaker_id)
                    if _fu_user:
                        context_fu["is_admin"] = _fu_user.is_admin

                await _handle_ai_agent_voice(text_fu, context_fu, hint="followup")
                return
            except Exception as e:
                logger.error(f"🔄 AI Agent follow-up error: {e}", exc_info=True)
                _ai_agent_followup.pop(device_id, None)
                _ai_agent_followup_triggered.discard(device_id)
                # Fall through to normal pipeline

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
        _ws_device_type = device_config.get("device_type", "AtomS3R") if device_config else "AtomS3R"
        speaker_task = loop.run_in_executor(
            None,  # default ThreadPoolExecutor
            build_speaker_context, audio_bytes, _ws_device_type
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
            "source": _ws_device_type,
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

        if config.SKIP_PRE_ROUTE:
            # Routing unificato: salta pre-route, vai diretto al routing completo
            await process_jarvis_logic(text, context)
        else:
            # Legacy: pre-route 3-way → dispatch
            pre_route_start = time.time()
            pre_result = await pre_route(text)
            pre_route_ms = (time.time() - pre_route_start) * 1000
            classification = pre_result.get("classification", "ALTRO")
            logger.info(f"WS pre-route: {classification} (conf={pre_result.get('confidence', 0):.2f}, {pre_route_ms:.0f}ms)")

            if classification == "DOMOTICA_CERTA":
                await process_jarvis_logic(text, context)
            elif classification == "DOMOTICA_INCERTA":
                await _handle_ai_agent_voice(text, context, hint="domotics")
            else:
                await _handle_ai_agent_voice(text, context, hint="")

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
    Riceve audio in streaming dai voice devices.
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
        source_type = "VirtualMic" if is_virtual_mic else (device_config.get("device_type", "AtomS3R") if device_config else "AtomS3R")
        speaker_start = time.time()
        speaker_ctx = build_speaker_context(audio_bytes, source_type)
        admin_metrics.record_speaker_id((time.time() - speaker_start) * 1000)

        context = {
            "source": source_type,
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

        if config.SKIP_PRE_ROUTE:
            # Routing unificato: salta pre-route, vai diretto al routing completo
            asyncio.create_task(process_jarvis_logic(text, context))
            classification = "unified"
        else:
            # Legacy: pre-route 3-way → dispatch
            pre_route_start = time.time()
            pre_result = await pre_route(text)
            pre_route_ms = (time.time() - pre_route_start) * 1000
            classification = pre_result.get("classification", "ALTRO")
            logger.info(f"Pre-route: {classification} (conf={pre_result.get('confidence', 0):.2f}, {pre_route_ms:.0f}ms)")

            if classification == "DOMOTICA_CERTA":
                asyncio.create_task(process_jarvis_logic(text, context))
            elif classification == "DOMOTICA_INCERTA":
                asyncio.create_task(_handle_ai_agent_voice(text, context, hint="domotics"))
            else:
                asyncio.create_task(_handle_ai_agent_voice(text, context, hint=""))

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
    indicating AI Agent is asking the user something (multi-turn).

    Simple regex is the right choice: AI Agent produces well-punctuated Italian,
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
# AI_AGENT VOICE HANDLER (for DOMOTICA_INCERTA and ALTRO pre-routes)
# ===========================================================================

async def _handle_ai_agent_voice(text: str, context: dict, hint: str = ""):
    """
    Handle voice commands that need AI Agent (non-certain domotics or general queries).

    For voice device sources: uses streaming TTS — sentences are spoken as they
    arrive from AI Agent SSE, without waiting for the full response. This dramatically
    reduces perceived latency.

    For other sources (Telegram, etc.): waits for full response, then delivers.

    Multi-turn: uses AI Agent 'user' parameter for stable session routing.
    If the speaker is identified, session key = speaker name (e.g. "marco").
    Otherwise, session key = room/device name to group anonymous interactions.
    AI Agent generates: openresponses-main-user:{session_user}
    """
    context["_user_text"] = text  # Per VirtualMic response tracking
    context["_intent"] = f"AI_AGENT:{hint}" if hint else "AI_AGENT"

    # Build stable session user key for AI Agent multi-turn.
    # Uses speaker name for identified users, room/source for anonymous.
    speaker_id = context.get("speaker_id")
    speaker_name = context.get("speaker_name", "")
    speaker_identified = context.get("speaker_identified", False)
    device_cfg = context.get("device_config")
    room = (device_cfg.get("room") if device_cfg else None) or context.get("room", "")
    if speaker_identified and speaker_name and speaker_name != "Sconosciuto":
        session_user = speaker_name.lower()
    elif room:
        session_user = room.lower()
    else:
        session_user = context.get("source", "unknown").lower()
    logger.info(f"AI Agent multi-turn session_user={session_user}")

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
        source in config.VOICE_SOURCES
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

        # Forward to AI Agent with streaming callback + multi-turn session
        response, response_id = await forward_to_ai_agent(
            text, context, hint=hint,
            stream_tts_callback=_stream_tts_chunk,
            session_user=session_user
        )
        if response_id:
            logger.debug(f"AI Agent response_id={response_id[:30]}... for session={session_user}")

        # Log full response text for debugging
        if response:
            logger.info(f"📝 AI response ({len(response)} chars): {response[:500]}{'...' if len(response) > 500 else ''}")

        # Post-streaming: save chat, update speaking state, VirtualMic tracking
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")

        # Speaking state + post-TTS handler
        room = context.get("room", "salotto").lower()
        device_id = context.get("device_id", "unknown")
        is_multi_turn = _needs_followup(response) and source in config.VOICE_SOURCES and source != "VirtualMic"

        # ── AI Agent follow-up tracking ──
        # Se AI Agent chiede chiarimenti (?), il prossimo voice input bypassa routing
        if is_multi_turn and device_id and device_id != "unknown":
            _ai_agent_followup[device_id] = {
                "session_user": session_user,
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "location": location,
                "room": room,
                "device_config": device_cfg,
                "source": source,
                "timestamp": time.time(),
            }
            logger.info(f"🔄 AI Agent follow-up SET for {device_id} (session={session_user})")
        elif device_id:
            # Conversazione finita, cleanup
            _ai_agent_followup.pop(device_id, None)

        if use_internal_speaker and device_id and device_id != "unknown":
            # Internal speaker: niente polling HA, gestione diretta
            # speak_to_device() invia frame Opus in modo sincrono — il device
            # riproduce man mano. Dopo l'ultimo frame serve solo un piccolo
            # delay per il flush del buffer DMA (~150ms).
            await asyncio.sleep(0.15)

            if is_multi_turn:
                success = await trigger_device_listen(device_id, silent=True)
                if success:
                    _ai_agent_followup_triggered.add(device_id)
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
        if source in config.VOICE_SOURCES:
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

        response, _ = await forward_to_ai_agent(text, context, hint=hint, session_user=session_user)

        # Log full response text for debugging
        if response:
            logger.info(f"📝 AI response ({len(response)} chars): {response[:500]}{'...' if len(response) > 500 else ''}")

        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        await deliver_final_response(response, context, sound_type="neutral")

# ===========================================================================
# PARAMETER EXTRACTION FROM USER TEXT (fallback when Qwen drops params)
# ===========================================================================

_TEXT_COLOR_MAP = {
    "rosso": [255, 0, 0], "rossa": [255, 0, 0], "red": [255, 0, 0],
    "verde": [0, 255, 0], "green": [0, 255, 0],
    "blu": [0, 0, 255], "blue": [0, 0, 255],
    "giallo": [255, 255, 0], "gialla": [255, 255, 0], "yellow": [255, 255, 0],
    "arancione": [255, 165, 0], "arancio": [255, 165, 0], "orange": [255, 165, 0],
    "viola": [128, 0, 128], "purple": [128, 0, 128],
    "rosa": [255, 105, 180], "pink": [255, 105, 180],
    "bianco": [255, 255, 255], "bianca": [255, 255, 255], "white": [255, 255, 255],
    "ciano": [0, 255, 255], "cyan": [0, 255, 255],
    "magenta": [255, 0, 255],
}

def _extract_params_from_text(user_text: str) -> dict:
    """
    Estrae parametri HA (colore, brightness, temperatura) dal testo utente.
    Usato come fallback quando Qwen non include i parametri nel payload.
    """
    params = {}
    text_l = user_text.lower()

    # Colore
    for color_name, rgb in _TEXT_COLOR_MAP.items():
        if color_name in text_l:
            params["rgb_color"] = rgb
            break

    # Luce calda/fredda
    if not params:
        if "luce calda" in text_l or "caldo" in text_l or "warm" in text_l:
            params["color_temp_kelvin"] = 2700
        elif "luce fredda" in text_l or "freddo" in text_l or "cool" in text_l:
            params["color_temp_kelvin"] = 6500

    # Brightness: "al X%", "luminosità X", "X percento"
    bri_match = re.search(r'(?:al\s+|luminosit[àa]\s+(?:al\s+)?|brightness\s+)(\d{1,3})\s*%?', text_l)
    if not bri_match:
        bri_match = re.search(r'(\d{1,3})\s*(?:percento|per\s*cento|%)', text_l)
    if bri_match:
        pct = int(bri_match.group(1))
        if 0 <= pct <= 100:
            params["brightness"] = round(pct * 255 / 100)

    # Temperatura: "a X gradi", "X gradi"
    temp_match = re.search(r'(?:a\s+)?(\d{1,2}(?:\.\d)?)\s*(?:gradi|°|degrees)', text_l)
    if temp_match:
        temp = float(temp_match.group(1))
        if 5 <= temp <= 40:
            params["temperature"] = temp

    return params


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

    # Carica room/zone/area reali dal DB — cerca location specifica PRIMA dei wildcard
    locations = get_entity_map_locations(location_id)
    if locations:
        # Ordina per lunghezza decrescente (match più specifico prima)
        # es. "Zona Giorno" deve matchare prima di "Giorno"
        all_names = sorted(locations, key=len, reverse=True)

        for name in all_names:
            if name.lower() in text_lower:
                return name

    # Wildcard solo se nessuna location specifica trovata
    wildcard_tokens = {"tutta la casa", "tutto", "tutti", "tutte", "ovunque", "dappertutto"}
    for wt in wildcard_tokens:
        if wt in text_lower:
            return wt

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
                # Se c'è un solo entity nel risultato, restituisci direttamente
                if len(discovered) == 1:
                    return _make_bulk_result(discovered, f"user_text:'{extracted}'")

                # Multiple entities: prova a restringere con entity_name di Qwen
                # prima di restituire bulk (evita "accendi luce specchio" → tutte le luci)
                disc_names = [e["entity_name"] for e in discovered]
                logger.info(
                    f"Entity resolution [step_A]: room='{extracted}', "
                    f"qwen_entity='{entity_name}', discovered={disc_names}"
                )

                # Parole generiche da escludere dal keyword matching
                _generic = {
                    "luce", "luci", "lampada", "lampade", "led", "la", "le", "il", "i",
                    "lo", "gli", "un", "una", "del", "della", "dei", "delle", "dello",
                    "di", "in", "nel", "nella", "al", "alla", "accendi", "spegni",
                    "apri", "chiudi", "alza", "abbassa", "imposta", "attiva", "disattiva",
                    "tapparella", "tapparelle", "tenda", "tende",
                    "sensore", "sensori", "interruttore", "interruttori",
                    "presa", "prese", "switch", "clima", "condizionatore",
                }

                if entity_name:
                    entity_lower = entity_name.lower().strip()
                    # Match esatto tra entity_name Qwen e discovered entities
                    exact = [e for e in discovered if e["entity_name"].lower().strip() == entity_lower]
                    if len(exact) == 1:
                        logger.info(
                            f"Entity resolution [user_text+qwen_exact]: "
                            f"'{entity_name}' in room '{extracted}' → {exact[0]['entity_id']}"
                        )
                        return {
                            "mode": "single",
                            "entity_ids": [exact[0]["entity_id"]],
                            "description": exact[0]["entity_name"],
                            "match_type": "exact_in_room",
                        }
                    # Match parziale (substring)
                    partial = [
                        e for e in discovered
                        if entity_lower in e["entity_name"].lower().strip()
                        or e["entity_name"].lower().strip() in entity_lower
                    ]
                    if len(partial) == 1:
                        logger.info(
                            f"Entity resolution [user_text+qwen_partial]: "
                            f"'{entity_name}' in room '{extracted}' → {partial[0]['entity_id']}"
                        )
                        return {
                            "mode": "single",
                            "entity_ids": [partial[0]["entity_id"]],
                            "description": partial[0]["entity_name"],
                            "match_type": "partial_in_room",
                        }

                    # Match per parole chiave da entity_name Qwen
                    keywords = [
                        w for w in re.sub(r'[,.\!\?\;\:\-]', ' ', entity_lower).split()
                        if w not in _generic and len(w) >= 3
                    ]
                    if keywords:
                        scored = []
                        for e in discovered:
                            e_lower = e["entity_name"].lower()
                            hits = sum(1 for kw in keywords if kw in e_lower)
                            if hits:
                                scored.append((hits, e))
                        if len(scored) == 1:
                            best = scored[0][1]
                            logger.info(
                                f"Entity resolution [user_text+qwen_keyword]: "
                                f"keywords={keywords} in room '{extracted}' → {best['entity_id']}"
                            )
                            return {
                                "mode": "single",
                                "entity_ids": [best["entity_id"]],
                                "description": best["entity_name"],
                                "match_type": "keyword_in_room",
                            }
                        elif len(scored) > 1:
                            # Più match: prendi quello con più keyword hits
                            scored.sort(key=lambda x: x[0], reverse=True)
                            if scored[0][0] > scored[1][0]:
                                best = scored[0][1]
                                logger.info(
                                    f"Entity resolution [user_text+qwen_keyword_best]: "
                                    f"keywords={keywords} in room '{extracted}' → {best['entity_id']} (score {scored[0][0]} vs {scored[1][0]})"
                                )
                                return {
                                    "mode": "single",
                                    "entity_ids": [best["entity_id"]],
                                    "description": best["entity_name"],
                                    "match_type": "keyword_in_room",
                                }

                # Fallback: estrai keywords dal testo utente originale
                # (utile quando Qwen ritorna la room come entity o non ritorna entity)
                if user_text:
                    ut_lower = re.sub(r'[,.\!\?\;\:\-\']', ' ', user_text.lower())
                    ut_keywords = [
                        w for w in ut_lower.split()
                        if w not in _generic and len(w) >= 3
                        and w not in extracted.lower().split()  # escludi parole della room
                    ]
                    if ut_keywords:
                        scored = []
                        for e in discovered:
                            e_lower = e["entity_name"].lower()
                            hits = sum(1 for kw in ut_keywords if kw in e_lower)
                            if hits:
                                scored.append((hits, e))
                        if len(scored) == 1:
                            best = scored[0][1]
                            logger.info(
                                f"Entity resolution [user_text_keyword]: "
                                f"keywords={ut_keywords} in room '{extracted}' → {best['entity_id']}"
                            )
                            return {
                                "mode": "single",
                                "entity_ids": [best["entity_id"]],
                                "description": best["entity_name"],
                                "match_type": "user_text_keyword",
                            }
                        elif len(scored) > 1:
                            scored.sort(key=lambda x: x[0], reverse=True)
                            if scored[0][0] > scored[1][0]:
                                best = scored[0][1]
                                logger.info(
                                    f"Entity resolution [user_text_keyword_best]: "
                                    f"keywords={ut_keywords} in room '{extracted}' → {best['entity_id']} (score {scored[0][0]} vs {scored[1][0]})"
                                )
                                return {
                                    "mode": "single",
                                    "entity_ids": [best["entity_id"]],
                                    "description": best["entity_name"],
                                    "match_type": "user_text_keyword",
                                }

                # Nessun match specifico: bulk solo se plurale esplicito
                plural_tokens = {"tutti", "tutte", "tutto", "le luci", "le tapparelle", "i climatizzatori"}
                text_lower = user_text.lower()
                if any(tok in text_lower for tok in plural_tokens):
                    return _make_bulk_result(discovered, f"user_text:'{extracted}'")

                # Singolare senza match → chiedi chiarimento
                return _make_clarify_result(discovered, f"user_text:'{extracted}'")

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

    # ── C-bis. FALLBACK SEMANTICO: nessun match testuale → ranking embedding ──
    # Risolve nomi che il match SQL su room/area non trova (es. "Tapparella Salotto"
    # → cover.tapparella_veranda_salotto). Per le AZIONI è conservativo: agisce solo
    # se il match è NETTO; se è ambiguo chiede conferma invece di agire a caso.
    try:
        import semantic_discovery
        _hits = semantic_discovery.search_sync(location_id, entity_name, domain=domain, top_k=4)
        _strong = [h for h in _hits if h.get("score", 0) >= 0.6]
        if _strong:
            _top = _strong[0]
            _margin = _top["score"] - (_strong[1]["score"] if len(_strong) > 1 else 0.0)
            if len(_strong) == 1 or _margin >= 0.04:
                logger.info(
                    f"Entity resolution [semantic]: '{entity_name}' (domain={domain}) → "
                    f"{_top['entity_id']} (score {_top['score']:.2f}, margin {_margin:.2f})"
                )
                return {
                    "mode": "single",
                    "entity_ids": [_top["entity_id"]],
                    "description": _top.get("entity_name") or entity_name,
                    "match_type": "semantic",
                }
            # Ambiguo: candidati troppo vicini → chiedi quale
            _cands = [
                {"entity_id": h["entity_id"], "entity_name": h.get("entity_name"), "room": h.get("room")}
                for h in _strong[:3]
            ]
            return _make_clarify_result(_cands, f"semantic:'{entity_name}'")
    except Exception as _e:
        logger.debug(f"semantic action resolution failed: {_e}")

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

        # Semantic resolution for free-text searches (handles language/synonyms,
        # e.g. "temperatura" -> "...temperature"); falls back to the SQL map query.
        discovered = []
        if params.get("search"):
            try:
                import semantic_discovery
                _dc = params.get("device_class") or semantic_discovery.device_class_hint(params["search"])
                _hits = await semantic_discovery.search(
                    target_location, params["search"],
                    # NB: the router's `domain` is unreliable for device/info queries
                    # ("pompa di calore" -> wrongly domain=climate, excluding the
                    # heat-pump sensors). search + device_class are the solid signals.
                    room=params.get("room"), domain=None,
                    device_class=_dc, top_k=8,
                )
                discovered = [
                    {"entity_id": h["entity_id"], "friendly_name": h["entity_name"],
                     "domain": h["entity_type"], "room": h.get("room")}
                    for h in _hits
                ]
            except Exception as _e:
                logger.debug(f"semantic state-query failed, SQL fallback: {_e}")

        if not discovered:
            # Build DB query to find matching entities (SQL map fallback)
            conn = _get_conn()
            c = conn.cursor()
            query = """
                SELECT entity_id, entity_name, entity_type, room, device_name, area, zone
                FROM entity_maps
                WHERE location_id = ? AND entity_id IS NOT NULL
                  AND COALESCE(visible, 1) = 1
            """
            q_params: list = [target_location]

            if params.get("domain"):
                query += " AND entity_type = ?"
                q_params.append(params["domain"])

            if params.get("room"):
                # Qwen spesso mette zone/piani nel campo room — cerca in room, area e zone
                _room_val = params["room"]
                query += " AND (LOWER(room) LIKE LOWER(?) OR LOWER(area) LIKE LOWER(?) OR LOWER(zone) LIKE LOWER(?))"
                q_params.extend([f"%{_room_val}%", f"%{_room_val}%", f"%{_room_val}%"])

            if params.get("zone"):
                query += " AND (LOWER(area) LIKE LOWER(?) OR LOWER(zone) LIKE LOWER(?))"
                q_params.extend([f"%{params['zone']}%", f"%{params['zone']}%"])

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

            for row in rows:
                discovered.append({
                    "entity_id": row["entity_id"],
                    "friendly_name": row["entity_name"],
                    "domain": row["entity_type"],
                    "room": row["room"],
                })

        if not discovered:
            return "Non ho trovato dispositivi corrispondenti."

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

        # Value-style answer when the result set is sensors (or a measured quantity
        # was asked). The router frequently omits `domain`, so decide from the actual
        # discovered entities instead of trusting params.domain (else sensors get
        # wrongly summarised as on/off).
        _disc_domains = {e.get("domain") for e in discovered}
        _is_value_query = bool(params.get("device_class")) or (
            bool(_disc_domains) and _disc_domains <= {"sensor", "binary_sensor"}
        )

        if _is_value_query:
            parts = []
            for ent in discovered:
                live = states.get(ent["entity_id"], {})
                attrs = live.get("attributes", {})
                unit = attrs.get("unit_of_measurement", "")
                state = live.get("state", "sconosciuto")
                val = f"{ent['friendly_name']}: {state}{' ' + unit if unit else ''}"
                parts.append(val)
            return f"Valori in {scope}: " + ", ".join(parts[:15])

        on_states = {"on", "heat", "cool", "auto", "open", "playing"}
        on_items = []
        off_items = []
        for ent in discovered:
            live = states.get(ent["entity_id"], {})
            if live.get("state", "").lower() in on_states:
                on_items.append(ent["friendly_name"])
            else:
                off_items.append(ent["friendly_name"])

        total = len(discovered)
        on_count = len(on_items)
        off_count = len(off_items)

        # Analizza il testo utente per capire cosa chiede
        _user_text = (context.get("_user_text") or "").lower()
        _asks_on = any(k in _user_text for k in ("acces", "attiv", "aperte", "aperti"))
        _asks_off = any(k in _user_text for k in ("spent", "spegn", "chiuse", "chiusi", "disattiv"))
        _asks_count = any(k in _user_text for k in ("quant", "numer"))

        # Risposta mirata in base alla domanda
        if _asks_count and _asks_on:
            if on_count == 0:
                return f"Nessuna {domain_label} accesa in {scope}."
            return f"{on_count} {domain_label} accese su {total} in {scope}."
        elif _asks_count and _asks_off:
            if off_count == 0:
                return f"Nessuna {domain_label} spenta in {scope}."
            return f"{off_count} {domain_label} spente su {total} in {scope}."
        elif _asks_count:
            return f"Ci sono {total} {domain_label} in {scope} ({on_count} accese, {off_count} spente)."
        elif _asks_on:
            if on_count == 0:
                return f"Nessuna {domain_label} accesa in {scope}."
            return f"{on_count} {domain_label} accese in {scope}: {', '.join(on_items[:15])}."
        elif _asks_off:
            if off_count == 0:
                return f"Nessuna {domain_label} spenta in {scope}."
            return f"{off_count} {domain_label} spente in {scope}: {', '.join(off_items[:15])}."

        # Domanda generica ("cosa c'è in..."): elenca tutto ma conciso
        if on_count == 0:
            return f"In {scope}: {total} {domain_label}, tutte spente."
        elif on_count == total:
            return f"In {scope}: {total} {domain_label}, tutte accese."
        else:
            return (f"In {scope}: {total} {domain_label}. "
                    f"Accese ({on_count}): {', '.join(on_items[:15])}. "
                    f"Spente ({off_count}): {', '.join(off_items[:15])}.")

    except Exception as e:
        logger.error(f"Entity query from SIMPLE_CHAT failed: {e}")
        return None


async def _phrase_ha_data(user_text: str, ha_data: str, context: dict,
                          speaker_id=None, location: str = None) -> str:
    """Turn structured HA data into a natural, spoken Italian answer.

    The entity executors return a compact technical string ("Salotto temperature:
    27.4 °C"); read verbatim it sounds like JSON. This rephrases it like a voice
    assistant would, picking the value relevant to the question.
    """
    try:
        phrased = await get_quick_response(
            f"L'utente ha chiesto: \"{user_text}\"\n\n"
            f"Dati attuali da Home Assistant:\n{ha_data}\n\n"
            f"Rispondi in italiano in modo naturale, breve e parlato, basandoti SOLO su "
            f"questi dati. NON elencare in formato tecnico o JSON, niente nomi di entità "
            f"grezzi. Se ci sono più valori scegli quello pertinente alla domanda. "
            f"Usa le unità di misura ESATTE come nei dati (W = watt, kW = kilowatt, "
            f"°C = gradi, % = percento); non inventare o convertire unità. "
            f"REGOLA CRITICA: usa ESCLUSIVAMENTE i valori presenti nei dati qui sopra. "
            f"Se i dati NON contengono quanto chiesto (es. uno storico, un confronto con "
            f"ieri/il passato, un valore non presente), DILLO che non hai quel dato — "
            f"NON inventare MAI numeri, date o valori non presenti.",
            context, user_id=speaker_id, location_id=location, enable_tools=False,
        )
        return phrased or ha_data
    except Exception as e:
        logger.debug(f"_phrase_ha_data failed, using raw: {e}")
        return ha_data


async def _execute_entity_status(payload: dict, location: str, context: dict) -> str | None:
    """
    Restituisce stato dettagliato + attributi + capacità di un'entità specifica.
    Usato da SIMPLE_CHAT con api_call="entity_status".
    """
    try:
        from database import resolve_entity_id, resolve_entity_id_fuzzy

        params = payload.get("params", {})
        entity_name = params.get("entity", "")
        target_location = params.get("location_id") or location or get_default_location_id()
        domain_hint = params.get("domain")
        room_hint = params.get("room")

        if not entity_name:
            return "Non ho capito quale dispositivo vuoi controllare."

        # Risolvi entity_id
        entity_id = resolve_entity_id(target_location, entity_name, domain_hint, room_hint)
        if not entity_id:
            # Fuzzy match
            fuzzy = resolve_entity_id_fuzzy(target_location, entity_name, domain_hint)
            if fuzzy:
                entity_id = fuzzy[0]["entity_id"]
                entity_name = fuzzy[0].get("entity_name", entity_name)
            else:
                return f"Non ho trovato il dispositivo '{entity_name}'."

        # Fetch live state da HA
        states = await multi_ha.get_states_bulk(target_location, [entity_id])
        live = states.get(entity_id, {})
        if not live:
            return f"Non riesco a leggere lo stato di '{entity_name}'."

        state = live.get("state", "sconosciuto")
        attrs = live.get("attributes", {})
        eid_domain = entity_id.split(".")[0]

        # Build risposta dettagliata per dominio
        parts = [f"{entity_name} ({eid_domain}) — stato: {state}"]

        if eid_domain == "light":
            if attrs.get("brightness") is not None:
                bri_pct = round(attrs["brightness"] / 255 * 100)
                parts.append(f"Luminosità: {bri_pct}%")
            if attrs.get("color_temp_kelvin") is not None:
                parts.append(f"Temperatura colore: {attrs['color_temp_kelvin']}K")
            if attrs.get("rgb_color") is not None:
                r, g, b = attrs["rgb_color"]
                parts.append(f"Colore RGB: ({r}, {g}, {b})")
            if "color_mode" in attrs:
                parts.append(f"Modalità colore: {attrs['color_mode']}")
            modes = attrs.get("supported_color_modes", [])
            if modes:
                caps = []
                if "brightness" in modes or "color_temp" in modes or "hs" in modes or "rgb" in modes or "xy" in modes:
                    caps.append("luminosità")
                if "color_temp" in modes:
                    caps.append("temperatura colore (caldo/freddo)")
                if any(m in modes for m in ("hs", "rgb", "xy", "rgbw", "rgbww")):
                    caps.append("colore RGB")
                if caps:
                    parts.append(f"Puoi configurare: {', '.join(caps)}")
                else:
                    parts.append("Solo accensione/spegnimento")

        elif eid_domain == "climate":
            if attrs.get("temperature") is not None:
                parts.append(f"Temperatura target: {attrs['temperature']}°C")
            if attrs.get("current_temperature") is not None:
                parts.append(f"Temperatura attuale: {attrs['current_temperature']}°C")
            if "hvac_modes" in attrs:
                parts.append(f"Modalità: {', '.join(attrs['hvac_modes'])}")

        elif eid_domain == "cover":
            if "current_position" in attrs:
                parts.append(f"Posizione: {attrs['current_position']}%")
            parts.append("Puoi configurare: posizione (0-100%)")

        elif eid_domain == "media_player":
            if "volume_level" in attrs:
                parts.append(f"Volume: {round(attrs['volume_level'] * 100)}%")
            if "source" in attrs:
                parts.append(f"Sorgente: {attrs['source']}")
            if "source_list" in attrs:
                parts.append(f"Sorgenti disponibili: {', '.join(attrs['source_list'][:8])}")

        elif eid_domain == "fan":
            if "percentage" in attrs:
                parts.append(f"Velocità: {attrs['percentage']}%")

        else:
            # Generic: mostra attributi non-system
            skip = {"friendly_name", "icon", "entity_picture", "supported_features",
                    "device_class", "attribution"}
            for k, v in list(attrs.items())[:10]:
                if k not in skip and not k.startswith("_"):
                    parts.append(f"{k}: {v}")

        return "\n".join(parts)

    except Exception as e:
        logger.error(f"Entity status query failed: {e}", exc_info=True)
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

    _ctx_t0 = time.time()
    logger.info(f"Processing: '{text[:50]}...' from {source} (speaker: {speaker_name}, location: {location})")

    # 0a. DIRTY AUDIO CHECK (era nel pre-route, ora qui — puro Python, no LLM)
    if is_dirty_audio(text):
        logger.debug(f"Dirty audio detected, ignoring: {text!r}")
        return

    # 0b. IMAGE GENERATION KEYWORD CHECK (puro Python)
    # Se è un comando di generazione immagini, forwarda a AI Agent che ha Cloud LLM
    if is_image_generation_intent(text):
        logger.info(f"Image generation keyword detected: '{text[:50]}'")
        await _handle_ai_agent_voice(text, context, hint="image_generation")
        return

    # 0c. CHECK CRITICAL SERVICES
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

    # 1b. QUICK CONTEXTUAL ANSWERS (richiedono contesto, non cache statica)
    _text_lower = text.lower().strip().rstrip("?!.")
    _quick = None
    if _text_lower in ("chi sono", "chi sono io", "come mi chiamo", "mi riconosci", "sai chi sono"):
        _quick = f"Sei {speaker_name}!" if speaker_name and speaker_name != "Sconosciuto" else "Non ti ho riconosciuto, chi sei?"
    elif _text_lower in ("dove sono", "in che stanza sono", "dove mi trovo"):
        _room = context.get("room", "Unknown")
        _loc = location or "sconosciuta"
        if _room and _room != "Unknown":
            _quick = f"Sei in {_room}, location {_loc}."
        else:
            _quick = f"Sei nella location {_loc}, ma non so in quale stanza."
    if _quick:
        logger.info(f"Quick contextual answer: '{_text_lower}'")
        save_chat_message("assistant", _quick, "JARVIS", None, "Jarvis")
        await deliver_final_response(_quick, context, sound_type="neutral")
        return

    # 2. SAFETY CHECK
    _t = time.time()
    is_safe_result, reason = await is_safe(text, source)
    _safety_ms = (time.time() - _t) * 1000
    if not is_safe_result:
        await send_telegram(f"⚠️ Comando bloccato: {reason}")
        log_event("SECURITY", f"Comando bloccato da {source}: {reason}", speaker_id, speaker_name)
        return

    # 3. MEMORIA RECENTE per routing (ultimi N turni, pura SQLite — no vector/embedding)
    # Qwen 3B non fa coreference complessa, ma 3 turni in 5 min coprono
    # "accendi X" → "spegnila" senza portare rumore da ore prima.
    # Il contesto completo (weighted + vector + stratified) resta per AI Agent/reasoning.
    _t = time.time()
    recent_turns = get_recent_turns(
        max_turns=config.ROUTER_MEMORY_TURNS,
        max_seconds=config.ROUTER_MEMORY_WINDOW_SECONDS,
    )
    router_memory_prompt = format_recent_turns_for_llm(recent_turns)
    _memory_ms = (time.time() - _t) * 1000

    # 4. SALVA INPUT
    _t = time.time()
    user_chat_id = save_chat_message("user", text, source, speaker_id, speaker_name)
    _save_ms = (time.time() - _t) * 1000

    # 5. ROUTING (con memoria ridotta per velocità)
    # Inietta status servizi nel contesto per routing intelligente
    _t = time.time()
    service_status_prompt = service_status.get_status_for_prompt(location)

    # Recupera location disponibili per il prompt
    available_locations = [{"id": loc.id, "name": loc.name, "city": loc.city}
                          for loc in get_all_locations(enabled_only=True)]
    _misc_ms = (time.time() - _t) * 1000

    _ctx_total_ms = (time.time() - _ctx_t0) * 1000
    logger.info(
        f"Context build timing: total={_ctx_total_ms:.0f}ms | "
        f"safety={_safety_ms:.0f}ms memory={_memory_ms:.0f}ms "
        f"save_msg={_save_ms:.0f}ms misc={_misc_ms:.0f}ms"
    )

    # Previous intent per continuità multi-turn
    prev_intent = get_last_intent(speaker_id, max_age_seconds=config.ROUTER_MEMORY_WINDOW_SECONDS) if speaker_id else None

    router_context = {
        **context,
        "memory": router_memory_prompt,
        "history_length": len(recent_turns),
        "service_status": service_status_prompt if service_status_prompt else "tutti i servizi online",
        "available_locations": available_locations,
        "location": location or "unknown"
    }
    if prev_intent:
        router_context["previous_intent"] = prev_intent["intent"]
        router_context["previous_confidence"] = prev_intent["confidence"]
        router_context["previous_payload"] = prev_intent.get("payload", {})

    routing_start = time.time()
    router_data = await get_routing(text, router_context)
    admin_metrics.record_routing((time.time() - routing_start) * 1000)

    intent = router_data.get("intent")
    conf = router_data.get("confidence", 0)
    interim_text = router_data.get("interim_response", "Hmm... ci penso...")

    # Arricchisce la riga utente con il routing per il job notturno di habit extraction.
    # Campi domotica-specifici (entity_id/ha_status) saranno aggiunti dopo l'esecuzione HA.
    try:
        update_chat_meta(user_chat_id, {
            "route": intent,
            "confidence": round(float(conf), 3) if conf is not None else None,
            "payload": router_data.get("payload", {}),
        })
    except Exception as _e:
        logger.debug(f"update_chat_meta(routing) failed: {_e}")

    # Salva intent per continuità multi-turn
    if speaker_id and intent:
        save_last_intent(speaker_id, intent, conf, router_data.get('payload', {}))

    # Recupera soglie confidenza da DB
    conf_high, conf_low = get_confidence_thresholds()

    logger.info(f"Routed: intent={intent}, confidence={conf:.2f}, speaker={speaker_name}, payload={router_data.get('payload', {})}")
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
        domain_raw = payload.get("domain") or None
        action = payload.get("action", "toggle")
        entity_raw = payload.get("entity", "unknown")
        ha_params = payload.get("parameters", {}) or {}

        # Qwen a volte rotta domande informative come HOME_CONTROL
        # Detect: azione non-HA, o testo è una domanda di stato → redirect a SIMPLE_CHAT entity_discover
        _valid_ha_actions = {
            "turn_on", "turn_off", "toggle",
            "open_cover", "close_cover", "stop_cover", "set_cover_position",
            "set_temperature", "set_hvac_mode",
            "volume_set", "volume_up", "volume_down", "media_play", "media_pause",
            "media_stop", "select_source",
            "set_percentage",
            "lock", "unlock",
        }
        _query_patterns = {"quali ", "quante ", "quanti ", "che stato ", "sono acces",
                           "è acces", "sono spen", "è spen", "cosa c'è ", "cosa ce ",
                           "elenco ", "elenca ", "mostrami ", "dimmi "}
        _text_lower = text.lower()
        _is_query = any(p in _text_lower for p in _query_patterns)
        if action not in _valid_ha_actions or _is_query:
            logger.info(f"HOME_CONTROL redirected to entity_discover: action='{action}' is_query={_is_query}")
            _disc_params = {"domain": domain_raw} if domain_raw else {}
            if entity_raw and entity_raw != "unknown":
                # entity potrebbe essere room/zona/piano
                _disc_params["room"] = entity_raw
            _disc_payload = {"api_call": "entity_discover", "params": _disc_params}
            query_response = await _execute_entity_query(_disc_payload, location, context)
            response = query_response or "Non ho trovato dispositivi."
            smart_cache.learn(text, response, "SIMPLE_CHAT")
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")
            return

        # Detect multi-entity/multi-command → redirect to AI Agent (Qwen 3B can't split)
        _entity_str = str(entity_raw) if entity_raw else ""
        _has_multi_entity = "," in _entity_str or (isinstance(entity_raw, list) and len(entity_raw) > 1)
        _has_array_params = any(isinstance(v, list) for v in ha_params.values()) if ha_params else False
        # Detect "verbo + e + verbo" pattern (comandi combinati)
        _domotica_verbs = {"accendi", "spegni", "apri", "chiudi", "alza", "abbassa",
                           "imposta", "metti", "cambia", "attiva", "disattiva"}
        _text_words = text.lower().split()
        _verb_positions = [i for i, w in enumerate(_text_words) if w in _domotica_verbs]
        _has_multi_command = len(_verb_positions) >= 2
        if _has_multi_entity or _has_array_params or _has_multi_command:
            logger.info(f"HOME_CONTROL multi-entity detected, redirecting to AI Agent: entity={entity_raw}")
            if source in config.VOICE_SOURCES:
                await _handle_ai_agent_voice(text, context, hint="")
            else:
                response, _ = await forward_to_ai_agent(text, context)
                if response:
                    save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
                    await deliver_final_response(response, context)
            return

        # Fallback: se Qwen non ha estratto parametri, prova a estrarli dal testo utente
        # Solo per azioni che accettano parametri (non turn_off, close, lock, etc.)
        _no_param_actions = {"turn_off", "close_cover", "lock", "media_stop", "media_pause"}
        if not ha_params and action not in _no_param_actions:
            ha_params = _extract_params_from_text(text)
            if ha_params:
                logger.info(f"Params extracted from user text: {ha_params}")

        # Fix Qwen: se parametri contengono colore/brightness → forza light + turn_on
        _light_hint_keys = {"color", "colour", "colore", "rgb_color", "brightness", "brightness_pct", "color_temp_kelvin"}
        if ha_params and any(k in ha_params for k in _light_hint_keys):
            if domain_raw != "light":
                logger.info(f"Domain fix: '{domain_raw}' → 'light' (params contain light attributes)")
                domain_raw = "light"
            if action not in ("turn_on",):
                logger.info(f"Action fix: '{action}' → 'turn_on' (params require turn_on for light)")
                action = "turn_on"

        # Normalizza domain — Qwen può restituire "light|switch|media_player", lista, o None (tutti)
        VALID_DOMAINS = {"light", "switch", "cover", "climate", "lock", "fan",
                         "media_player", "sensor", "binary_sensor", "camera",
                         "automation", "scene", "script", "input_boolean"}
        if domain_raw is None:
            domain = None
        elif isinstance(domain_raw, list):
            domain = domain_raw[0] if domain_raw else None
        else:
            domain = str(domain_raw).strip().lower()
            # Se contiene pipe o non è un dominio valido HA → None (discovery trova tutto)
            if "|" in domain or domain not in VALID_DOMAINS:
                logger.info(f"Domain '{domain_raw}' non valido o multi-domain, usando discovery senza filtro dominio")
                domain = None

        # Override domain: se l'utente dice "tutto/tutti/tutte" senza tipo specifico → all domains
        # Qwen 3B tende a forzare domain="light" anche per "spegni tutto"
        if domain:
            _all_tokens = {"tutto", "tutti", "tutte", "dispositivi", "ogni cosa"}
            _type_tokens = {"luc", "tapparell", "clima", "volume", "ventilator", "serratur", "interruttor"}
            text_l = text.lower()
            has_all = any(tok in text_l for tok in _all_tokens)
            has_type = any(tok in text_l for tok in _type_tokens)
            if has_all and not has_type:
                logger.info(f"Domain override: '{domain}' → None (user text has wildcard without type)")
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
            if source in config.VOICE_SOURCES:
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
        source_channel = "voice" if source in config.VOICE_SOURCES else source.lower()
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

            # ── COVER "PREFERITO" (Cherubini): premi il button per-tapparella ──
            # Il router manda set_cover_position con position non-numerica ("preferita")
            # o il testo dice "preferito"; la posizione preferita è esposta come
            # button.<cover>_livello_predefinito (componente cherubini_meta).
            _fav_pos = ha_params.get("position")
            _is_cover_fav = (
                (domain_raw == "cover" or (not domain_raw and target["entity_ids"]
                    and all(e.startswith("cover.") for e in target["entity_ids"])))
                and ("preferit" in _text_lower
                     or (_fav_pos is not None and not str(_fav_pos).strip().isdigit()))
            )
            if _is_cover_fav:
                _cover_ids = [e for e in target["entity_ids"] if e.startswith("cover.")]
                _preset_btns = [e.replace("cover.", "button.") + "_livello_predefinito" for e in _cover_ids]
                _ok, _msg = await multi_ha.call_service(
                    target_location, "button", "press", {"entity_id": _preset_btns}
                )
                logger.info(f"Cover favourite: pressed {len(_preset_btns)} preset buttons ok={_ok}")
                _n = len(_preset_btns)
                response = (
                    f"Ho messo {'la tapparella' if _n == 1 else f'le {_n} tapparelle'} alla posizione preferita."
                    if _ok else "Non sono riuscito a impostare la posizione preferita."
                )
                save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
                await deliver_final_response(response, context, sound_type="positive" if _ok else "negative")
                return

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

            # Normalizza parametri Qwen → formato HA
            def _normalize_ha_params(params: dict) -> dict:
                """Converte parametri 'creativi' di Qwen in formato HA standard."""
                if not params:
                    return params
                normalized = dict(params)

                # color/colour name → rgb_color
                COLOR_MAP = {
                    "rosso": [255, 0, 0], "red": [255, 0, 0],
                    "verde": [0, 255, 0], "green": [0, 255, 0],
                    "blu": [0, 0, 255], "blue": [0, 0, 255],
                    "giallo": [255, 255, 0], "yellow": [255, 255, 0],
                    "arancione": [255, 165, 0], "orange": [255, 165, 0],
                    "viola": [128, 0, 128], "purple": [128, 0, 128],
                    "rosa": [255, 105, 180], "pink": [255, 105, 180],
                    "bianco": [255, 255, 255], "white": [255, 255, 255],
                    "ciano": [0, 255, 255], "cyan": [0, 255, 255],
                    "magenta": [255, 0, 255],
                    "caldo": None, "warm": None,  # → color_temp
                    "freddo": None, "cool": None,  # → color_temp
                }
                for key in ("color", "colour", "colore"):
                    if key in normalized:
                        color_val = str(normalized.pop(key)).lower().strip()
                        rgb = COLOR_MAP.get(color_val)
                        if rgb is not None:
                            normalized["rgb_color"] = rgb
                        elif color_val in ("caldo", "warm"):
                            normalized["color_temp_kelvin"] = 2700
                        elif color_val in ("freddo", "cool"):
                            normalized["color_temp_kelvin"] = 6500

                # brightness_pct → brightness (0-255)
                if "brightness_pct" in normalized and "brightness" not in normalized:
                    try:
                        pct = int(normalized.pop("brightness_pct"))
                        normalized["brightness"] = round(pct * 255 / 100)
                    except (ValueError, TypeError):
                        normalized.pop("brightness_pct", None)

                return normalized

            # Sanitizza parametri HA da Qwen (rimuove valori nulli/vuoti)
            def _sanitize_ha_params(params: dict, entity_domain: str) -> dict:
                """Filtra e valida parametri per il dominio."""
                if not params:
                    return {}
                # Whitelist parametri validi per dominio
                VALID_PARAMS = {
                    "light": {"brightness", "color_temp_kelvin", "rgb_color", "transition"},
                    "climate": {"temperature", "hvac_mode"},
                    "cover": {"position"},
                    "media_player": {"volume_level", "is_volume_muted", "media_content_id",
                                     "media_content_type", "source"},
                    "fan": {"percentage", "preset_mode"},
                    "input_number": {"value"},
                    "input_select": {"option"},
                }
                allowed = VALID_PARAMS.get(entity_domain)
                if not allowed:
                    return {}
                return {k: v for k, v in params.items() if k in allowed and v is not None}

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
                    # Passa parametri anche per bulk (es: brightness per tutte le luci)
                    grp_params = _sanitize_ha_params(_normalize_ha_params(ha_params), grp_domain) or None
                    grp_success, grp_err = await call_hass_service_bulk(
                        target_location, grp_domain, grp_action, grp_ids, grp_params
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
                # Aggiungi parametri extra da Qwen (brightness, temperature, position, etc.)
                normalized = _normalize_ha_params(ha_params)
                clean_params = _sanitize_ha_params(normalized, eid_domain)
                if ha_params:
                    logger.info(
                        f"HOME_CONTROL param pipeline: raw={ha_params} → "
                        f"normalized={normalized} → sanitized={clean_params} (domain={eid_domain})"
                    )
                if clean_params:
                    service_data.update(clean_params)
                logger.info(f"HOME_CONTROL call: {eid_domain}.{mapped_action} service_data={service_data}")
                success, err = await call_hass_service(target_location, eid_domain, mapped_action, service_data)
                entity_desc = target["description"]
                log_detail = f"[{target_location}] {mapped_action} su {entity_desc} ({entity_id})" + (f" params={clean_params}" if clean_params else "")

            admin_metrics.record_hass((time.time() - hass_start) * 1000)

            # Arricchisce la riga utente con l'outcome HA per il job notturno di habit extraction.
            try:
                if target["mode"] == "bulk" and len(target["entity_ids"]) > 1:
                    ha_meta = {
                        "ha_mode": "bulk",
                        "ha_entity_ids": list(target["entity_ids"]),
                        "ha_action": action,
                        "ha_params": _normalize_ha_params(ha_params) or {},
                        "ha_status": "ok" if success else ("partial" if total_ok else "error"),
                        "ha_error": err,
                        "ha_location": target_location,
                    }
                else:
                    ha_meta = {
                        "ha_mode": "single",
                        "ha_entity_id": entity_id,
                        "ha_domain": eid_domain,
                        "ha_action": mapped_action,
                        "ha_params": clean_params or {},
                        "ha_status": "ok" if success else "error",
                        "ha_error": err,
                        "ha_location": target_location,
                    }
                update_chat_meta(user_chat_id, ha_meta)
            except Exception as _e:
                logger.debug(f"update_chat_meta(home_control) failed: {_e}")

            if success:
                action_verb = {
                    "turn_on": "acceso", "turn_off": "spento", "toggle": "cambiato",
                    "open_cover": "aperto", "close_cover": "chiuso", "stop_cover": "fermato",
                    "set_temperature": "impostato", "set_hvac_mode": "impostato",
                    "set_cover_position": "posizionato", "volume_set": "impostato",
                    "volume_up": "alzato volume", "volume_down": "abbassato volume",
                    "media_play": "avviato", "media_pause": "messo in pausa",
                    "media_stop": "fermato", "media_next_track": "avanti",
                    "media_previous_track": "indietro", "select_source": "selezionato",
                    "set_percentage": "impostato", "set_value": "impostato",
                    "lock": "bloccato", "unlock": "sbloccato",
                    "trigger": "eseguito",
                }.get(action, action)
                # Usa response del router se disponibile (più naturale)
                router_response = router_data.get("response", "")
                if router_response and ha_params:
                    response = router_response
                elif target["mode"] == "bulk" and len(target["entity_ids"]) > 1:
                    response = f"Fatto! Ho {action_verb} {target['description']}."
                else:
                    response = f"Fatto! {action_verb}: {entity_desc}."
                smart_cache.learn(text, response, intent)
                log_event("HASS", log_detail, speaker_id, speaker_name)
            else:
                response = f"Problema con {entity_desc}: {err}"
                log_event("HARDWARE_ERROR", f"Fallito {log_detail}: {err}", speaker_id, speaker_name)

            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")

            # Quick feedback: suono breve per comandi vocali (voice devices)
            # Solo Telegram riceve TTS completo, o in caso di errore
            quick_feedback_enabled = get_global_preference("ha_quick_feedback", "True") == "True"
            device_cfg = context.get("device_config")
            use_internal_speaker = device_cfg.get("use_internal_speaker", False) if device_cfg else False
            if source in config.VOICE_SOURCES and quick_feedback_enabled:
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
                    # Voice device: feedback rapido
                    device_id = context.get("device_id", "unknown")
                    if use_internal_speaker and device_id != "unknown":
                        # Speaker interno: breve TTS via Opus streaming
                        from internal_tts import speak_to_device
                        from ws_audio_handler import notify_tts_done
                        feedback_text = response if not success else "Fatto!"
                        try:
                            await speak_to_device(feedback_text, device_id)
                        except Exception as e:
                            logger.error(f"Internal speaker feedback failed: {e}")
                        await asyncio.sleep(0.15)
                        await notify_tts_done(device_id)
                    else:
                        # Speaker Alexa della stanza
                        from ws_audio_handler import notify_tts_done
                        room = context.get("room", "salotto").lower()
                        room_speakers = get_room_speakers(target_location)
                        if room_speakers:
                            target_player = room_speakers.get(room) or next(iter(room_speakers.values()), config.DEFAULT_FALLBACK_SPEAKER)
                        else:
                            target_player = config.DEFAULT_FALLBACK_SPEAKER
                        await quick_feedback(success, target_player, err, target_location)
                        await notify_tts_done(device_id)
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

    # --- AI_AGENT (reasoning via AI Agent gateway) ---
    elif intent == "AI_AGENT":
        logger.info("AI_AGENT intent, forwarding to AI Agent")
        if source in config.VOICE_SOURCES:
            # Voice: usa streaming TTS (sentence-by-sentence) per latenza percepita minima
            await _handle_ai_agent_voice(text, context, hint="")
        else:
            # Telegram/altro: non-streaming
            response, _ = await forward_to_ai_agent(text, context)
            log_event("AI_AGENT", f"Domanda: {text[:50]}...", speaker_id, speaker_name)
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")
            # Track follow-up per Telegram
            _tg_key = f"tg_{chat_id}"
            if _needs_followup(response):
                _tg_session = speaker_name.lower() if speaker_name and speaker_name != "Sconosciuto" else f"tg_{chat_id}"
                _ai_agent_followup[_tg_key] = {
                    "session_user": _tg_session,
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "location": location,
                    "context": context,
                    "timestamp": time.time(),
                }
                logger.info(f"🔄 Telegram AI Agent follow-up SET for {_tg_key}")
            else:
                _ai_agent_followup.pop(_tg_key, None)
        return

    # --- VERIFY_WITH_AI_AGENT (confronto risposta precedente) ---
    elif intent == "VERIFY_WITH_AI_AGENT":
        last_exchange = _get_last_qa_from_memory(weighted_memory)
        if not last_exchange:
            response = "Non ricordo cosa ti ho detto. Puoi ripetere la domanda?"
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")
            return

        if source in config.VOICE_SOURCES:
            verify_text = f"Verifica questa risposta: alla domanda '{last_exchange['question']}' ho risposto '{last_exchange['answer']}'. È corretto?"
            await _handle_ai_agent_voice(verify_text, context, hint="verify")
        else:
            await deliver_final_response("Chiedo conferma...", context)
            verify_text = f"Verifica questa risposta: alla domanda '{last_exchange['question']}' ho risposto '{last_exchange['answer']}'. È corretto?"
            response, _ = await forward_to_ai_agent(verify_text, context)
            log_event("VERIFY_AI_AGENT", f"Verifica risposta precedente", speaker_id, speaker_name)
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")
        return

    # --- IMAGE_GENERATION → forwarda a AI Agent (ha Cloud LLM nativo) ---
    elif intent == "IMAGE_GENERATION":
        logger.info("IMAGE_GENERATION intent, forwarding to AI Agent")
        if source in config.VOICE_SOURCES:
            await _handle_ai_agent_voice(text, context, hint="image_generation")
        else:
            response, _ = await forward_to_ai_agent(text, context, hint="image_generation")
            log_event("IMAGE_GEN", f"Richiesta: {text[:50]}...", speaker_id, speaker_name)
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")
        return

    # --- RETRY ---
    elif intent == "RETRY":
        response = router_data.get("response", "Puoi ripetere specificando meglio?")
        save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
        # Richiesta chiarimento con suono neutrale
        await deliver_final_response(response, context, sound_type="neutral")

    # --- LOW CONFIDENCE: forward to AI Agent ---
    elif conf < conf_low:
        logger.info(f"Low confidence ({conf:.2f} < {conf_low}), forwarding to AI Agent")
        if source in config.VOICE_SOURCES:
            await _handle_ai_agent_voice(text, context, hint=f"low_confidence_{intent}")
        else:
            response, _ = await forward_to_ai_agent(text, context, hint=f"low_confidence_{intent}")
            save_chat_message("assistant", response, "JARVIS", None, "Jarvis")
            await deliver_final_response(response, context, sound_type="neutral")

    # --- SIMPLE CHAT / UNKNOWN ---
    else:
        payload = router_data.get("payload", {})
        api_call = payload.get("api_call") if isinstance(payload, dict) else None

        # Auto-upgrade: se Qwen manda entity_discover ma il testo chiede parametri/configurazione
        # di un'entità specifica, promuovi a entity_status
        if api_call == "entity_discover":
            _params = payload.get("params", {})
            # Auto-upgrade SOLO per domande su parametri/configurazione di un'entità
            # specifica — NON per la semplice presenza di `search`, altrimenti OGNI
            # domanda di stato finirebbe sul path entity_status (non-semantico).
            _status_keywords = {"parametr", "impostazion", "configurar", "configurazion",
                                "che può fare", "cosa può fare"}
            _text_lower = text.lower()
            if any(kw in _text_lower for kw in _status_keywords):
                if not _params.get("entity"):
                    _params["entity"] = _params.get("search", "") or ""
                api_call = "entity_status"
                payload["api_call"] = "entity_status"
                logger.info(f"SIMPLE_CHAT: auto-upgraded entity_discover → entity_status (params={_params})")

        # Multi-turn: if router indicated an api_call, execute it for live HA data
        if api_call == "entity_status":
            logger.info(f"SIMPLE_CHAT: executing entity_status with params={payload.get('params', {})}")
            status_response = await _execute_entity_status(payload, location, context)
            if status_response:
                response = await _phrase_ha_data(text, status_response, context, speaker_id, location)
            else:
                response = router_data.get("response") or await get_quick_response(text, context)
        elif api_call in ("entity_discover", "entity_bulk"):
            logger.info(f"SIMPLE_CHAT multi-turn: executing {api_call} with params={payload.get('params', {})}")
            query_response = await _execute_entity_query(payload, location, context)
            if query_response and not query_response.startswith("Non ho trovato"):
                # Frasizza i dati HA in una risposta naturale parlata (no "json")
                response = await _phrase_ha_data(text, query_response, context, speaker_id, location)
            elif query_response:
                response = query_response  # messaggio "non trovato", letto così com'è
            else:
                # Query failed, use router's interim response or quick response
                response = router_data.get("response")
                if not response:
                    response = await get_quick_response(text, context)
        elif api_call in ("web_search", "web_fetch"):
            # Il router ha già estratto la query → esegui ricerca, poi formula risposta
            logger.info(f"SIMPLE_CHAT: {api_call} requested, executing search directly")
            try:
                from web_tools import execute_web_search
                _query = payload.get("params", {}).get("query", text)
                search_results = await execute_web_search(_query)
                # Passa risultati a Qwen per formulare risposta naturale (senza tools)
                response = await get_quick_response(
                    f"L'utente ha chiesto: {text}\n\nRisultati ricerca web:\n{search_results}\n\n"
                    f"Rispondi in modo conciso basandoti sui risultati.",
                    context, user_id=speaker_id, location_id=location, enable_tools=False
                )
            except Exception as e:
                logger.error(f"Web search failed: {e}", exc_info=True)
                response = "Mi dispiace, non sono riuscito a cercare sul web."
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
    4. Speaker locale voice device (se fallback_local_speaker=True)

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

    if is_silent_time and source == "AI Agent":
        await send_telegram(f"🌙 {text}")
        await _immediate_tts_done()
        return

    if source == "Telegram":
        await send_telegram(text)
        await _immediate_tts_done()
        return

    # === FALLBACK CHAIN per voice devices ===

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
                    if _needs_followup(text) and source in config.VOICE_SOURCES and source != "VirtualMic":
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

        # 4. Speaker locale voice device (ultimo fallback)
        if not target_player and fallback_local:
            use_local_speaker = True
            logger.info("All speakers failed, falling back to local device speaker")

    else:
        # Legacy mode - usa room_speakers mapping
        room_speakers = get_room_speakers(location)
        if room_speakers:
            target_player = room_speakers.get(room) or next(iter(room_speakers.values()), config.DEFAULT_FALLBACK_SPEAKER)
        else:
            target_player = config.DEFAULT_FALLBACK_SPEAKER

        # Setta stato SPEAKING prima di parlare (per notificare voice devices)
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
