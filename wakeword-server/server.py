"""jarvis-wakeword-server — FastAPI + WebSocket handler.

Receives continuous Opus audio from voice devices (AtomS3R, NabuVoice),
runs openWakeWord detection, and relays audio sessions to the orchestrator VPS on wake.
"""

import asyncio
import enum
import json
import logging
import time
from typing import Dict, Optional

import httpx
import numpy as np
import opuslib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import (
    DEVICE_API_TOKEN, OPUS_SAMPLE_RATE, OPUS_CHANNELS,
    OPUS_FRAME_SAMPLES, WAKEWORD_THRESHOLD, ORCHESTRATOR_URL, LOCATION_ID,
    HUB_HOST, HUB_PORT, AEC_BACKEND, HUB_VAD_START_SENSITIVITY, HUB_VAD_PREFIX_PADDING_MS,
)
from wakeword import DeviceWakeWordEngine
from multiroom import MultiroomCooldown
from relay import OrchestratorRelay
from hubrelay import HubRelay, HubRejected, HubUnavailable
from livesession import HubLiveSession
from aec import create_echo_canceller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wakeword-server")

app = FastAPI(title="jarvis-wakeword-server")


# ---------------------------------------------------------------------------
# HTTP proxy client (management calls → orchestrator via Tailscale)
# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup_http_client():
    global _http_client
    if ORCHESTRATOR_URL:
        _http_client = httpx.AsyncClient(
            base_url=ORCHESTRATOR_URL,
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        logger.info(f"HTTP proxy client ready → {ORCHESTRATOR_URL}")
    else:
        logger.warning("ORCHESTRATOR_URL not set — HTTP proxy endpoints disabled")


@app.on_event("shutdown")
async def _shutdown_http_client():
    global _http_client
    if _http_client:
        await _http_client.aclose()


# ---------------------------------------------------------------------------
# Device state machine
# ---------------------------------------------------------------------------

class DeviceState(enum.Enum):
    IDLE = "idle"           # Streaming audio, running wake word detection
    WAKING = "waking"       # Wake detected, waiting for device audio_start
    STREAMING = "streaming" # Relaying audio to VPS
    BUSY = "busy"           # VPS processing / TTS playback


class DeviceConnection:
    """Tracks a single device's WS connection and state."""

    def __init__(self, device_id: str, ws: WebSocket):
        self.device_id = device_id
        self.ws = ws
        self.state = DeviceState.IDLE
        self.firmware_version: str = "unknown"
        self.device_type: str = "unknown"  # Determined from hello message fw string
        self.live_session: bool = False  # True during live session
        self.tts_prepare_token = None    # avviso asincrono in corso (prepare_tts)
        self._ws_write_lock = asyncio.Lock()  # Serializza write verso device

        # Opus decoder (16 kHz mono, same as firmware encoder)
        self._opus_decoder = opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)

        # Wake word engine
        self.wakeword = DeviceWakeWordEngine(device_id)
        self.wakeword.init()

        # VPS relay (created on-demand)
        self.relay: Optional[OrchestratorRelay] = None

        # Sessione live full-duplex sul Voice Live Hub (solo tra accept e fine sessione)
        self.hub_session: Optional[HubLiveSession] = None
        # Handshake col hub in corso (connect + offer + attesa accept) e il suo call_id:
        # se viene abbandonato l'orchestrator va comunque avvisato (live_session_end)
        self.hub_start_task: Optional[asyncio.Task] = None
        self.hub_pending_call_id: str = ""

    def decode_opus(self, opus_data: bytes) -> Optional[np.ndarray]:
        """Decode Opus frame to int16 PCM."""
        try:
            pcm_bytes = self._opus_decoder.decode(opus_data, OPUS_FRAME_SAMPLES)
            return np.frombuffer(pcm_bytes, dtype=np.int16)
        except Exception as e:
            logger.warning(f"[{self.device_id}] Opus decode error: {e}")
            return None

    async def send_json(self, data: dict):
        """Send JSON to device."""
        try:
            async with self._ws_write_lock:
                await self.ws.send_text(json.dumps(data))
        except Exception as e:
            logger.error(f"[{self.device_id}] send_json error: {e}")

    async def send_binary(self, data: bytes):
        """Send binary (TTS Opus) to device."""
        try:
            async with self._ws_write_lock:
                await self.ws.send_bytes(data)
        except Exception as e:
            logger.error(f"[{self.device_id}] send_binary error: {e}")

    async def close_relay(self):
        if self.relay:
            await self.relay.close()
            self.relay = None

    def close(self):
        self.wakeword.close()


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_connections: Dict[str, DeviceConnection] = {}
_connections_lock = asyncio.Lock()
_multiroom = MultiroomCooldown()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_token(token: str) -> bool:
    if not DEVICE_API_TOKEN:
        return True  # No token configured = open (dev mode)
    return token == DEVICE_API_TOKEN


# ---------------------------------------------------------------------------
# WebSocket endpoint: /ws/audio
# ---------------------------------------------------------------------------

@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket,
                   device_id: str = Query(...),
                   token: str = Query("")):
    if not _check_token(token):
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    device_id = device_id.upper().strip()

    conn = DeviceConnection(device_id, ws)
    async with _connections_lock:
        old = _connections.pop(device_id, None)
        if old:
            old.close()
        _connections[device_id] = conn
    if old:
        # Riconnessione col vecchio WS non ancora caduto: la sua sessione hub va chiusa
        # subito, non quando il vecchio receive() andra' in errore
        await _teardown_hub(old)

    logger.info(f"[{device_id}] Connected")

    try:
        while True:
            message = await ws.receive()

            if message["type"] == "websocket.disconnect":
                break

            if "text" in message:
                await _handle_text(conn, message["text"])
            elif "bytes" in message:
                await _handle_binary(conn, message["bytes"])

    except WebSocketDisconnect:
        logger.info(f"[{device_id}] Disconnected")
    except Exception as e:
        logger.error(f"[{device_id}] WS error: {e}")
    finally:
        await _teardown_hub(conn)
        await conn.close_relay()
        conn.close()
        async with _connections_lock:
            _connections.pop(device_id, None)
        logger.info(f"[{device_id}] Cleaned up")


async def _handle_text(conn: DeviceConnection, raw: str):
    """Handle JSON control message from device."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return

    msg_type = msg.get("type", "")

    if conn.hub_session is not None and await _handle_hub_device_text(conn, msg_type, msg):
        return
    if conn.hub_start_task is not None and await _handle_hub_pending_device_text(conn, msg_type, msg):
        return

    if msg_type == "hello":
        conn.firmware_version = msg.get("fw", "unknown")
        # Determine device type from firmware version string
        fw_lower = conn.firmware_version.lower()
        if "voicepe" in fw_lower or "nabuvoice" in fw_lower:
            conn.device_type = "NabuVoice"
        else:
            conn.device_type = "AtomS3R"
        logger.info(f"[{conn.device_id}] Hello (fw={conn.firmware_version}, type={conn.device_type})")
        # Send welcome
        await conn.send_json({"type": "welcome",
                              "server_time": int(time.time())})
        # Relay hello to VPS so orchestrator can push config back
        await _ensure_relay_for_hello(conn, raw)

    elif msg_type == "audio_start":
        # Device starting audio session (wake word, button press, or trigger_listen)
        if conn.state in (DeviceState.WAKING, DeviceState.IDLE, DeviceState.BUSY):
            # From IDLE/BUSY: button press or late trigger_listen response — open relay first
            if not conn.relay or not conn.relay.is_connected:
                try:
                    await _open_relay(conn)
                except Exception as e:
                    logger.error(f"[{conn.device_id}] Failed to open relay for audio_start: {e}")
                    return
            conn.state = DeviceState.STREAMING
            logger.info(f"[{conn.device_id}] State → STREAMING")
            # Relay audio_start to VPS
            if conn.relay and conn.relay.is_connected:
                await conn.relay.send_text(raw)

    elif msg_type == "audio_end":
        # User aborted via button
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(raw)
        conn.state = DeviceState.IDLE
        conn.wakeword.reset()
        await conn.close_relay()
        logger.info(f"[{conn.device_id}] State → IDLE (user abort)")

    elif msg_type == "pong":
        pass  # Keepalive

    elif msg_type == "state":
        # Device state update — just relay to VPS if connected
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(raw)

    elif msg_type == "speaker_stop":
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(raw)
        conn.state = DeviceState.IDLE
        conn.wakeword.reset()
        await conn.close_relay()

    elif msg_type == "volume_change":
        # Relay volume change to orchestrator (NabuVoice rotary encoder)
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(raw)
        logger.debug(f"[{conn.device_id}] volume_change relayed")

    else:
        # Unknown message — relay if connected
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(raw)


async def _handle_binary(conn: DeviceConnection, data: bytes):
    """Handle binary Opus frame from device."""
    if conn.hub_session is not None:
        # --- Sessione hub: mic sempre aperto (anche WAKING/BUSY), AEC e poi al hub ---
        pcm = conn.decode_opus(data)
        if pcm is not None and conn.hub_session is not None:
            await conn.hub_session.uplink(pcm)
        return

    if conn.state == DeviceState.IDLE:
        # --- Wake word detection mode ---
        pcm = conn.decode_opus(data)
        if pcm is None:
            return

        # Check multi-room cooldown
        if _multiroom.is_cooled_down(conn.device_id):
            return

        if conn.wakeword.process_audio(pcm):
            await _on_wake_detected(conn)

    elif conn.state == DeviceState.STREAMING:
        # --- Relay mode: pass-through Opus to VPS (no decode needed) ---
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_binary(data)

    # WAKING and BUSY states: drop audio silently


async def _on_wake_detected(conn: DeviceConnection):
    """Handle wake word detection for a device."""
    logger.info(f"[{conn.device_id}] WAKE DETECTED")
    conn.tts_prepare_token = None

    # Multi-room cooldown
    _multiroom.on_wake(conn.device_id)

    # Mute this device's detection until session ends
    conn.wakeword.mute(60.0)  # Long mute; reset on tts_done

    conn.state = DeviceState.WAKING
    logger.info(f"[{conn.device_id}] State → WAKING")

    # Notify device to play beep and prepare
    await conn.send_json({"type": "wake_detected"})

    # Open relay to VPS (the device will send audio_start next)
    try:
        await _open_relay(conn)
    except Exception as e:
        logger.error(f"[{conn.device_id}] Failed to open relay: {e}")
        conn.state = DeviceState.IDLE
        conn.wakeword.reset()


async def _ensure_relay_for_hello(conn: DeviceConnection, hello_raw: str):
    """Open a temporary relay to forward the hello message to VPS.

    The VPS needs the hello to push config back. We open, send hello,
    and keep the connection alive briefly for the config_update response.
    """
    try:
        await _open_relay(conn)
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(hello_raw)
            # The relay recv_loop will forward config_update back to the device.
            # We leave the relay open; it will be reused for the next wake or
            # closed after an inactivity timeout (handled by VPS side).
    except Exception as e:
        logger.warning(f"[{conn.device_id}] Hello relay failed: {e}")


async def _open_relay(conn: DeviceConnection):
    """Open a relay WS to VPS for this device."""
    if conn.relay and conn.relay.is_connected:
        return  # Already open

    async def on_vps_message(message: str | bytes):
        """Forward VPS messages to device."""
        if isinstance(message, str):
            # JSON control message
            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                await conn.send_json({"type": "error", "message": "Invalid VPS response"})
                return

            msg_type = msg.get("type", "")
            if msg_type == "live_session_start" and msg.get("backend") == "hub":
                # Sessione Gemini Live: al device arriva il live_session_start duplex
                # solo dopo l'accept del hub (vedi _start_hub_session)
                await _begin_hub_session(conn, msg)
                return
            if conn.hub_session is not None and msg_type in _HUB_SKIPPED_TURN_MESSAGES:
                # Il turno lo gestisce il hub: messaggi del loop a turni non per l'Atom
                logger.info(f"[{conn.device_id}] {msg_type} from orchestrator ignored (hub session)")
                return
            if msg_type == "live_session_end":
                # Prima si ferma il media del hub (pacer, handshake), poi si avvisa il device
                await _abandon_hub_start(conn)
                await _end_hub_session(conn, send_ended=True)

            # Forward to device
            await conn.send_json(msg)

            # State transitions based on VPS messages
            if msg_type == "ready":
                if conn.state == DeviceState.STREAMING:
                    pass  # Already streaming, ready is confirmation
            elif msg_type == "speech_end":
                conn.state = DeviceState.BUSY
                logger.info(f"[{conn.device_id}] State → BUSY")
            elif msg_type == "live_session_start":
                conn.live_session = True
                conn.wakeword.mute(3600)  # Mute wakeword for entire session
                logger.info(f"[{conn.device_id}] 🎙️ Live session STARTED (wakeword muted)")
            elif msg_type == "live_session_end":
                conn.live_session = False
                conn.wakeword.mute(0)  # Unmute wakeword
                conn.wakeword.reset()
                logger.info(f"[{conn.device_id}] 🎙️ Live session ENDED (wakeword unmuted)")
            elif msg_type == "tts_done":
                conn.tts_prepare_token = None
                conn.state = DeviceState.IDLE
                conn.wakeword.reset()
                if not conn.live_session:
                    conn.wakeword.mute(0)  # Unmute only if not in live session
                    await conn.close_relay()
                    logger.info(f"[{conn.device_id}] State → IDLE (tts_done, relay closed)")
                else:
                    # Live session: keep relay open, wakeword stays muted
                    logger.info(f"[{conn.device_id}] State → IDLE (tts_done, live session — relay kept open)")
            elif msg_type == "trigger_listen":
                # VPS wants multi-turn follow-up (or next live session turn)
                conn.state = DeviceState.WAKING
                logger.info(f"[{conn.device_id}] State → WAKING (trigger_listen)")

        elif isinstance(message, bytes):
            if conn.hub_session is not None:
                return  # In sessione hub il parlato arriva dal hub, non dall'orchestrator
            # Binary TTS Opus — pass through to device
            await conn.send_binary(message)

    conn.relay = OrchestratorRelay(conn.device_id, on_vps_message)
    await conn.relay.connect()


# ---------------------------------------------------------------------------
# Voice Live Hub: sessione live full-duplex (Gemini Live) per l'Atom
# ---------------------------------------------------------------------------
# L'orchestrator manda live_session_start con backend "hub" e l'identita' gia' risolta; qui
# si apre la connessione TCP al hub, e solo dopo l'accept l'Atom entra in modalita' duplex.
# Da quel momento il media va Atom <-> wakeword-server (AEC) <-> hub e l'orchestrator resta
# fuori dal loop audio: riceve solo live_session_end in ingresso con call_id e reason.
# Con HUB_PORT=0 o HUB_HOST vuoto nessuna di queste funzioni apre connessioni e il resto e'
# quello di oggi.

# Messaggi del loop a turni che l'orchestrator puo' ancora mandare durante una sessione hub
# (annunci, avvisi): non arrivano all'Atom in duplex e non cambiano lo stato
_HUB_SKIPPED_TURN_MESSAGES = ("ready", "speech_end", "tts_start", "tts_done", "trigger_listen")
# Reason che l'Atom puo' mettere nel suo live_session_end (passano tali e quali)
_DEVICE_END_REASONS = ("button", "error")


def _hub_address() -> tuple[str, int]:
    """Indirizzo del hub, solo dall'env: HUB_PORT<=0 o HUB_HOST vuoto = funzione spenta."""
    if HUB_PORT <= 0 or not HUB_HOST:
        return "", 0
    return HUB_HOST, HUB_PORT


async def _notify_orchestrator_live_end(conn: DeviceConnection, call_id: str, reason: str,
                                        reopen_relay: bool = True):
    """live_session_end in ingresso all'orchestrator (fine sessione hub lato device)."""
    if not (conn.relay and conn.relay.is_connected):
        if not reopen_relay:
            return
        try:
            await _open_relay(conn)
        except Exception as e:
            logger.warning(f"[{conn.device_id}] live_session_end ({reason}) not delivered: {e}")
            return
    await conn.relay.send_text(json.dumps(
        {"type": "live_session_end", "call_id": call_id, "reason": reason}))


def _hub_start_done(conn: DeviceConnection):
    """Fine dell'handshake (riuscito o no): da qui non va piu' abbandonato."""
    if conn.hub_start_task is asyncio.current_task():
        conn.hub_start_task = None
        conn.hub_pending_call_id = ""


async def _hub_fallback(conn: DeviceConnection, call_id: str, reason: str):
    """Hub non usabile: si riferisce all'orchestrator e basta. E' l'orchestrator a decidere
    se convertire la sessione in quella live di oggi (manda il suo live_session_start) o
    chiudere (live_session_end + tts_done). Intanto il device torna in ascolto, con la
    wake word muta per poco: se l'orchestrator non risponde non resta sordo."""
    _hub_start_done(conn)
    logger.warning(f"[{conn.device_id}] Hub live session not started ({reason}), call {call_id or '-'}")
    conn.state = DeviceState.IDLE
    conn.wakeword.mute(5)
    conn.wakeword.reset()
    await _notify_orchestrator_live_end(conn, call_id, reason)


async def _begin_hub_session(conn: DeviceConnection, msg: dict):
    """live_session_start "hub" dall'orchestrator: l'handshake col hub gira in un task a
    parte, cosi' il relay continua a ricevere (es. un live_session_end che lo annulla)."""
    call_id = str(msg.get("call_id") or "")
    if call_id and (conn.hub_pending_call_id == call_id
                    or (conn.hub_session is not None and conn.hub_session.call_id == call_id)):
        return  # duplicato
    # Un nuovo start sostituisce quello vecchio: l'orchestrator sa gia' che e' finito, l'Atom
    # no (se il nuovo handshake fallisce resterebbe in duplex)
    await _abandon_hub_start(conn)
    await _end_hub_session(conn, send_ended=True, tell_device=True)
    conn.hub_pending_call_id = call_id
    conn.hub_start_task = asyncio.create_task(_start_hub_session(conn, msg))


async def _abandon_hub_start(conn: DeviceConnection, orch_reason: Optional[str] = None,
                             reopen_relay: bool = True) -> bool:
    """Annulla un handshake col hub in corso; con orch_reason avvisa l'orchestrator, che ha
    gia' registrato la sessione hub. True se c'era un handshake da annullare."""
    task = conn.hub_start_task
    if task is None:
        return False
    call_id = conn.hub_pending_call_id
    conn.hub_start_task = None
    conn.hub_pending_call_id = ""
    if not task.done() and task is not asyncio.current_task():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[{conn.device_id}] Hub handshake error on abort: {e!r}")
    logger.info(f"[{conn.device_id}] Hub handshake aborted (call {call_id or '-'}, "
                f"reason {orch_reason or 'orchestrator'})")
    if orch_reason:
        await _notify_orchestrator_live_end(conn, call_id, orch_reason, reopen_relay)
    return True


async def _create_aec():
    """EchoCanceller fuori dall'event loop: il primo import di livekit e la creazione
    dell'APM costano ~100 ms e bloccherebbero gli altri device."""
    fut = asyncio.ensure_future(asyncio.to_thread(create_echo_canceller, AEC_BACKEND))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        # Il thread finisce comunque: l'AEC creato va chiuso, non lasciato appeso
        def _close(f):
            if not f.cancelled() and f.exception() is None:
                f.result().close()
        fut.add_done_callback(_close)
        raise


async def _start_hub_session(conn: DeviceConnection, msg: dict):
    try:
        await _run_hub_handshake(conn, msg)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Mai lasciare l'orchestrator con una sessione hub che il device non ha
        call_id = str(msg.get("call_id") or "")
        logger.error(f"[{conn.device_id}] Hub session start failed: {e!r}")
        if conn.hub_start_task is asyncio.current_task():
            await _hub_fallback(conn, call_id, "hub_unreachable")
        elif conn.hub_session is not None and conn.hub_session.call_id == call_id:
            await _end_hub_session(conn, send_ended=True, tell_device=True, orch_reason="error")
    finally:
        _hub_start_done(conn)


async def _run_hub_handshake(conn: DeviceConnection, msg: dict):
    call_id = str(msg.get("call_id") or "")
    profile = str(msg.get("profile") or "")
    name = str(msg.get("name") or "")
    if not (call_id and profile and name):
        logger.warning(f"[{conn.device_id}] Hub live_session_start without call_id/profile/name")
        await _hub_fallback(conn, call_id, "error")
        return
    host, port = _hub_address()
    if not host or not port:
        await _hub_fallback(conn, call_id, "hub_unreachable")
        return

    offer = {"type": "offer", "call_id": call_id, "profile": profile, "name": name}
    for key in ("voice", "location_id", "room"):
        if msg.get(key):
            offer[key] = str(msg[key])
    if isinstance(msg.get("deliver"), list) and msg["deliver"]:
        offer["deliver"] = [str(d) for d in msg["deliver"]]
    vad = {}
    if HUB_VAD_START_SENSITIVITY:
        vad["startOfSpeechSensitivity"] = HUB_VAD_START_SENSITIVITY
    if HUB_VAD_PREFIX_PADDING_MS > 0:
        vad["prefixPaddingMs"] = HUB_VAD_PREFIX_PADDING_MS
    if vad:
        offer["vad"] = vad

    relay = HubRelay(conn.device_id, call_id)
    try:
        await relay.connect(host, port)
        await relay.send_offer(offer)
        aec = await _create_aec()
    except HubRejected as e:
        await relay.close()
        logger.warning(f"[{conn.device_id}] Hub rejected call {call_id}: {e}")
        await _hub_fallback(conn, call_id, "hub_rejected")
        return
    except HubUnavailable as e:
        await relay.close()
        logger.warning(f"[{conn.device_id}] Hub unavailable for call {call_id}: {e}")
        await _hub_fallback(conn, call_id, "hub_unreachable")
        return
    except asyncio.CancelledError:
        # Handshake abbandonato (device disconnesso, tap, fine dall'orchestrator):
        # niente socket appesi; l'orchestrator lo avvisa chi ha annullato
        await relay.close()
        raise
    except Exception as e:
        await relay.close()
        logger.warning(f"[{conn.device_id}] Hub handshake failed for call {call_id}: {e!r}")
        await _hub_fallback(conn, call_id, "hub_unreachable")
        return

    try:
        session = HubLiveSession(conn.device_id, call_id, relay, conn.send_json,
                                 conn.send_binary, AEC_BACKEND, aec=aec)
    except Exception:
        aec.close()
        await relay.send_json({"type": "ended", "call_id": call_id})
        await relay.close()
        raise
    # Accept ricevuto: da qui la sessione e' di conn e si chiude con _end_hub_session
    _hub_start_done(conn)
    conn.hub_session = session
    conn.live_session = True
    conn.state = DeviceState.BUSY
    conn.wakeword.mute(3600)
    # Prima il device in duplex, poi il media: il hub puo' parlare subito
    await conn.send_json({"type": "live_session_start", "duplex": True})
    if conn.hub_session is not session:
        return  # chiusa durante l'invio (device sparito, tap, orchestrator)

    async def on_hub_json(m: dict):
        if conn.hub_session is not session:
            return
        mtype = m.get("type")
        if mtype == "clear":
            await session.clear()
        elif mtype == "hangup":
            logger.info(f"[{conn.device_id}] Hub hangup (call {call_id})")
            await _end_hub_session(conn, tell_device=True, orch_reason="hub_hangup")

    async def on_hub_closed():
        if conn.hub_session is session:
            await _end_hub_session(conn, tell_device=True, orch_reason="error")

    relay.start(on_hub_json, session.on_hub_audio, on_hub_closed)
    session.start()
    logger.info(f"[{conn.device_id}] 🎙️ Hub live session STARTED (call {call_id}, profile {profile})")


async def _end_hub_session(conn: DeviceConnection, *, send_ended: bool = False,
                           tell_device: bool = False, orch_reason: Optional[str] = None,
                           reopen_relay: bool = True):
    """Chiude la sessione hub (idempotente) e rimette il device in ascolto wake word."""
    session = conn.hub_session
    if session is None:
        return
    conn.hub_session = None
    try:
        await session.close(send_ended=send_ended)
    except Exception as e:  # il reset dello stato e gli avvisi sotto devono girare comunque
        logger.warning(f"[{conn.device_id}] Hub session close error: {e!r}")
    conn.live_session = False
    conn.state = DeviceState.IDLE
    conn.wakeword.mute(0)
    conn.wakeword.reset()
    if tell_device:
        await conn.send_json({"type": "live_session_end"})
    if orch_reason:
        await _notify_orchestrator_live_end(conn, session.call_id, orch_reason, reopen_relay)
    logger.info(f"[{conn.device_id}] 🎙️ Hub live session ENDED (call {session.call_id}, "
                f"reason {orch_reason or 'orchestrator'})")


async def _teardown_hub(conn: DeviceConnection):
    """Device sparito (WS chiuso o sostituito): handshake o sessione hub chiusi, l'orchestrator
    avvisato con reason "error" se il relay c'e' ancora. Non deve mai sollevare: dopo c'e'
    la pulizia del relay e della connessione."""
    try:
        await _abandon_hub_start(conn, "error", reopen_relay=False)
        await _end_hub_session(conn, send_ended=True, orch_reason="error", reopen_relay=False)
    except Exception as e:
        logger.warning(f"[{conn.device_id}] Hub teardown error: {e!r}")


def _device_end_reason(msg: dict) -> str:
    reason = msg.get("reason")
    return reason if reason in _DEVICE_END_REASONS else "button"


async def _handle_hub_device_text(conn: DeviceConnection, msg_type: str, msg: dict) -> bool:
    """Messaggi del device durante una sessione hub. True = gestito qui."""
    if msg_type == "live_session_end":
        # Tap singolo ("button") o duplex non avviabile sull'Atom ("error")
        await _end_hub_session(conn, send_ended=True, orch_reason=_device_end_reason(msg))
        return True
    if msg_type == "audio_end":
        # Ripiego per firmware che non mandano live_session_end: il relay non va chiuso
        await _end_hub_session(conn, send_ended=True, orch_reason="button")
        return True
    if msg_type == "speaker_stop":
        # Doppio tap = fine sessione come il tap singolo. Lo speaker_stop non va inoltrato
        # e il relay resta aperto: l'orchestrator deve ricevere il live_session_end
        await _end_hub_session(conn, send_ended=True, tell_device=True, orch_reason="button")
        return True
    if msg_type == "audio_start":
        # Un Atom in duplex non apre turni: se lo fa non e' piu' nella sessione (desync).
        # Si chiude il hub senza avvisare l'orchestrator (il suo live_session_end + tts_done
        # chiuderebbe il turno appena aperto): quando il turno gli arriva, chiude lui la
        # sessione hub e lo passa alla pipeline normale
        logger.warning(f"[{conn.device_id}] audio_start during hub session: device left it")
        await _end_hub_session(conn, send_ended=True)
        return False
    return False


async def _handle_hub_pending_device_text(conn: DeviceConnection, msg_type: str, msg: dict) -> bool:
    """Messaggi del device durante l'handshake col hub. True = gestito qui."""
    if msg_type == "live_session_end":
        await _abandon_hub_start(conn, _device_end_reason(msg))
        return True
    if msg_type in ("audio_end", "speaker_stop"):
        # Il device lascia perdere prima del duplex: l'orchestrator viene avvisato prima
        # dello speaker_stop / audio_end di oggi, che poi chiude il relay
        await _abandon_hub_start(conn, "button")
        return False
    if msg_type == "audio_start":
        # Turno nuovo: come in sessione, niente avviso (ci pensa l'orchestrator col turno)
        await _abandon_hub_start(conn)
        return False
    return False


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    wake_word_threshold: float = Field(ge=0.1, le=1.0)


@app.post("/api/config/{device_id}")
async def update_device_config(device_id: str, body: ConfigUpdate):
    """Push wake_word_threshold from orchestrator to a connected device."""
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _connections.get(device_id)

    if not conn:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not connected")

    conn.wakeword.set_threshold(body.wake_word_threshold)
    return {"status": "ok", "device_id": device_id,
            "wake_word_threshold": conn.wakeword.threshold}


@app.post("/api/trigger_listen/{device_id}")
async def trigger_listen(device_id: str, silent: bool = True):
    """Relay trigger_listen to a device (for enrollment from dashboard)."""
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _connections.get(device_id)

    if not conn:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not connected")
    if conn.hub_session is not None:
        raise HTTPException(status_code=409, detail="Device in hub live session")

    # Open relay before sending trigger_listen — device will respond with audio_start
    try:
        await _open_relay(conn)
    except Exception as e:
        logger.warning(f"[{device_id}] trigger_listen relay open failed: {e}")

    conn.state = DeviceState.WAKING
    conn.wakeword.mute(60.0)
    await conn.send_json({"type": "trigger_listen", "silent": silent})
    logger.info(f"[{device_id}] trigger_listen relayed (silent={silent})")
    return {"status": "ok", "device_id": device_id}


# Avvisi asincroni (timer, sveglie, monitor di Hermes): il device e' IDLE e senza
# relay, ma l'orchestrator deve fargli dire qualcosa. Si apre il relay come per una
# wake word, stato BUSY e wake word muta (la voce dell'avviso non deve risvegliarlo)
# finche' l'orchestrator non manda tts_done. Se tts_done non arriva, il watchdog
# rimette il device in ascolto: un avviso perso non deve lasciarlo sordo.
_TTS_PREPARE_TIMEOUT_S = 90.0


@app.post("/api/prepare_tts/{device_id}")
async def prepare_tts(device_id: str, request: Request):
    if DEVICE_API_TOKEN and request.headers.get("Authorization", "") != f"Bearer {DEVICE_API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _connections.get(device_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not connected")
    if conn.state != DeviceState.IDLE or conn.live_session:
        raise HTTPException(status_code=409, detail=f"Device busy ({conn.state.value})")
    try:
        await _open_relay(conn)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Relay open failed: {e}")
    conn.state = DeviceState.BUSY
    conn.wakeword.mute(_TTS_PREPARE_TIMEOUT_S)
    token = object()
    conn.tts_prepare_token = token

    async def _watchdog():
        await asyncio.sleep(_TTS_PREPARE_TIMEOUT_S)
        if getattr(conn, "tts_prepare_token", None) is token and conn.state == DeviceState.BUSY \
                and not conn.live_session:
            conn.tts_prepare_token = None
            conn.state = DeviceState.IDLE
            conn.wakeword.mute(0)
            conn.wakeword.reset()
            await conn.close_relay()
            logger.warning(f"[{device_id}] prepare_tts: nessun tts_done in "
                           f"{_TTS_PREPARE_TIMEOUT_S:.0f}s, torno IDLE")

    asyncio.create_task(_watchdog())
    logger.info(f"[{device_id}] prepare_tts: relay aperto, State → BUSY (avviso)")
    return {"status": "ok", "device_id": device_id}


@app.get("/health")
async def health():
    """Health check."""
    async with _connections_lock:
        connected = list(_connections.keys())
    return {"status": "healthy", "connected_devices": len(connected),
            "devices": connected}


@app.get("/api/devices")
async def list_devices():
    """List connected devices and their state."""
    async with _connections_lock:
        result = []
        for did, conn in _connections.items():
            result.append({
                "device_id": did,
                "state": conn.state.value,
                "firmware": conn.firmware_version,
                "device_type": conn.device_type,
                "threshold": conn.wakeword.threshold,
                "relay_connected": conn.relay.is_connected if conn.relay else False,
            })
    return result


# ---------------------------------------------------------------------------
# HTTP Proxy: Device management calls → Orchestrator via Tailscale
# ---------------------------------------------------------------------------
# These endpoints allow voice devices (AtomS3R, NabuVoice) to reach the orchestrator through
# the wakeword-server (LAN) instead of directly via the public internet.
# Bearer tokens never leave the LAN + Tailscale encrypted tunnel.
# ---------------------------------------------------------------------------

async def _proxy_request(request: Request, path: str,
                         extra_query: Optional[Dict[str, str]] = None) -> JSONResponse:
    """Forward an HTTP request to the orchestrator and return the response.

    extra_query: params injected into the forwarded query string unless the
    device already provided them (lets a single-home wakeword-server scope
    requests, e.g. location_id, that the firmware does not send).
    """
    if not _http_client:
        return JSONResponse(
            status_code=503,
            content={"error": "Orchestrator URL not configured"},
        )

    # Validate device bearer token
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:] if auth_header.lower().startswith("bearer ") else auth_header
    if not _check_token(token):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    # Build target URL preserving query string, injecting extra_query defaults
    from urllib.parse import urlencode, parse_qs
    params = {k: v[0] for k, v in parse_qs(str(request.url.query)).items()}
    if extra_query:
        for k, v in extra_query.items():
            if v and k not in params:
                params[k] = v
    query_string = urlencode(params)
    target_url = f"{path}?{query_string}" if query_string else path

    # Read body for POST/PUT/PATCH
    body = None
    content_type = request.headers.get("content-type", "")
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    # Forward auth + content-type headers
    forward_headers = {}
    if auth_header:
        forward_headers["Authorization"] = auth_header
    if content_type:
        forward_headers["Content-Type"] = content_type

    try:
        resp = await _http_client.request(
            method=request.method,
            url=target_url,
            content=body,
            headers=forward_headers,
        )
        # Return orchestrator response as-is
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception:
            return JSONResponse(status_code=resp.status_code,
                                content={"raw": resp.text})
    except httpx.TimeoutException:
        logger.error(f"Proxy timeout: {request.method} {path}")
        return JSONResponse(status_code=504, content={"error": "Orchestrator timeout"})
    except Exception as e:
        logger.error(f"Proxy error: {request.method} {path} → {e}")
        return JSONResponse(status_code=502, content={"error": f"Proxy error: {e}"})


@app.get("/device_config")
async def proxy_device_config(request: Request):
    """Proxy device config fetch to orchestrator."""
    return await _proxy_request(request, "/device_config")


@app.post("/heartbeat")
async def proxy_heartbeat(request: Request):
    """Proxy device heartbeat to orchestrator."""
    return await _proxy_request(request, "/heartbeat")


@app.get("/room_temperature/{room:path}")
async def proxy_room_temperature(room: str, request: Request):
    """Proxy room temperature fetch to orchestrator.

    Inject this home's location_id (firmware does not send it) so the
    orchestrator scopes the lookup to the right home instead of returning
    the first location's weather fallback.
    """
    return await _proxy_request(
        request, f"/room_temperature/{room}",
        extra_query={"location_id": LOCATION_ID},
    )


@app.get("/device_status")
async def proxy_device_status_get(request: Request):
    """Proxy device status query to orchestrator."""
    return await _proxy_request(request, "/device_status")


@app.post("/device_status")
async def proxy_device_status_post(request: Request):
    """Proxy device status update to orchestrator."""
    return await _proxy_request(request, "/device_status")


@app.post("/speaker/suppress")
async def proxy_speaker_suppress(request: Request):
    """Proxy speaker suppress to orchestrator."""
    return await _proxy_request(request, "/speaker/suppress")


@app.post("/speaker/restore")
async def proxy_speaker_restore(request: Request):
    """Proxy speaker restore to orchestrator."""
    return await _proxy_request(request, "/speaker/restore")


@app.post("/speaker/volume_change")
async def proxy_speaker_volume_change(request: Request):
    """Proxy volume change to orchestrator (NabuVoice rotary encoder)."""
    return await _proxy_request(request, "/speaker/volume_change")
