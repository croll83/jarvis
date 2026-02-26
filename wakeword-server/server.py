"""jarvis-wakeword-server — FastAPI + WebSocket handler.

Receives continuous Opus audio from AtomS3R devices, runs openWakeWord
detection, and relays audio sessions to the orchestrator VPS on wake.
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
    OPUS_FRAME_SAMPLES, WAKEWORD_THRESHOLD, ORCHESTRATOR_URL,
)
from wakeword import DeviceWakeWordEngine
from multiroom import MultiroomCooldown
from relay import OrchestratorRelay

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
        self.live_session: bool = False  # True during live session

        # Opus decoder (16 kHz mono, same as firmware encoder)
        self._opus_decoder = opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)

        # Wake word engine
        self.wakeword = DeviceWakeWordEngine(device_id)
        self.wakeword.init()

        # VPS relay (created on-demand)
        self.relay: Optional[OrchestratorRelay] = None

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
            await self.ws.send_text(json.dumps(data))
        except Exception as e:
            logger.error(f"[{self.device_id}] send_json error: {e}")

    async def send_binary(self, data: bytes):
        """Send binary (TTS Opus) to device."""
        try:
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

    if msg_type == "hello":
        conn.firmware_version = msg.get("fw", "unknown")
        logger.info(f"[{conn.device_id}] Hello (fw={conn.firmware_version})")
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

    else:
        # Unknown message — relay if connected
        if conn.relay and conn.relay.is_connected:
            await conn.relay.send_text(raw)


async def _handle_binary(conn: DeviceConnection, data: bytes):
    """Handle binary Opus frame from device."""
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
            # Binary TTS Opus — pass through to device
            await conn.send_binary(message)

    conn.relay = OrchestratorRelay(conn.device_id, on_vps_message)
    await conn.relay.connect()


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
                "threshold": conn.wakeword.threshold,
                "relay_connected": conn.relay.is_connected if conn.relay else False,
            })
    return result


# ---------------------------------------------------------------------------
# HTTP Proxy: Device management calls → Orchestrator via Tailscale
# ---------------------------------------------------------------------------
# These endpoints allow AtomS3R devices to reach the orchestrator through
# the wakeword-server (LAN) instead of directly via the public internet.
# Bearer tokens never leave the LAN + Tailscale encrypted tunnel.
# ---------------------------------------------------------------------------

async def _proxy_request(request: Request, path: str) -> JSONResponse:
    """Forward an HTTP request to the orchestrator and return the response."""
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

    # Build target URL preserving query string
    query_string = str(request.url.query)
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
    """Proxy room temperature fetch to orchestrator."""
    return await _proxy_request(request, f"/room_temperature/{room}")


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
