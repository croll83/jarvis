"""jarvis-wakeword-server — FastAPI + WebSocket handler.

Receives continuous Opus audio from voice devices (AtomS3R, NabuVoice),
runs openWakeWord detection, and relays audio sessions to the orchestrator VPS on wake.
"""

import asyncio
import base64
import enum
import json
import logging
import time
import uuid
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
    HUB_ATOM_MIC_GAIN_DB, HUB_AEC_REF_MODE, HUB_ONBOARD_GAIN_DB,
    WAKE_PREROLL_S, WAKE_PREROLL_SHADOW, WAKE_CLIP_ARCHIVE, WAKE_CLIP_DIR, WAKE_CLIP_MAX_FILES,
    WAKE_CLIP_MAX_MB, WAKE_VOICE_GATE, WAKE_VOICE_GATE_TIMEOUT_S, LIVE_ON_WAKE,
    LIVE_ACK_TIMEOUT_S, LIVE_START_TIMEOUT_S, LIVE_ACCEPT_TIMEOUT_S, LIVE_COMMAND_MAX_S,
    LIVE_COMMAND_MIN_SPEECH_MS, LIVE_COMMAND_MARGIN_DB, LIVE_COMMAND_MIN_DBFS,
    LIVE_COMMAND_CHIRP_SKIP_MS,
)
from wakeword import DeviceWakeWordEngine
from multiroom import MultiroomCooldown
from relay import OrchestratorRelay
from hubrelay import HubRelay, HubRejected, HubUnavailable
from livesession import HubLiveSession, parse_aligned, parse_level
from aec import create_echo_canceller
from preroll import ClipArchive, PcmRing, speech_ms

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
    LIVE_PENDING = "live_pending"  # Wake (o tap) in attesa: cancello a voce, ack, live_start


class DeviceConnection:
    """Tracks a single device's WS connection and state."""

    def __init__(self, device_id: str, ws: WebSocket):
        self.device_id = device_id
        self.ws = ws
        self.state = DeviceState.IDLE
        self.firmware_version: str = "unknown"
        self.device_type: str = "unknown"  # Determined from hello message fw string
        self.caps: set[str] = set()      # Dall'hello; "aec_ref" = riferimento AEC dal codec,
                                         # "aec_onboard" = AEC gia' fatto sul device (NabuVoice)
        # Fine sessione hub di un device con PCM in uplink: fino a qui i frame binari fuori
        # sessione sono PCM ancora in volo, non Opus da decodificare per la wake word
        self.pcm_tail_until: float = 0.0
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

        # Fase 3 (design 19): pre-roll della wake word, flag dal welcome dell'orchestrator
        # (in cache finche' resta connesso) e la wake/tap in LIVE_PENDING
        self.preroll_ring = PcmRing(WAKE_PREROLL_S)
        self.live_on_wake: bool = False
        self.wake_voice_gate: bool = False
        self.wake_pending: Optional["WakePending"] = None
        # Apertura del relay in corso: chi arriva dopo aspetta questa invece di aprirne un'altra
        self.relay_opening: Optional[asyncio.Task] = None
        self.closed = False  # close() fatto: connessione sostituita o WS caduto

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
        self.closed = True
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
    if conn.wake_pending is not None and await _handle_live_pending_device_text(conn, msg_type, msg):
        return
    if conn.hub_start_task is not None and await _handle_hub_pending_device_text(conn, msg_type, msg):
        return

    if msg_type == "hello":
        conn.firmware_version = msg.get("fw", "unknown")
        caps = msg.get("caps")
        conn.caps = {str(c) for c in caps} if isinstance(caps, list) else set()
        # Determine device type from firmware version string
        fw_lower = conn.firmware_version.lower()
        if "voicepe" in fw_lower or "nabuvoice" in fw_lower:
            conn.device_type = "NabuVoice"
        else:
            conn.device_type = "AtomS3R"
        logger.info(f"[{conn.device_id}] Hello (fw={conn.firmware_version}, type={conn.device_type}"
                    f"{', caps=' + ','.join(sorted(conn.caps)) if conn.caps else ''})")
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

    elif msg_type == "live_start":
        # Richiesta live dal device: wake rilevata in locale (NabuVoice, con pre-roll) o tap
        await _on_device_live_start(conn, msg)

    elif msg_type == "live_wake_ack":
        # Ack arrivato dopo che il LIVE_PENDING si e' chiuso (timeout): non riguarda piu' nessuno
        logger.info(f"[{conn.device_id}] Stale live_wake_ack (req {msg.get('req_id')}) ignored")

    else:
        # Unknown message — relay if connected
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(raw)


async def _handle_binary(conn: DeviceConnection, data: bytes):
    """Handle binary Opus frame from device."""
    if conn.hub_session is not None:
        # --- Sessione hub: mic sempre aperto (anche WAKING/BUSY), AEC e poi al hub ---
        if _aec_onboard(conn):
            # NabuVoice: PCM s16le 16 kHz gia' ripulito dall'XMOS. Prima di parse_level/aligned:
            # un frame PCM puo' iniziare con 0xA5 per caso
            await conn.hub_session.uplink_pcm(data)
            return
        level = parse_level(data)
        if level is not None:
            seq, rms, opus, muted = level
            pcm = conn.decode_opus(opus)
            if pcm is not None and conn.hub_session is not None:
                await conn.hub_session.uplink_level(seq, rms, pcm, muted)
            return
        aligned = parse_aligned(data)
        if aligned is not None:
            await conn.hub_session.uplink_aligned(*aligned)
            return
        pcm = conn.decode_opus(data)
        if pcm is not None and conn.hub_session is not None:
            await conn.hub_session.uplink(pcm)
        return

    if conn.state == DeviceState.IDLE:
        # --- Wake word detection mode ---
        if conn.pcm_tail_until and time.monotonic() < conn.pcm_tail_until:
            return  # coda PCM della sessione hub appena chiusa (il device non ha ancora smesso)
        pcm = conn.decode_opus(data)
        if pcm is None:
            return

        # Ring del pre-roll: prima del cooldown multiroom, che ritorna presto
        conn.preroll_ring.append(pcm)

        # Check multi-room cooldown
        if _multiroom.is_cooled_down(conn.device_id):
            return

        if conn.wakeword.process_audio(pcm):
            await _on_wake_detected(conn)

    elif conn.state == DeviceState.LIVE_PENDING:
        # --- Buffer del comando (o niente, in attesa del cancello a voce) ---
        _pending_audio(conn, data)

    elif conn.state == DeviceState.STREAMING:
        # --- Relay mode: pass-through Opus to VPS (no decode needed) ---
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_binary(data)

    # WAKING and BUSY states: drop audio silently


async def _on_wake_detected(conn: DeviceConnection):
    """Handle wake word detection for a device."""
    logger.info(f"[{conn.device_id}] WAKE DETECTED")
    conn.tts_prepare_token = None

    # Pre-roll: finisce sul rilevamento e contiene la wake word. Il ring si riempie solo in
    # IDLE, quindi il chirp e il resto dopo il rilevamento non ci entrano
    preroll = conn.preroll_ring.snapshot()
    score = float(getattr(conn.wakeword, "last_score", 0.0) or 0.0)
    _archive_clip(conn, preroll, score)

    # Multi-room cooldown
    _multiroom.on_wake(conn.device_id)

    # Mute this device's detection until session ends
    conn.wakeword.mute(60.0)  # Long mute; reset on tts_done

    gate = _voice_gate_on(conn) and bool(preroll)
    live = _live_on_server_wake(conn)
    if gate or live:
        # Fase 3: prima il cancello a voce, poi il live (o il classico) in LIVE_PENDING
        pend = WakePending(_new_req_id(), "wake", device_initiated=False, preroll=preroll,
                           score=score, live=live)
        conn.wake_pending = pend
        conn.state = DeviceState.LIVE_PENDING
        logger.info(f"[{conn.device_id}] State → LIVE_PENDING (req {pend.req_id}, "
                    f"gate={gate}, live={live})")
        if gate:
            pend.task = asyncio.create_task(_gate_flow(conn, pend))
        else:
            await _live_offer_to_device(conn, pend)
        return

    if await _classic_wake(conn) and WAKE_PREROLL_SHADOW and preroll:
        # Shadow: voice ID sul pre-roll lato orchestrator, prima dell'audio_start del device
        await _send_wake_preroll(conn, _new_req_id(), preroll, score, gate=False)


async def _classic_wake(conn: DeviceConnection) -> bool:
    """Il turno classico di oggi: wake_detected al device e relay aperto. False se il relay
    non si apre (device di nuovo in IDLE)."""
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
        return False
    return True


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
    task = conn.relay_opening
    if task is None or task.done():
        # Una sola apertura alla volta, condivisa da chi arriva dopo (cancello a voce,
        # LIVE_PENDING, tap): un secondo relay resterebbe orfano. Il task non appartiene a chi
        # l'ha avviato: se quello viene annullato (gesto del device durante il cancello)
        # l'apertura continua per gli altri, che altrimenti riceverebbero un CancelledError
        # nel loop WS del device e lo farebbero cadere.
        task = asyncio.ensure_future(_connect_relay(conn))
        conn.relay_opening = task

        def _opened(t: asyncio.Task, conn=conn):
            if conn.relay_opening is t:
                conn.relay_opening = None
            if not t.cancelled():
                t.exception()  # recuperata: nessun "exception was never retrieved"
        task.add_done_callback(_opened)
    await asyncio.shield(task)


async def _connect_relay(conn: DeviceConnection):
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
            if msg_type in ("welcome", "live_on_wake"):
                # welcome a ogni apertura del relay; live_on_wake quando il toggle cambia in
                # admin (admin_api._apply_live_on_wake). E poi al device come oggi
                _read_welcome_flags(conn, msg)
            elif msg_type in _LIVE_ORCH_MESSAGES:
                await _handle_live_orch_message(conn, msg_type, msg)
                return
            if conn.wake_pending is not None and await _handle_live_pending_orch_message(conn, msg_type, msg):
                return
            if msg_type == "live_session_start" and msg.get("req_id") and conn.wake_pending is None:
                # Risposta a una richiesta gia' chiusa (timeout, tap): l'orchestrator ha avuto
                # live_cancel, il hub non si apre
                logger.info(f"[{conn.device_id}] Stale live_session_start (req {msg.get('req_id')}) ignored")
                return
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
            if conn.wake_pending is not None:
                return  # In LIVE_PENDING nessun turno parla sul device
            # Binary TTS Opus — pass through to device
            await conn.send_binary(message)

    relay = OrchestratorRelay(conn.device_id, on_vps_message)
    conn.relay = relay
    await relay.connect()
    if conn.closed:
        # Device sparito durante l'apertura (che chi l'ha chiesta non puo' piu' annullare):
        # niente relay orfano verso l'orchestrator
        await relay.close()
        if conn.relay is relay:
            conn.relay = None


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
                                        reopen_relay: bool = True, detail: Optional[str] = None):
    """live_session_end in ingresso all'orchestrator (fine sessione hub lato device). detail
    (solo per le sessioni con origine wake o tap) = motivo del rejected del hub."""
    if not (conn.relay and conn.relay.is_connected):
        if not reopen_relay:
            return
        try:
            await _open_relay(conn)
        except Exception as e:
            logger.warning(f"[{conn.device_id}] live_session_end ({reason}) not delivered: {e}")
            return
    msg = {"type": "live_session_end", "call_id": call_id, "reason": reason}
    if detail:
        msg["detail"] = detail
    await conn.relay.send_text(json.dumps(msg))


def _hub_start_done(conn: DeviceConnection):
    """Fine dell'handshake (riuscito o no): da qui non va piu' abbandonato."""
    if conn.hub_start_task is asyncio.current_task():
        conn.hub_start_task = None
        conn.hub_pending_call_id = ""


async def _hub_fallback(conn: DeviceConnection, call_id: str, reason: str,
                        pend: Optional["WakePending"] = None, detail: Optional[str] = None):
    """Hub non usabile: si riferisce all'orchestrator e basta. E' l'orchestrator a decidere
    se convertire la sessione in quella live di oggi (manda il suo live_session_start) o
    chiudere (live_session_end + tts_done). Intanto il device torna in ascolto, con la
    wake word muta per poco: se l'orchestrator non risponde non resta sordo.

    Con origine wake o tap (pend) invece l'orchestrator non converte e non risponde: qui si
    passa subito al ripiego classico (design 19.3, punto 5)."""
    _hub_start_done(conn)
    if pend is not None:
        logger.warning(f"[{conn.device_id}] Hub live session not started ({reason}"
                       f"{', ' + detail if detail else ''}), call {call_id or '-'}, "
                       f"req {pend.req_id}: classic fallback")
        await _notify_orchestrator_live_end(conn, call_id, reason, detail=detail)
        await _live_pending_exit(conn, pend, "classic", reason=reason)
        return
    logger.warning(f"[{conn.device_id}] Hub live session not started ({reason}), call {call_id or '-'}")
    conn.state = DeviceState.IDLE
    conn.wakeword.mute(5)
    conn.wakeword.reset()
    await _notify_orchestrator_live_end(conn, call_id, reason)


async def _begin_hub_session(conn: DeviceConnection, msg: dict,
                             pend: Optional["WakePending"] = None):
    """live_session_start "hub" dall'orchestrator: l'handshake col hub gira in un task a
    parte, cosi' il relay continua a ricevere (es. un live_session_end che lo annulla).
    pend = la wake o il tap in LIVE_PENDING a cui l'orchestrator ha risposto (origine
    wake/tap), None per il percorso "avvia una sessione live" di oggi."""
    call_id = str(msg.get("call_id") or "")
    if call_id and (conn.hub_pending_call_id == call_id
                    or (conn.hub_session is not None and conn.hub_session.call_id == call_id)):
        return  # duplicato
    # Un nuovo start sostituisce quello vecchio: l'orchestrator sa gia' che e' finito, l'Atom
    # no (se il nuovo handshake fallisce resterebbe in duplex)
    await _abandon_hub_start(conn)
    await _end_hub_session(conn, send_ended=True, tell_device=True)
    conn.hub_pending_call_id = call_id
    conn.hub_start_task = asyncio.create_task(_start_hub_session(conn, msg, pend))


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


def _fw_version(fw: str) -> Optional[tuple]:
    try:
        return tuple(int(p) for p in fw.split("-", 1)[0].split("."))
    except ValueError:
        return None


def _duplex_capable(conn: DeviceConnection) -> bool:
    """Il device sa fare la sessione live full-duplex: NabuVoice solo se lo dichiara (cap duplex),
    Atom dal 5.1.0-ws (i 5.2+ dichiarano aec_ref). Versione illeggibile = come oggi."""
    if "duplex" in conn.caps or "aec_ref" in conn.caps:
        return True
    if conn.device_type == "NabuVoice":
        return False
    version = _fw_version(conn.firmware_version)
    return version is None or version >= (5, 1)


def _aec_onboard(conn: DeviceConnection) -> bool:
    """Device che fa da se' AEC+NS+AGC (NabuVoice, XMOS XU316): in sessione hub manda PCM
    16 kHz gia' ripulito e il server non ci mette ne' AEC, ne' cancello, ne' AGC."""
    return "aec_onboard" in conn.caps


# Dopo la fine di una sessione hub con PCM in uplink, per quanto si scartano i frame binari in
# IDLE: il device smette di mandare PCM solo quando riceve il live_session_end
_PCM_TAIL_S = 1.5


async def _create_aec(agc: bool = True, backend: Optional[str] = None):
    """EchoCanceller fuori dall'event loop: il primo import di livekit e la creazione
    dell'APM costano ~100 ms e bloccherebbero gli altri device."""
    fut = asyncio.ensure_future(asyncio.to_thread(create_echo_canceller, backend or AEC_BACKEND, agc))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        # Il thread finisce comunque: l'AEC creato va chiuso, non lasciato appeso
        def _close(f):
            if not f.cancelled() and f.exception() is None:
                f.result().close()
        fut.add_done_callback(_close)
        raise


def _drop_aec_task(task: Optional[asyncio.Task]):
    """AEC creato in parallelo all'handshake che non serve piu': fermato o chiuso."""
    if task is None:
        return
    if not task.done():
        task.cancel()  # _create_aec chiude da se' l'AEC del thread che finisce dopo
    elif not task.cancelled() and task.exception() is None:
        task.result().close()


# Scala ADC del firmware Atom fuori dal duplex (JARVIS_CODEC_MIC_SCALE_DB)
_ATOM_MIC_SCALE_DB = 30


def _mic_gain_db() -> Optional[int]:
    try:
        return int(HUB_ATOM_MIC_GAIN_DB) if HUB_ATOM_MIC_GAIN_DB else None
    except ValueError:
        logger.warning(f"HUB_ATOM_MIC_GAIN_DB={HUB_ATOM_MIC_GAIN_DB!r} non numerico, ignorato")
        return None


def _duplex_start_msg(conn: DeviceConnection) -> dict:
    """live_session_start per il device; ai firmware con la cap "aec_ref" si chiede il
    riferimento AEC dal loopback del codec (ed eventualmente un guadagno mic per il duplex).
    Con "aec_onboard" (NabuVoice) niente riferimento: l'AEC lo fa il device."""
    start = {"type": "live_session_start", "duplex": True}
    if _aec_onboard(conn):
        return start
    if "aec_ref" in conn.caps and HUB_AEC_REF_MODE == "level":
        start["aec_ref"] = "level"
    elif "aec_ref" in conn.caps:
        start["aec_ref"] = True
        gain = _mic_gain_db()
        if gain is not None:
            start["mic_gain_db"] = gain
    return start


async def _start_hub_session(conn: DeviceConnection, msg: dict,
                             pend: Optional["WakePending"] = None):
    try:
        await _run_hub_handshake(conn, msg, pend)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Mai lasciare l'orchestrator con una sessione hub che il device non ha
        call_id = str(msg.get("call_id") or "")
        logger.error(f"[{conn.device_id}] Hub session start failed: {e!r}")
        if conn.hub_start_task is asyncio.current_task():
            await _hub_fallback(conn, call_id, "hub_unreachable", pend)
        elif conn.hub_session is not None and conn.hub_session.call_id == call_id:
            await _end_hub_session(conn, send_ended=True, tell_device=True, orch_reason="error")
    finally:
        _hub_start_done(conn)


async def _run_hub_handshake(conn: DeviceConnection, msg: dict,
                             pend: Optional["WakePending"] = None):
    call_id = str(msg.get("call_id") or "")
    profile = str(msg.get("profile") or "")
    name = str(msg.get("name") or "")
    if not (call_id and profile and name):
        logger.warning(f"[{conn.device_id}] Hub live_session_start without call_id/profile/name")
        await _hub_fallback(conn, call_id, "error", pend)
        return
    if not _duplex_capable(conn):
        # Un firmware che non conosce il duplex ignorerebbe il live_session_start: sessione appesa
        # e stanza mutata dall'orchestrator. Meglio la sessione live di prima
        logger.warning(f"[{conn.device_id}] Hub session refused: fw {conn.firmware_version} has no duplex")
        await _hub_fallback(conn, call_id, "error", pend)
        return
    host, port = _hub_address()
    if not host or not port:
        await _hub_fallback(conn, call_id, "hub_unreachable", pend)
        return

    offer = {"type": "offer", "call_id": call_id, "profile": profile, "name": name}
    for key in ("voice", "location_id", "room"):
        if msg.get(key):
            offer[key] = str(msg[key])
    if isinstance(msg.get("deliver"), list) and msg["deliver"]:
        offer["deliver"] = [str(d) for d in msg["deliver"]]
    vad = {}
    # Sensibilita' bassa solo dove c'e' eco residuo (Atom); con l'AEC sul device (XMOS) il
    # barge-in a voce passa dal VAD di Gemini e va lasciato al default
    if HUB_VAD_START_SENSITIVITY and not _aec_onboard(conn):
        vad["startOfSpeechSensitivity"] = HUB_VAD_START_SENSITIVITY
    if HUB_VAD_PREFIX_PADDING_MS > 0:
        vad["prefixPaddingMs"] = HUB_VAD_PREFIX_PADDING_MS
    if vad:
        offer["vad"] = vad
    # Oggetto opaco dell'orchestrator (identified, origin, ...): nell'offer cosi' com'e', e le
    # sue chiavi anche al primo livello (il hub legge prima l'oggetto, poi il primo livello)
    extra = dict(msg["offer_extra"]) if isinstance(msg.get("offer_extra"), dict) else None
    if pend is not None:
        extra = extra if extra is not None else {}
        extra.setdefault("origin", str(msg.get("origin") or pend.trigger))
    aec_task = None
    relay = HubRelay(conn.device_id, call_id)
    if pend is not None:
        # Accept tardivo del hub (contesto + Gemini aperto) e AEC creato durante l'attesa
        relay.ACCEPT_TIMEOUT_S = LIVE_ACCEPT_TIMEOUT_S
        if not _aec_onboard(conn):
            aec_task = asyncio.ensure_future(_create_aec(agc="aec_ref" not in conn.caps))

    try:
        await relay.connect(host, port)
        if pend is not None:
            # Comando detto tutto d'un fiato (19.12, D1): c'e' parlato nel buffer? Deciso il
            # piu' tardi possibile (dopo il connect): il buffer cresce fino all'offer
            extra["command"] = _command_detected(conn, pend)
        if extra is not None:
            for key, value in extra.items():
                # Le chiavi gia' nell'offer (call_id, profile, name, vad...) non si toccano
                if key not in _OFFER_PROTECTED_KEYS and key not in offer:
                    offer[key] = value
            offer["offer_extra"] = extra
        await relay.send_offer(offer)
        if _aec_onboard(conn):
            # Passthrough: niente AEC3, AGC, NS ne' cancello sul mic gia' ripulito dall'XMOS
            aec = create_echo_canceller("off")
        elif aec_task is not None:
            aec = await aec_task
        else:
            aec = await _create_aec(agc="aec_ref" not in conn.caps)
    except HubRejected as e:
        _drop_aec_task(aec_task)
        await relay.close()
        logger.warning(f"[{conn.device_id}] Hub rejected call {call_id}: {e}")
        await _hub_fallback(conn, call_id, "hub_rejected", pend,
                            detail=(str(e) or None) if pend is not None else None)
        return
    except HubUnavailable as e:
        _drop_aec_task(aec_task)
        await relay.close()
        logger.warning(f"[{conn.device_id}] Hub unavailable for call {call_id}: {e}")
        await _hub_fallback(conn, call_id, "hub_unreachable", pend)
        return
    except asyncio.CancelledError:
        # Handshake abbandonato (device disconnesso, tap, fine dall'orchestrator):
        # niente socket appesi; l'orchestrator lo avvisa chi ha annullato
        _drop_aec_task(aec_task)
        await relay.close()
        raise
    except Exception as e:
        _drop_aec_task(aec_task)
        await relay.close()
        logger.warning(f"[{conn.device_id}] Hub handshake failed for call {call_id}: {e!r}")
        await _hub_fallback(conn, call_id, "hub_unreachable", pend)
        return

    try:
        session = HubLiveSession(conn.device_id, call_id, relay, conn.send_json,
                                 conn.send_binary, "off" if _aec_onboard(conn) else AEC_BACKEND,
                                 aec=aec)
    except Exception:
        aec.close()
        await relay.send_json({"type": "ended", "call_id": call_id})
        await relay.close()
        raise
    gain = (_mic_gain_db() if "aec_ref" in conn.caps and HUB_AEC_REF_MODE != "level"
            and not _aec_onboard(conn) else None)
    if gain is not None:
        # Il guadagno tolto al mic in duplex torna dopo l'AEC, fisso (l'AGC e' spento)
        session.aec.set_aligned_gain_db(_ATOM_MIC_SCALE_DB - gain)
    if _aec_onboard(conn) and HUB_ONBOARD_GAIN_DB:
        session.set_pcm_gain_db(HUB_ONBOARD_GAIN_DB)
    # Accept ricevuto: da qui la sessione e' di conn e si chiude con _end_hub_session
    _hub_start_done(conn)
    command = b""
    if pend is not None:
        # Il buffer del comando va al hub per primo: preso, LIVE_PENDING chiuso e sessione
        # assegnata senza await in mezzo, cosi' nessun frame dal vivo lo scavalca
        command = bytes(pend.buffer)
        if conn.wake_pending is pend:
            _clear_live_pending(conn, pend)
    conn.hub_session = session
    conn.live_session = True
    conn.state = DeviceState.BUSY
    conn.wakeword.mute(3600)
    if pend is not None:
        sent = session.prime(command, pcm_device=pend.pcm)
        logger.info(f"[{conn.device_id}] Hub accept for req {pend.req_id} after "
                    f"{time.monotonic() - pend.t0:.2f}s, command buffer {sent // 32} ms")
        # La stanza si muta da qui (live_session_active), non al live_session_start
        await _send_orch(conn, {"type": "live_session_active", "call_id": call_id,
                                "req_id": pend.req_id})
        if conn.hub_session is not session:
            return
    else:
        # Sessione da "avvia una sessione live": la stanza e' gia' mutata, ma il live_session_active
        # dice all'orchestrator che questo peer inoltra gli annunci all'hub (19.12 D6)
        await _send_orch(conn, {"type": "live_session_active", "call_id": call_id})
        if conn.hub_session is not session:
            return
    # Prima il device in duplex, poi il media: il hub puo' parlare subito
    await conn.send_json(_duplex_start_msg(conn))
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
    if _aec_onboard(conn):
        conn.pcm_tail_until = time.monotonic() + _PCM_TAIL_S
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
        await _teardown_live_pending(conn)
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
        # Doppio tap / pressione lunga ("button") o duplex non avviabile sull'Atom ("error")
        await _end_hub_session(conn, send_ended=True, orch_reason=_device_end_reason(msg))
        return True
    if msg_type == "live_interrupt":
        # Tap singolo: zittire Jarvis. Qui si butta il parlato non ancora mandato e l'Atom
        # riceve audio_clear (svuota coda e ring come a un barge-in); il hub scarta il resto
        # della risposta in corso di Gemini. La sessione continua.
        session = conn.hub_session
        logger.info(f"[{conn.device_id}] Hub session: interrupt from the device (call {session.call_id})")
        await session.clear()
        await session.relay.send_json({"type": "interrupt", "call_id": session.call_id})
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
# Fase 3: dalla wake word (o dal tap) direttamente a Gemini Live (design 19, 19.12)
# ---------------------------------------------------------------------------
# Il peer chiede, l'orchestrator decide: il wakeword-server non apre mai il hub senza il
# call_id dell'orchestrator. Ogni richiesta ha un req_id coniato qui (wake rilevate dal server)
# o dal device (live_start della NabuVoice, tap), e ogni risposta lo riporta.
#
# Wake rilevata qui su un Atom con cap "live_wake" e live abilitato (LIVE_ON_WAKE + flag
# live_on_wake del welcome): LIVE_PENDING, wake_detected{live,req_id} al device, ack del device
# (live_wake_ack ok|dnd|busy), live_start{req_id,trigger,preroll} all'orchestrator, poi una sola
# uscita verso il device: live_session_start duplex (dopo l'accept del hub), wake_detected
# normale (ripiego classico) oppure live_abort (DND, doppione, annullo: torna a riposo).
# Richiesta dal device (live_start): stesse fasi senza ack; le uscite sono live_session_start,
# live_declined{req_id,reason} (il device fa il suo classico) oppure live_abort.
# Il cancello a voce viene prima di tutto: wake_preroll{gate:true} e verdetto wake_gate.
# Con i flag spenti niente di questo gira e il percorso e' quello di oggi.

# Messaggi dell'orchestrator che riguardano solo il wakeword-server (mai inoltrati al device)
_LIVE_ORCH_MESSAGES = ("wake_gate", "live_declined", "announce")
# Rifiuti dell'orchestrator che non portano al classico: il device torna a riposo
_SILENT_DECLINES = ("duplicate", "voice_gate", "gate")
# Chiavi dell'offer che offer_extra non puo' sovrascrivere
_OFFER_PROTECTED_KEYS = ("type", "call_id", "offer_extra")
# Wake word muta dopo una wake scartata o abbandonata: la coda della stessa frase non deve
# rilevare di nuovo
_LIVE_REST_MUTE_S = 2.0
_MAX_REQ_ID = 64
_PREROLL_MAX_BYTES = 4 * OPUS_SAMPLE_RATE * 2  # pre-roll dal device: tetto 4 s

_background_tasks: set = set()


class WakePending:
    """Una wake o un tap in LIVE_PENDING. phase: gate (attesa del verdetto del cancello a voce)
    -> ack (attesa di live_wake_ack, solo wake rilevate qui) -> orch (live_start mandato,
    attesa della risposta) -> hub (handshake col hub)."""

    def __init__(self, req_id: str, trigger: str, *, device_initiated: bool,
                 preroll: bytes = b"", score: Optional[float] = None, live: bool = True,
                 pcm: bool = False):
        self.req_id = req_id
        self.trigger = trigger
        self.device_initiated = device_initiated
        self.preroll = preroll
        self.score = score
        self.live = live          # False = solo cancello a voce, poi classico
        self.pcm = pcm            # uplink PCM s16le (NabuVoice) invece di Opus
        self.phase = "gate"
        self.buffer = bytearray()
        self.max_bytes = int(LIVE_COMMAND_MAX_S * OPUS_SAMPLE_RATE) * 2
        self.pcm_tail = b""
        self.vad_skip = (0, 0)    # byte del buffer da non contare nel VAD (chirp)
        self.start_sent = False   # live_start partito: un abbandono va detto con live_cancel
        self.gate_future: Optional[asyncio.Future] = None
        self.timer: Optional[asyncio.Task] = None
        self.task: Optional[asyncio.Task] = None
        self.t0 = time.monotonic()

    def add(self, pcm: bytes):
        room = self.max_bytes - len(self.buffer)
        if room > 0 and pcm:
            self.buffer += pcm[:room]


def _new_req_id() -> str:
    return uuid.uuid4().hex[:16]


def _voice_gate_on(conn: DeviceConnection) -> bool:
    mode = WAKE_VOICE_GATE
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return conn.wake_voice_gate  # auto: segue il welcome dell'orchestrator


def _live_allowed(conn: DeviceConnection) -> bool:
    """Interruttore generale, flag del device in cache, hub configurato, firmware duplex."""
    return (LIVE_ON_WAKE and conn.live_on_wake and bool(_hub_address()[0])
            and _duplex_capable(conn))


def _live_on_server_wake(conn: DeviceConnection) -> bool:
    """Live sulle wake rilevate qui: solo firmware con la cap "live_wake". I device con l'AEC a
    bordo (NabuVoice) rilevano in locale e chiedono da soli con live_start."""
    return _live_allowed(conn) and "live_wake" in conn.caps and not _aec_onboard(conn)


def _read_welcome_flags(conn: DeviceConnection, msg: dict):
    """Flag per device dal welcome dell'orchestrator (a ogni apertura del relay)."""
    if "live_on_wake" in msg:
        conn.live_on_wake = bool(msg.get("live_on_wake"))
    if "wake_voice_gate" in msg:
        conn.wake_voice_gate = bool(msg.get("wake_voice_gate"))


def _spawn(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _save_clip(device_id: str, score: float, pcm: bytes, when: float):
    try:
        archive = ClipArchive(WAKE_CLIP_DIR, WAKE_CLIP_MAX_FILES, int(WAKE_CLIP_MAX_MB * 1024 * 1024))
        path = archive.save(device_id, score, pcm, when)
        if path:
            logger.info(f"[{device_id}] Wake clip saved: {path}")
    except Exception as e:
        logger.warning(f"[{device_id}] Wake clip not saved: {e!r}")


def _archive_clip(conn: DeviceConnection, pcm: bytes, score: float):
    """Pre-roll di ogni rilevamento su disco, fuori dall'event loop."""
    if not WAKE_CLIP_ARCHIVE or not WAKE_CLIP_DIR or not pcm:
        return
    _spawn(asyncio.to_thread(_save_clip, conn.device_id, score, pcm, time.time()))


def _b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


async def _send_orch(conn: DeviceConnection, msg: dict, reopen: bool = True) -> bool:
    """Messaggio JSON all'orchestrator sul relay del device."""
    if not (conn.relay and conn.relay.is_connected):
        if not reopen:
            return False
        try:
            await _open_relay(conn)
        except Exception as e:
            logger.warning(f"[{conn.device_id}] {msg.get('type')} not delivered: {e}")
            return False
    if not (conn.relay and conn.relay.is_connected):
        return False
    await conn.relay.send_text(json.dumps(msg))
    return True


async def _send_wake_preroll(conn: DeviceConnection, req_id: str, preroll: bytes,
                             score: float, gate: bool) -> bool:
    """Pre-roll all'orchestrator per il voice ID: in shadow (gate false) logga solo i punteggi,
    con gate true l'orchestrator risponde wake_gate{req_id,action:"keep"|"drop"}."""
    msg = {"type": "wake_preroll", "req_id": req_id, "score": round(score, 3), "gate": gate,
           "codec": "pcm_s16le", "rate": OPUS_SAMPLE_RATE, "b64": _b64(preroll)}
    model = getattr(conn.wakeword, "model_name", None)
    if model:
        msg["model"] = str(model)
    return await _send_orch(conn, msg)


def _pending_audio(conn: DeviceConnection, data: bytes):
    """Binari in LIVE_PENDING: audio del comando (Opus decodificato, o PCM della NabuVoice dopo
    il suo live_start). Nessuna wake word qui: una wake durante LIVE_PENDING non conta."""
    pend = conn.wake_pending
    if pend is None or not pend.live:
        return
    if pend.pcm:
        data = pend.pcm_tail + bytes(data)
        if len(data) % 2:
            pend.pcm_tail, data = data[-1:], data[:-1]
        else:
            pend.pcm_tail = b""
        pend.add(data)
        return
    pcm = conn.decode_opus(data)
    if pcm is not None:
        pend.add(pcm.tobytes())


def _command_detected(conn: DeviceConnection, pend: WakePending) -> bool:
    ms = speech_ms(bytes(pend.buffer), pend.preroll, pend.vad_skip,
                   margin_db=LIVE_COMMAND_MARGIN_DB, min_dbfs=LIVE_COMMAND_MIN_DBFS)
    command = ms >= LIVE_COMMAND_MIN_SPEECH_MS
    logger.info(f"[{conn.device_id}] Command buffer {len(pend.buffer) // 32} ms, speech {ms} ms "
                f"-> command={command} (req {pend.req_id})")
    return command


def _arm_timer(conn: DeviceConnection, pend: WakePending, seconds: float, phase: str):
    if pend.timer is not None and not pend.timer.done():
        pend.timer.cancel()
    pend.timer = asyncio.create_task(_pending_timeout(conn, pend, seconds, phase))


async def _pending_timeout(conn: DeviceConnection, pend: WakePending, seconds: float, phase: str):
    await asyncio.sleep(seconds)
    if conn.wake_pending is not pend or pend.phase != phase:
        return
    pend.timer = None  # e' questo task: _clear_live_pending non deve cancellarlo
    logger.warning(f"[{conn.device_id}] LIVE_PENDING timeout in phase {phase} "
                   f"({seconds:.1f}s, req {pend.req_id})")
    await _live_pending_exit(conn, pend, "classic", reason="timeout", cancel_orch=True)


def _clear_live_pending(conn: DeviceConnection, pend: WakePending):
    """Toglie la wake/tap in attesa e ferma timer e task (non quello corrente)."""
    if conn.wake_pending is pend:
        conn.wake_pending = None
    current = asyncio.current_task()
    for task in (pend.timer, pend.task):
        if task is not None and not task.done() and task is not current:
            task.cancel()
    pend.timer = None
    if pend.gate_future is not None and not pend.gate_future.done():
        pend.gate_future.cancel()


def _rest_after_pending(conn: DeviceConnection, pend: WakePending, mute_s: float):
    conn.state = DeviceState.IDLE
    conn.wakeword.mute(mute_s)
    conn.wakeword.reset()
    if pend.pcm:
        conn.pcm_tail_until = time.monotonic() + _PCM_TAIL_S


async def _cancel_live_pending(conn: DeviceConnection, pend: WakePending):
    """Il device rinuncia (tap, doppio tap, pressione lunga, timeout del firmware): live_cancel
    all'orchestrator se aveva gia' la richiesta, handshake col hub fermato senza
    live_session_end (lo copre live_cancel). Niente al device: lo stato lo decide il chiamante."""
    _clear_live_pending(conn, pend)
    await _abandon_hub_start(conn)
    if pend.start_sent:
        await _send_orch(conn, {"type": "live_cancel", "req_id": pend.req_id}, reopen=False)
    if pend.pcm:
        conn.pcm_tail_until = time.monotonic() + _PCM_TAIL_S
    logger.info(f"[{conn.device_id}] LIVE_PENDING cancelled by the device (req {pend.req_id}, "
                f"phase {pend.phase})")


async def _live_pending_exit(conn: DeviceConnection, pend: WakePending, how: str,
                             reason: str = "", cancel_orch: bool = False):
    """Chiude il LIVE_PENDING con una delle uscite verso il device: "classic" (ripiego: wake
    normale per le wake rilevate qui, live_declined per le richieste del device) oppure
    "abort" (live_abort, torna a riposo)."""
    if conn.wake_pending is not pend:
        return
    _clear_live_pending(conn, pend)
    await _abandon_hub_start(conn)
    if cancel_orch and pend.start_sent:
        await _send_orch(conn, {"type": "live_cancel", "req_id": pend.req_id}, reopen=False)
    logger.info(f"[{conn.device_id}] LIVE_PENDING -> {how} (req {pend.req_id}, phase {pend.phase}"
                f"{', ' + reason if reason else ''})")
    if how == "abort":
        await conn.send_json({"type": "live_abort", "req_id": pend.req_id})
        _rest_after_pending(conn, pend, _LIVE_REST_MUTE_S)
    elif pend.device_initiated:
        await conn.send_json({"type": "live_declined", "req_id": pend.req_id,
                              "reason": reason or "error"})
        # Il device apre da se' il turno classico (audio_start); se non lo fa non resta sordo
        _rest_after_pending(conn, pend, 5.0)
    else:
        # Wake normale: stato WAKING, wake word ancora muta, il firmware fa activate_listening
        await _classic_wake(conn)


async def _gate_flow(conn: DeviceConnection, pend: WakePending):
    """Cancello a voce: pre-roll all'orchestrator, verdetto entro il timeout. Senza verdetto
    (timeout, relay giu', orchestrator vecchio) la wake passa."""
    verdict = "pass"
    relay_ok = True
    try:
        await _open_relay(conn)
        pend.gate_future = asyncio.get_running_loop().create_future()
        if not await _send_wake_preroll(conn, pend.req_id, pend.preroll, pend.score or 0.0, gate=True):
            relay_ok = False
        else:
            verdict = await asyncio.wait_for(pend.gate_future, WAKE_VOICE_GATE_TIMEOUT_S)
    except asyncio.CancelledError:
        if conn.wake_pending is pend:
            raise
        return  # chiuso da un gesto del device o dalla disconnessione
    except asyncio.TimeoutError:
        logger.warning(f"[{conn.device_id}] Voice gate: no verdict in {WAKE_VOICE_GATE_TIMEOUT_S:.1f}s "
                       f"(req {pend.req_id}), wake passes")
    except Exception as e:
        relay_ok = False
        logger.warning(f"[{conn.device_id}] Voice gate unavailable ({e!r}), wake passes")
    if conn.wake_pending is not pend:
        return
    if verdict == "drop":
        logger.info(f"[{conn.device_id}] Voice gate: wake dropped (req {pend.req_id}, "
                    f"score {pend.score or 0.0:.3f})")
        _clear_live_pending(conn, pend)
        _rest_after_pending(conn, pend, _LIVE_REST_MUTE_S)
        _multiroom.cancel(conn.device_id)
        return
    if pend.live and relay_ok:
        await _live_offer_to_device(conn, pend)
    else:
        _clear_live_pending(conn, pend)
        await _classic_wake(conn)


async def _live_offer_to_device(conn: DeviceConnection, pend: WakePending):
    """Wake rilevata qui, live possibile: wake_detected{live} al device e intanto il relay."""
    pend.phase = "ack"
    _arm_timer(conn, pend, LIVE_ACK_TIMEOUT_S, "ack")
    await conn.send_json({"type": "wake_detected", "live": True, "req_id": pend.req_id})
    try:
        await _open_relay(conn)
    except Exception as e:
        # Orchestrator irraggiungibile: wake normale subito, senza aspettare l'ack
        logger.error(f"[{conn.device_id}] Failed to open relay for live wake: {e}")
        await _live_pending_exit(conn, pend, "classic", reason="relay")


async def _send_live_start(conn: DeviceConnection, pend: WakePending):
    """live_start all'orchestrator; la risposta (live_session_start, live_declined) deve
    arrivare entro LIVE_START_TIMEOUT_S, altrimenti live_cancel e ripiego."""
    pend.phase = "orch"
    _arm_timer(conn, pend, LIVE_START_TIMEOUT_S, "orch")
    try:
        await _open_relay(conn)
    except Exception as e:
        logger.error(f"[{conn.device_id}] Failed to open relay for live_start: {e}")
        await _live_pending_exit(conn, pend, "classic", reason="relay")
        return
    if conn.wake_pending is not pend or pend.phase != "orch":
        return
    msg = {"type": "live_start", "req_id": pend.req_id, "trigger": pend.trigger}
    if pend.preroll:
        msg["preroll"] = {"codec": "pcm_s16le", "rate": OPUS_SAMPLE_RATE, "b64": _b64(pend.preroll)}
    if pend.score is not None:
        msg["score"] = round(pend.score, 3)
    pend.start_sent = True
    if not await _send_orch(conn, msg, reopen=False):
        await _live_pending_exit(conn, pend, "classic", reason="relay")
        return
    logger.info(f"[{conn.device_id}] live_start sent (req {pend.req_id}, trigger {pend.trigger}, "
                f"preroll {len(pend.preroll) // 32} ms)")


async def _on_device_live_start(conn: DeviceConnection, msg: dict):
    """live_start dal device: wake rilevata in locale (NabuVoice, pre-roll nel JSON, mai binari
    dopo) o tap (trigger "tap", niente pre-roll, il buffer del comando parte da qui)."""
    req_id = msg.get("req_id")
    if not isinstance(req_id, str) or not req_id or len(req_id) > _MAX_REQ_ID:
        logger.warning(f"[{conn.device_id}] live_start without a valid req_id ignored")
        return
    b64 = msg.get("preroll_b64")
    if not b64 and isinstance(msg.get("preroll"), dict):
        b64 = msg["preroll"].get("b64")
    trigger = msg.get("trigger")
    if trigger not in ("wake", "tap"):
        trigger = "wake" if b64 else "tap"
    preroll = b""
    if b64 and trigger == "wake":
        try:
            preroll = base64.b64decode(b64, validate=True)[-_PREROLL_MAX_BYTES:]
            preroll = preroll[:len(preroll) - len(preroll) % 2]
        except Exception:
            logger.warning(f"[{conn.device_id}] live_start with a bad preroll_b64: sent without")
            preroll = b""
    reason = ""
    if not _live_allowed(conn):
        reason = "disabled"
    elif (conn.hub_session is not None or conn.hub_start_task is not None or conn.live_session
            or conn.state in (DeviceState.STREAMING, DeviceState.LIVE_PENDING)):
        reason = "busy"
    if reason:
        if _aec_onboard(conn) and conn.hub_session is None:
            # La NabuVoice manda PCM subito dopo il live_start: in IDLE non e' Opus per la
            # wake word, va scartato finche' il device non ha letto il rifiuto
            conn.pcm_tail_until = time.monotonic() + _PCM_TAIL_S
        await conn.send_json({"type": "live_declined", "req_id": req_id, "reason": reason})
        return
    pend = WakePending(req_id, trigger, device_initiated=True, preroll=preroll,
                       pcm=_aec_onboard(conn))
    conn.tts_prepare_token = None
    conn.wake_pending = pend
    conn.state = DeviceState.LIVE_PENDING
    conn.wakeword.mute(60.0)
    conn.wakeword.reset()
    logger.info(f"[{conn.device_id}] State → LIVE_PENDING (device live_start, req {req_id}, "
                f"trigger {trigger})")
    await _send_live_start(conn, pend)


async def _handle_live_pending_device_text(conn: DeviceConnection, msg_type: str, msg: dict) -> bool:
    """Messaggi del device in LIVE_PENDING. True = gestito qui."""
    pend = conn.wake_pending
    if msg_type == "live_wake_ack":
        if msg.get("req_id") != pend.req_id or pend.phase != "ack":
            logger.info(f"[{conn.device_id}] live_wake_ack for req {msg.get('req_id')} ignored")
            return True
        status = msg.get("status")
        if status != "ok":
            # DND o device occupato: niente, come oggi con l'attivazione ignorata
            await _live_pending_exit(conn, pend, "abort", reason=f"ack {status}")
            return True
        # Il chirp suona adesso: quei ms non contano come parlato
        skip = LIVE_COMMAND_CHIRP_SKIP_MS * OPUS_SAMPLE_RATE // 1000 * 2
        pend.vad_skip = (len(pend.buffer), len(pend.buffer) + skip)
        await _send_live_start(conn, pend)
        return True
    if msg_type == "live_start":
        if msg.get("req_id") != pend.req_id:
            await conn.send_json({"type": "live_declined", "req_id": msg.get("req_id"), "reason": "busy"})
        return True
    if msg_type == "audio_start":
        # Tap: l'attivazione classica di oggi, l'orchestrator lo sa prima dell'audio_start
        await _cancel_live_pending(conn, pend)
        conn.state = DeviceState.WAKING
        return False
    if msg_type in ("audio_end", "speaker_stop"):
        # Pressione lunga / doppio tap: live_cancel e poi il comportamento di oggi
        await _cancel_live_pending(conn, pend)
        return False
    if msg_type == "live_session_end":
        # Timeout di sicurezza del firmware: rinuncia, a riposo
        await _cancel_live_pending(conn, pend)
        _rest_after_pending(conn, pend, _LIVE_REST_MUTE_S)
        return True
    return False


async def _handle_live_orch_message(conn: DeviceConnection, msg_type: str, msg: dict):
    """wake_gate, live_declined, announce dall'orchestrator."""
    pend = conn.wake_pending
    if msg_type == "wake_gate":
        if (pend is not None and msg.get("req_id") == pend.req_id and pend.phase == "gate"
                and pend.gate_future is not None and not pend.gate_future.done()):
            # action keep|drop (orchestrator); verdict/drop accettati per robustezza
            drop = "drop" in (msg.get("action"), msg.get("verdict")) or msg.get("drop") is True
            pend.gate_future.set_result("drop" if drop else "pass")
        else:
            logger.info(f"[{conn.device_id}] Late wake_gate for req {msg.get('req_id')} ignored "
                        f"(action {msg.get('action') or msg.get('verdict')})")
        return
    if msg_type == "live_declined":
        if pend is None or msg.get("req_id") != pend.req_id or pend.phase != "orch":
            logger.info(f"[{conn.device_id}] live_declined for req {msg.get('req_id')} ignored")
            return
        reason = str(msg.get("reason") or "")
        if reason in _SILENT_DECLINES:
            await _live_pending_exit(conn, pend, "abort", reason=reason)
        else:
            await _live_pending_exit(conn, pend, "classic", reason=reason or "declined")
        return
    if msg_type == "announce":
        # Annuncio per la stanza con la sessione hub (19.12, D6): lo legge Gemini
        session = conn.hub_session
        call_id = msg.get("call_id")
        if session is None or (call_id and call_id != session.call_id):
            logger.warning(f"[{conn.device_id}] announce for call {call_id or '-'} dropped "
                           f"(no matching hub session)")
            return
        fwd = dict(msg)
        fwd["call_id"] = session.call_id
        await session.relay.send_json(fwd)
        logger.info(f"[{conn.device_id}] announce relayed to the hub (call {session.call_id}, "
                    f"urgent={bool(msg.get('urgent'))})")


async def _handle_live_pending_orch_message(conn: DeviceConnection, msg_type: str, msg: dict) -> bool:
    """Messaggi dell'orchestrator con un LIVE_PENDING in corso. True = gestito qui."""
    pend = conn.wake_pending
    if msg_type == "live_session_start" and msg.get("req_id"):
        if msg.get("req_id") != pend.req_id or pend.phase != "orch":
            logger.info(f"[{conn.device_id}] live_session_start for req {msg.get('req_id')} "
                        f"ignored (pending req {pend.req_id}, phase {pend.phase})")
            return True
        if msg.get("backend") != "hub":
            await _live_pending_exit(conn, pend, "classic", reason="error", cancel_orch=True)
            return True
        pend.phase = "hub"
        if pend.timer is not None:
            pend.timer.cancel()
            pend.timer = None
        await _begin_hub_session(conn, msg, pend)
        return True
    if msg_type == "live_session_start":
        # Sessione decisa dall'orchestrator per altro (percorso di oggi): la richiesta in
        # attesa decade, poi tutto come oggi
        await _cancel_live_pending(conn, pend)
        conn.state = DeviceState.BUSY
        return False
    if msg_type == "live_session_end":
        if pend.phase in ("orch", "hub"):
            await _live_pending_exit(conn, pend, "abort", reason="orchestrator end")
        return True
    if msg_type in _HUB_SKIPPED_TURN_MESSAGES:
        logger.info(f"[{conn.device_id}] {msg_type} from orchestrator ignored (LIVE_PENDING)")
        return True
    return False


async def _teardown_live_pending(conn: DeviceConnection):
    """Device sparito con una wake/tap in attesa: live_cancel se l'orchestrator la conosceva."""
    pend = conn.wake_pending
    if pend is None:
        return
    _clear_live_pending(conn, pend)
    await _abandon_hub_start(conn)
    if pend.start_sent:
        await _send_orch(conn, {"type": "live_cancel", "req_id": pend.req_id}, reopen=False)


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
    if conn.wake_pending is not None:
        raise HTTPException(status_code=409, detail="Device in LIVE_PENDING")

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
                "live_on_wake": conn.live_on_wake,
                "wake_voice_gate": conn.wake_voice_gate,
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
