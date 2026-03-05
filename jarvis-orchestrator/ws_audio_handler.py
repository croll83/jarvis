"""
WebSocket Audio Handler -- Persistent bidirectional WebSocket for voice devices (AtomS3R, NabuVoice, etc.).

Protocol (unified: control + audio on a single persistent connection):
  1. Device connects: ws://.../ws/audio?device_id=...&token=...
  2. Server accepts, sends {"type":"welcome", "server_time": ...}
  3. Device sends {"type":"hello", "fw":"...", "device_id":"..."}
  4. Connection stays open (idle: only JSON keepalive / state updates)

  Audio session (within the persistent connection):
  5. Device sends {"type":"audio_start"} when wake word detected or trigger_listen received
  6. Server creates WsAudioSession, sends {"type":"ready","session_id":"..."}
  7. Device sends binary audio frames (Opus: 20ms ~30-80 bytes; PCM: 640 bytes)
  8. Silero VAD detects speech end → server sends {"type":"speech_end"}
  9. Callback on_speech_complete(device_id, pcm_bytes_16k)
  10. Audio session destroyed, but WebSocket stays open → back to step 4

  Server → Device commands:
  - {"type":"trigger_listen","silent":true}  -- trigger mic activation
  - {"type":"ping"}                          -- keepalive

  Device → Server state:
  - {"type":"state","state":"idle|listening|busy|dnd|error"}

  Backward compatibility:
  - If device sends binary Opus frames without "audio_start" first,
    auto-create audio session (legacy ephemeral protocol).

Notes:
  - Silero VAD loaded via onnxruntime (no torch dependency).
  - Each audio session gets its own RNN state for isolation.
  - Opus decoded to 16 kHz directly (firmware encodes at 16 kHz).
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Callable, Awaitable, Optional, Dict

import numpy as np
import onnxruntime as ort

import config

logger = logging.getLogger("JARVIS_WS_AUDIO")


# ---------------------------------------------------------------------------
# Opus decoder via opuslib (ctypes wrapper for libopus)
# ---------------------------------------------------------------------------
try:
    import opuslib
    _OPUS_AVAILABLE = True
except ImportError:
    logger.warning("opuslib not installed -- WS audio will not work")
    _OPUS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Silero VAD via ONNX Runtime (no torch dependency)
# ---------------------------------------------------------------------------
_vad_onnx_path: Optional[str] = None


class SileroVADOnnx:
    """
    Wrapper leggero per Silero VAD ONNX.
    Mantiene lo stato RNN (h, c) e il context buffer internamente.
    Ogni sessione deve avere la propria istanza.

    IMPORTANTE: il modello Silero VAD richiede un context di 64 campioni
    prepeso all'input (per 16kHz). L'input effettivo è (1, 576) non (1, 512).
    Vedi silero_vad/utils_vad.py OnnxWrapper.__call__ per riferimento.
    """

    CONTEXT_SIZE = 64  # 64 samples context per 16kHz

    def __init__(self, model_path: str):
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._sess = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT_SIZE), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def __call__(self, audio_chunk: np.ndarray) -> float:
        """
        Esegui VAD su un chunk audio float32 (512 samples @ 16kHz).
        Ritorna probabilita di speech [0.0, 1.0].
        """
        inp = audio_chunk.reshape(1, -1).astype(np.float32)

        # Prepend context (last 64 samples from previous chunk)
        inp_with_ctx = np.concatenate([self._context, inp], axis=1)

        out, new_state = self._sess.run(
            ["output", "stateN"],
            {
                "input": inp_with_ctx,
                "state": self._state,
                "sr": self._sr,
            },
        )
        self._state = new_state
        # Save last CONTEXT_SIZE samples as context for next call
        self._context = inp[:, -self.CONTEXT_SIZE:]
        return float(out[0, 0])

    def reset(self):
        """Reset stato RNN e context (per nuova sessione/utterance)."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT_SIZE), dtype=np.float32)


def init_vad():
    """
    Pre-carica path modello Silero VAD ONNX e verifica che funzioni.
    Chiamato dal lifespan di main.py.
    """
    global _vad_onnx_path

    # Cerca il modello ONNX nel package silero_vad installato
    import site
    candidates = []
    for sp in site.getsitepackages():
        p = os.path.join(sp, "silero_vad", "data", "silero_vad.onnx")
        if os.path.isfile(p):
            candidates.append(p)

    # Fallback: cerca in paths comuni
    if not candidates:
        common = "/usr/local/lib/python3.11/site-packages/silero_vad/data/silero_vad.onnx"
        if os.path.isfile(common):
            candidates.append(common)

    if not candidates:
        raise FileNotFoundError("Silero VAD ONNX model not found. Is silero-vad installed?")

    _vad_onnx_path = candidates[0]

    # Verifica che il modello si carichi correttamente
    test_vad = SileroVADOnnx(_vad_onnx_path)
    test_chunk = np.zeros(512, dtype=np.float32)
    prob = test_vad(test_chunk)
    logger.info(f"Silero VAD model pre-loaded (ONNX): {_vad_onnx_path} (test prob={prob:.4f})")


def _new_vad_instance() -> SileroVADOnnx:
    """Crea una nuova istanza VAD con stato RNN fresco."""
    if _vad_onnx_path is None:
        raise RuntimeError("VAD not initialized. Call init_vad() first.")
    return SileroVADOnnx(_vad_onnx_path)


# ---------------------------------------------------------------------------
# Persistent connections registry
# ---------------------------------------------------------------------------
_persistent_connections: Dict[str, "PersistentDeviceConnection"] = {}
_connections_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# WsAudioSession -- handles one speech utterance within a persistent connection
# ---------------------------------------------------------------------------
class WsAudioSession:
    """
    Gestisce una singola sessione audio (un utterance) all'interno di una
    connessione WS persistente.

    - Riceve frame audio binari via WebSocket (Opus o PCM raw)
    - Decodifica Opus -> PCM 16 kHz mono (opuslib) oppure converte PCM int16 -> float32
    - Silero VAD su chunk da 512 samples @ 16 kHz
    - Detecta speech start (min_speech_ms) -> speech end (min_silence_ms)
    - Chiama callback con PCM 16 kHz concatenato
    """

    # Opus frame size: 320 samples = 20ms @ 16 kHz
    OPUS_FRAME_SAMPLES = 320
    # VAD chunk size: 512 samples @ 16 kHz = 32ms
    VAD_CHUNK_SIZE = 512

    def __init__(
        self,
        device_id: str,
        on_speech_complete: Callable[[str, bytes], Awaitable[None]],
        session_id: Optional[str] = None,
        codec: str = "opus",
    ):
        self.device_id = device_id
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.on_speech_complete = on_speech_complete
        self.codec = codec  # "opus" (AtomS3R) or "pcm" (NabuVoice)

        # Config
        self._timeout = getattr(config, 'WS_AUDIO_SESSION_TIMEOUT', 60)
        self._vad_threshold = config.VAD_THRESHOLD
        self._min_silence_ms = config.VAD_MIN_SILENCE_MS
        self._min_speech_ms = config.VAD_MIN_SPEECH_MS

        # Opus decoder: 16 kHz mono (only needed for Opus codec)
        if codec == "opus" and _OPUS_AVAILABLE:
            self._opus_decoder = opuslib.Decoder(16000, 1)
        else:
            self._opus_decoder = None

        # VAD state (per-session ONNX instance per RNN state isolation)
        self._vad = _new_vad_instance()

        # Audio buffer (16 kHz PCM float32)
        self._audio_buffer: list[np.ndarray] = []
        self._vad_chunk_buffer = np.array([], dtype=np.float32)

        # Speech state
        self._speech_started = False
        self._speech_frames = 0   # frames con speech consecutivo
        self._silence_frames = 0  # frames con silenzio consecutivo dopo speech
        self._speech_start_time: Optional[float] = None

        # Timing
        self._created_at = time.time()
        self._last_audio_at: Optional[float] = None
        self._closed = False

        # Stats
        self._frames_received = 0

    def decode_opus_frame(self, opus_data: bytes) -> Optional[np.ndarray]:
        """
        Decode one Opus frame to PCM float32 at 16 kHz.
        Returns None if decoder not available or decode error.
        """
        if not self._opus_decoder:
            return None
        try:
            pcm_bytes = self._opus_decoder.decode(opus_data, self.OPUS_FRAME_SAMPLES)
            pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
            pcm_float = pcm_int16.astype(np.float32) / 32768.0

            if self._frames_received % 50 == 1:
                rms = float(np.sqrt(np.mean(pcm_float ** 2)))
                logger.info(f"[{self.session_id}] Decoded Opus frame #{self._frames_received}: "
                            f"{len(pcm_bytes)} bytes -> {len(pcm_int16)} samples, "
                            f"RMS={rms:.4f}, max={float(np.max(np.abs(pcm_float))):.4f}")

            return pcm_float
        except Exception as e:
            if self._frames_received <= 3:
                logger.warning(f"[{self.session_id}] Opus decode error on frame "
                               f"#{self._frames_received}: {e}")
            return None

    def decode_pcm_frame(self, raw_data: bytes) -> Optional[np.ndarray]:
        """
        Convert raw 16-bit PCM bytes to float32.
        NabuVoice sends 640-byte frames (320 samples × 2 bytes) at 16 kHz mono.
        """
        pcm_int16 = np.frombuffer(raw_data, dtype=np.int16)
        pcm_float = pcm_int16.astype(np.float32) / 32768.0

        if self._frames_received % 50 == 1:
            rms = float(np.sqrt(np.mean(pcm_float ** 2)))
            logger.info(f"[{self.session_id}] PCM frame #{self._frames_received}: "
                        f"{len(raw_data)} bytes -> {len(pcm_int16)} samples, "
                        f"RMS={rms:.4f}, max={float(np.max(np.abs(pcm_float))):.4f}")

        return pcm_float

    def process_audio(self, pcm_float: np.ndarray) -> bool:
        """
        Feed PCM float32 to VAD. Returns True when speech is complete.
        """
        chunk_duration_ms = (self.VAD_CHUNK_SIZE / 16000) * 1000  # 32ms

        self._vad_chunk_buffer = np.concatenate([self._vad_chunk_buffer, pcm_float])

        while len(self._vad_chunk_buffer) >= self.VAD_CHUNK_SIZE:
            chunk = self._vad_chunk_buffer[:self.VAD_CHUNK_SIZE]
            self._vad_chunk_buffer = self._vad_chunk_buffer[self.VAD_CHUNK_SIZE:]

            speech_prob = self._vad(chunk)
            is_speech = speech_prob > self._vad_threshold

            if self._frames_received % 50 == 1:
                logger.info(f"[{self.session_id}] VAD prob={speech_prob:.3f} "
                            f"thresh={self._vad_threshold} is_speech={is_speech} "
                            f"started={self._speech_started} "
                            f"speech_f={self._speech_frames} sil_f={self._silence_frames}")

            if is_speech:
                self._silence_frames = 0
                self._speech_frames += 1
                self._audio_buffer.append(chunk)

                if not self._speech_started:
                    total_speech_ms = self._speech_frames * chunk_duration_ms
                    if total_speech_ms >= self._min_speech_ms:
                        self._speech_started = True
                        self._speech_start_time = time.time()
                        logger.info(f"Speech started in session {self.session_id}")
            else:
                self._speech_frames = 0

                if self._speech_started:
                    self._audio_buffer.append(chunk)
                    self._silence_frames += 1

                    total_silence_ms = self._silence_frames * chunk_duration_ms
                    if total_silence_ms >= self._min_silence_ms:
                        logger.info(f"Speech ended in session {self.session_id} "
                                    f"(silence {total_silence_ms:.0f}ms)")
                        return True
                else:
                    lead_in_chunks = int(300 / chunk_duration_ms)
                    self._audio_buffer.append(chunk)
                    if len(self._audio_buffer) > lead_in_chunks:
                        self._audio_buffer = self._audio_buffer[-lead_in_chunks:]

        return False

    async def deliver_speech(self):
        """Concatena buffer, converte a PCM 16-bit bytes, chiama callback."""
        if not self._audio_buffer:
            return

        full_pcm = np.concatenate(self._audio_buffer)
        duration_s = len(full_pcm) / 16000

        logger.info(f"Delivering speech from session {self.session_id}: "
                    f"{duration_s:.1f}s, {len(full_pcm)} samples, "
                    f"codec={self.codec}, frames_received={self._frames_received}")

        pcm_int16 = (full_pcm * 32767).clip(-32768, 32767).astype(np.int16)
        pcm_bytes = pcm_int16.tobytes()

        # Reset audio state for potential next utterance on same connection
        self._audio_buffer = []
        self._speech_started = False
        self._speech_frames = 0
        self._silence_frames = 0

        try:
            await self.on_speech_complete(self.device_id, pcm_bytes)
        except Exception as e:
            logger.error(f"Error in speech callback for session {self.session_id}: {e}")


# ---------------------------------------------------------------------------
# PersistentDeviceConnection
# ---------------------------------------------------------------------------
class PersistentDeviceConnection:
    """
    Represents a persistent WebSocket connection to one voice device (AtomS3R, NabuVoice, etc.).
    Manages the lifecycle of multiple audio sessions over a single connection.
    """

    def __init__(self, device_id: str, websocket, on_speech_complete):
        self.device_id = device_id
        self.websocket = websocket
        self.on_speech_complete = on_speech_complete
        self.connected_at = time.time()
        self.last_state: Optional[str] = None
        self.last_state_at: Optional[float] = None
        self.last_pong_at: Optional[float] = None
        self.firmware_version: Optional[str] = None
        self.device_type: str = "AtomS3R"  # Determined from hello message firmware version
        self.audio_session: Optional[WsAudioSession] = None
        self._last_session_ended_at: float = 0.0  # cooldown per stray frames post-session
        self._closed = False

    async def send_command(self, command: dict) -> bool:
        """Send a JSON command to the device. Returns False if send fails."""
        if self._closed:
            return False
        try:
            await self.websocket.send_json(command)
            return True
        except Exception as e:
            logger.warning(f"Control WS send failed for {self.device_id}: {e}")
            self._closed = True
            return False

    async def trigger_listen(self, silent: bool = True) -> bool:
        """
        Send trigger_listen command to device.
        silent=True: multi-turn follow-up (no wake sound, no speaker suppress)
        silent=False: remote enrollment (play wake sound)
        """
        return await self.send_command({
            "type": "trigger_listen",
            "silent": silent,
        })

    def start_audio_session(self, codec: str = "opus") -> WsAudioSession:
        """Create a new audio session within this persistent connection."""
        session = WsAudioSession(
            device_id=self.device_id,
            on_speech_complete=self.on_speech_complete,
            codec=codec,
        )
        self.audio_session = session
        return session

    def end_audio_session(self):
        """End the current audio session (connection stays open)."""
        if self.audio_session:
            self.audio_session._closed = True
            self.audio_session = None
            self._last_session_ended_at = time.time()


# ---------------------------------------------------------------------------
# WebSocket endpoint handler (persistent)
# ---------------------------------------------------------------------------
async def ws_audio_endpoint(
    websocket,
    device_id: str,
    token: str,
    on_speech_complete: Callable[[str, bytes], Awaitable[None]],
):
    """
    FastAPI WebSocket endpoint handler for persistent device connection.

    Supports both:
    - New persistent protocol (hello → idle → audio_start → opus → speech_end → idle → ...)
    - Legacy ephemeral protocol (connect → opus frames immediately → speech_end → disconnect)
    """
    from starlette.websockets import WebSocketState

    # 1. Validate token
    if config.DEVICE_API_TOKEN and token != config.DEVICE_API_TOKEN:
        logger.warning(f"WS audio: invalid token from device {device_id}")
        await websocket.close(code=4001, reason="Invalid token")
        return

    if not device_id:
        await websocket.close(code=4002, reason="Missing device_id")
        return

    # Note: opuslib is only needed for Opus codec devices (AtomS3R).
    # PCM codec devices (NabuVoice) work without it.
    if not _OPUS_AVAILABLE:
        logger.warning("WS audio: opuslib not installed — only PCM codec devices will work")

    # 2. Close previous connection for this device (if any)
    async with _connections_lock:
        old_conn = _persistent_connections.pop(device_id, None)
    if old_conn:
        old_conn._closed = True
        logger.info(f"Replacing previous persistent connection for device {device_id}")

    # 3. Accept WebSocket and create persistent connection
    await websocket.accept()

    conn = PersistentDeviceConnection(
        device_id=device_id,
        websocket=websocket,
        on_speech_complete=on_speech_complete,
    )

    # Register in persistent connections
    async with _connections_lock:
        _persistent_connections[device_id] = conn

    logger.info(f"Persistent WS connection established for device {device_id}")

    # 4. Send welcome
    await conn.send_command({"type": "welcome", "server_time": time.time()})

    # 5. Main receive loop (persistent — stays open until device disconnects)
    is_persistent = False  # Will be set to True when we receive "hello" or "audio_start"
    legacy_session_started = False  # For backward compat with ephemeral protocol

    try:
        last_activity = time.time()

        while not conn._closed:
            # Keepalive timeout: 120s of no activity → send ping
            # Hard timeout: 300s of no activity → close
            now = time.time()
            idle_seconds = now - last_activity

            # Audio session timeout check
            if conn.audio_session and not conn.audio_session._closed:
                session_elapsed = now - conn.audio_session._created_at
                if session_elapsed > conn.audio_session._timeout:
                    logger.warning(f"Audio session {conn.audio_session.session_id} timed out "
                                   f"after {session_elapsed:.0f}s")
                    # Deliver partial speech if any
                    had_speech = (conn.audio_session._speech_started
                                  and conn.audio_session._audio_buffer)
                    if had_speech:
                        try:
                            await websocket.send_json({"type": "speech_end"})
                        except Exception:
                            pass
                        await conn.audio_session.deliver_speech()
                    conn.end_audio_session()
                    # No speech delivered → handle based on live session state
                    if not had_speech:
                        if _is_device_in_live_session(device_id):
                            # Live session: re-trigger listen instead of closing
                            await conn.send_command({"type": "trigger_listen", "silent": True})
                            logger.info(f"Device {device_id}: session timeout without speech → "
                                        f"re-trigger (live session)")
                        else:
                            await conn.send_command({"type": "tts_done"})
                            logger.info(f"Device {device_id}: session timeout without speech → tts_done")
                    if not is_persistent:
                        break  # Legacy mode: close after session ends

                # No-audio timeout within session (10s)
                if conn.audio_session and conn.audio_session._last_audio_at:
                    audio_silence = now - conn.audio_session._last_audio_at
                    if audio_silence > 10.0:
                        logger.warning(f"Audio session {conn.audio_session.session_id}: "
                                       f"no audio for {audio_silence:.0f}s")
                        had_speech = (conn.audio_session._speech_started
                                      and conn.audio_session._audio_buffer)
                        if had_speech:
                            try:
                                await websocket.send_json({"type": "speech_end"})
                            except Exception:
                                pass
                            await conn.audio_session.deliver_speech()
                        conn.end_audio_session()
                        # No speech delivered → handle based on live session state
                        if not had_speech:
                            if _is_device_in_live_session(device_id):
                                await conn.send_command({"type": "trigger_listen", "silent": True})
                                logger.info(f"Device {device_id}: no-audio timeout → "
                                            f"re-trigger (live session)")
                            else:
                                await conn.send_command({"type": "tts_done"})
                                logger.info(f"Device {device_id}: no-audio timeout → tts_done")
                        if not is_persistent:
                            break

            # Keepalive for persistent connections
            if is_persistent and idle_seconds > 120:
                sent = await conn.send_command({"type": "ping"})
                if not sent:
                    break
                last_activity = time.time()

            # Receive message with timeout
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue

            last_activity = time.time()

            # Handle WebSocket disconnect
            if message.get("type") == "websocket.disconnect":
                logger.info(f"Device {device_id}: WebSocket disconnected")
                break

            # Handle binary message (audio frame: Opus or PCM)
            if "bytes" in message and message["bytes"]:
                audio_data = message["bytes"]

                # Auto-create session if binary arrives without audio_start (legacy compat)
                if not conn.audio_session:
                    # Cooldown: ignore stray frames arriving shortly after a session ends
                    # (prevents spurious legacy sessions from leftover device frames)
                    if conn._last_session_ended_at and (time.time() - conn._last_session_ended_at) < 2.0:
                        logger.debug(f"Device {device_id}: ignoring stray binary frame "
                                     f"({time.time() - conn._last_session_ended_at:.1f}s after session end)")
                        continue
                    if not legacy_session_started:
                        logger.info(f"Device {device_id}: legacy mode — auto-creating audio session on first binary frame")
                        legacy_session_started = True
                    session = conn.start_audio_session()
                    await websocket.send_json({"type": "ready", "session_id": session.session_id})
                    logger.info(f"Audio session {session.session_id} auto-started for device {device_id}")

                session = conn.audio_session
                if session and not session._closed:
                    session._last_audio_at = time.time()
                    session._frames_received += 1

                    if session._frames_received <= 3 or session._frames_received % 500 == 0:
                        logger.info(f"[{session.session_id}] {session.codec.upper()} frame "
                                    f"#{session._frames_received}: "
                                    f"{len(audio_data)} bytes")

                    # Decode based on codec
                    if session.codec == "pcm":
                        pcm_float = session.decode_pcm_frame(audio_data)
                    else:
                        pcm_float = session.decode_opus_frame(audio_data)
                    if pcm_float is None:
                        continue

                    speech_complete = session.process_audio(pcm_float)

                    if speech_complete:
                        try:
                            await websocket.send_json({"type": "speech_end"})
                        except Exception:
                            pass

                        await session.deliver_speech()
                        conn.end_audio_session()

                        if not is_persistent:
                            break  # Legacy mode: close after speech

            # Handle text message (JSON control)
            elif "text" in message and message["text"]:
                try:
                    ctrl = json.loads(message["text"])
                    msg_type = ctrl.get("type", "")

                    if msg_type == "hello":
                        # Device announces persistent mode
                        is_persistent = True
                        conn.firmware_version = ctrl.get("fw", "unknown")
                        # Determine device_type from firmware version string
                        fw_lower = conn.firmware_version.lower()
                        if "voicepe" in fw_lower or "nabuvoice" in fw_lower:
                            conn.device_type = "NabuVoice"
                        else:
                            conn.device_type = "AtomS3R"
                        logger.info(f"Device {device_id}: persistent mode (fw={conn.firmware_version}, type={conn.device_type})")

                        # Auto-register device if not already in database + update heartbeat
                        try:
                            from database import get_voice_device, register_unknown_voice_device, update_voice_device_heartbeat
                            dev = get_voice_device(device_id)
                            if not dev:
                                dev = register_unknown_voice_device(
                                    device_id=device_id,
                                    firmware_version=conn.firmware_version,
                                )
                                logger.info(f"Device {device_id}: auto-registered in database "
                                            f"(type={conn.device_type}, fw={conn.firmware_version})")
                            else:
                                # Update last_seen_at on WS connect (keeps device online)
                                update_voice_device_heartbeat(
                                    device_id=device_id,
                                    firmware_version=conn.firmware_version,
                                )
                        except Exception as e:
                            logger.error(f"Device {device_id}: auto-registration failed: {e}")
                            dev = None

                        # Device reconnected — clean up stale live session if any
                        try:
                            from main import get_live_session, end_live_session
                            stale_session = get_live_session(device_id)
                            if stale_session:
                                logger.warning(f"Device {device_id}: stale live session detected on reconnect — ending it")
                                await end_live_session(device_id, reason="device_reconnect")
                        except Exception as e:
                            logger.debug(f"Device {device_id}: live session cleanup on hello: {e}")

                        # Push saved config to device (sensitivity, volume, speaker type, etc.)
                        try:
                            if dev is None:
                                from database import get_voice_device
                                dev = get_voice_device(device_id)
                            if dev:
                                config_msg = {"type": "config_update"}
                                if dev.wake_word_sensitivity is not None:
                                    config_msg["wake_word_sensitivity"] = dev.wake_word_sensitivity
                                if dev.speaker_volume is not None:
                                    config_msg["speaker_volume"] = dev.speaker_volume
                                # Tell device whether to use internal speaker for TTS
                                config_msg["speaker_type"] = "internal" if dev.use_internal_speaker else "alexa"
                                await conn.send_command(config_msg)
                                logger.info(f"Device {device_id}: pushed saved config "
                                            f"(sensitivity={dev.wake_word_sensitivity}, "
                                            f"volume={dev.speaker_volume}, "
                                            f"speaker_type={'internal' if dev.use_internal_speaker else 'alexa'})")
                        except Exception as e:
                            logger.debug(f"Device {device_id}: config push on hello failed: {e}")

                    elif msg_type == "audio_start":
                        # Device wants to start an audio session
                        is_persistent = True  # Confirm persistent mode
                        if conn.audio_session and not conn.audio_session._closed:
                            logger.warning(f"Device {device_id}: audio_start while session active — ending previous")
                            # Don't send tts_done here — device is already starting a new session
                            conn.end_audio_session()

                        # Update last_seen_at on every voice interaction
                        try:
                            from database import update_voice_device_heartbeat
                            update_voice_device_heartbeat(device_id=device_id)
                        except Exception:
                            pass

                        # Parse codec: "pcm" (NabuVoice) or "opus" (AtomS3R, default)
                        codec = ctrl.get("codec", "opus")
                        session = conn.start_audio_session(codec=codec)
                        await websocket.send_json({"type": "ready", "session_id": session.session_id})
                        logger.info(f"Audio session {session.session_id} started for device {device_id} (codec={codec})")

                    elif msg_type == "audio_end":
                        # Device voluntarily ends audio session (user pressed button)
                        # Kill immediately — do NOT process/deliver any speech
                        if conn.audio_session and not conn.audio_session._closed:
                            conn.end_audio_session()
                            await conn.send_command({"type": "tts_done"})
                            logger.info(f"Device {device_id}: audio session killed by device (audio_end)")

                    elif msg_type == "speaker_stop":
                        # Double-tap emergency stop: stop the speaker associated with this device
                        logger.info(f"Device {device_id}: speaker_stop (double-tap)")

                        # If device is in a live session, end it
                        try:
                            from main import get_live_session, end_live_session
                            if get_live_session(device_id):
                                logger.info(f"Device {device_id}: speaker_stop during live session — ending session")
                                await end_live_session(device_id, reason="button_stop")
                                # end_live_session handles tts_done via schedule_post_tts
                                # Skip normal speaker_stop flow
                                continue
                        except Exception as e:
                            logger.error(f"Live session check on speaker_stop failed: {e}")

                        try:
                            from database import get_voice_device
                            from integrations import mute_speaker_for_stop
                            dev = get_voice_device(device_id)
                            if dev and dev.output_speaker and dev.location_id:
                                loc = dev.location_id
                                spk = dev.output_speaker
                                # Alexa TTS (notify.alexa_media) is a cloud-driven behavior
                                # that cannot be interrupted via media_stop or play_media.
                                # Strategy: mute immediately, auto-unmute when speak() is called next.
                                await mute_speaker_for_stop(loc, spk)
                                # Clear speaking_state for this device's room
                                from main import speaking_state, speaking_state_lock
                                async with speaking_state_lock:
                                    room = dev.friendly_name
                                    if room and room in speaking_state:
                                        del speaking_state[room]
                                        logger.info(f"Cleared speaking_state for room: {room}")
                            else:
                                logger.warning(f"speaker_stop: device {device_id} has no output_speaker configured")
                            # Cancel any pending post-TTS task (multi-turn trigger, etc.)
                            from main import cancel_pending_tts_task
                            cancel_pending_tts_task(device_id)
                        except Exception as e:
                            logger.error(f"speaker_stop failed for device {device_id}: {e}")
                        # Tell device to go back to IDLE
                        await conn.send_command({"type": "tts_done"})

                    elif msg_type == "state":
                        state = ctrl.get("state", "unknown")
                        conn.last_state = state
                        conn.last_state_at = time.time()
                        logger.debug(f"Device {device_id} state: {state}")

                    elif msg_type == "volume_change":
                        direction = ctrl.get("direction", "up")
                        logger.info(f"Device {device_id}: volume_change {direction}")
                        try:
                            await _handle_volume_change(device_id, direction)
                        except Exception as e:
                            logger.error(f"volume_change failed for {device_id}: {e}")

                    elif msg_type == "pong":
                        conn.last_pong_at = time.time()

                    elif msg_type == "end":
                        # Legacy: client requested end
                        logger.info(f"Device {device_id}: end requested")
                        break

                except json.JSONDecodeError:
                    pass

    except Exception as e:
        if not conn._closed:
            logger.error(f"WS error for device {device_id}: {e}")

    finally:
        # Deliver partial speech if audio session was active
        if conn.audio_session and conn.audio_session._speech_started and conn.audio_session._audio_buffer:
            logger.info(f"Device {device_id}: delivering partial speech on disconnect")
            try:
                await websocket.send_json({"type": "speech_end"})
            except Exception:
                pass
            await conn.audio_session.deliver_speech()

        conn._closed = True
        conn.end_audio_session()

        # Remove from persistent connections
        async with _connections_lock:
            if _persistent_connections.get(device_id) is conn:
                del _persistent_connections[device_id]

        # Close WebSocket
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
        except Exception:
            pass

        duration = time.time() - conn.connected_at
        logger.info(f"Device {device_id} disconnected "
                    f"(was connected {duration:.0f}s, persistent={is_persistent})")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def trigger_device_listen(device_id: str, silent: bool = True) -> bool:
    """
    Trigger a device to start listening via persistent WS.
    Falls back to wakeword-server REST API if device not directly connected.

    Args:
        device_id: MAC address of target device (uppercase)
        silent: True for multi-turn follow-up (no wake sound),
                False for enrollment/manual trigger (with wake sound)

    Returns:
        True if command was sent successfully, False if device not connected.
    """
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _persistent_connections.get(device_id)

    if conn:
        result = await conn.trigger_listen(silent=silent)
        logger.info(f"trigger_device_listen({device_id}, silent={silent}): {'OK' if result else 'FAILED'}")
        return result

    # Device not directly connected — try via wakeword-server REST
    result = await _trigger_via_wakeword_server(device_id, silent)
    if result:
        logger.info(f"trigger_device_listen({device_id}, silent={silent}): OK (via wakeword-server)")
        return True

    logger.warning(f"trigger_device_listen: device {device_id} not connected (direct or via wakeword-server)")
    return False


async def _trigger_via_wakeword_server(device_id: str, silent: bool) -> bool:
    """Try to trigger_listen via wakeword-server REST API."""
    try:
        from config import WAKEWORD_SERVER_URLS, DEVICE_API_TOKEN
        from database import get_voice_device
        if not WAKEWORD_SERVER_URLS:
            return False

        device = get_voice_device(device_id)
        wakeword_url = None
        if device and device.location_id:
            wakeword_url = WAKEWORD_SERVER_URLS.get(device.location_id)
        if not wakeword_url and len(WAKEWORD_SERVER_URLS) == 1:
            wakeword_url = next(iter(WAKEWORD_SERVER_URLS.values()))
        if not wakeword_url:
            return False

        import httpx
        headers = {}
        if DEVICE_API_TOKEN:
            headers["Authorization"] = f"Bearer {DEVICE_API_TOKEN}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{wakeword_url}/api/trigger_listen/{device_id}",
                params={"silent": str(silent).lower()},
                headers=headers,
            )
            return resp.status_code == 200
    except Exception as e:
        logger.debug(f"trigger_via_wakeword_server failed for {device_id}: {e}")
        return False


async def get_connected_devices() -> list:
    """Return list of currently connected device_ids."""
    async with _connections_lock:
        return list(_persistent_connections.keys())


async def get_device_state(device_id: str) -> Optional[str]:
    """Get last reported state of a device, or None if not connected."""
    async with _connections_lock:
        conn = _persistent_connections.get(device_id.upper().strip())
    return conn.last_state if conn else None


async def get_active_session_count() -> int:
    """Ritorna numero di sessioni audio attive (per health/metrics)."""
    async with _connections_lock:
        count = 0
        for conn in _persistent_connections.values():
            if conn.audio_session and not conn.audio_session._closed:
                count += 1
        return count


async def get_persistent_connection_count() -> int:
    """Ritorna numero di connessioni persistenti attive."""
    async with _connections_lock:
        return len(_persistent_connections)


async def notify_tts_start(device_id: str) -> bool:
    """
    Notify a device that TTS playback is about to start.
    Device uses this to prepare internal speaker and start queuing Opus frames.
    MUST be called before send_tts_frame() — otherwise device drops all frames.

    Returns True if command was sent, False if device not connected.
    """
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _persistent_connections.get(device_id)

    if not conn:
        return False

    result = await conn.send_command({"type": "tts_start"})
    if result:
        logger.info(f"notify_tts_start({device_id}): sent")
    return result


async def notify_tts_done(device_id: str) -> bool:
    """
    Notify a device that TTS playback is complete (response delivered).
    Device uses this to transition from BUSY → IDLE state.

    Returns True if command was sent, False if device not connected.
    """
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _persistent_connections.get(device_id)

    if not conn:
        return False

    result = await conn.send_command({"type": "tts_done"})
    if result:
        logger.info(f"notify_tts_done({device_id}): sent")
    return result


async def send_tts_frame(device_id: str, opus_data: bytes) -> bool:
    """
    Invia un singolo frame Opus TTS al device via WebSocket persistente.
    Il device decodifica e riproduce il frame sullo speaker interno.

    Returns True se inviato, False se device non connesso.
    """
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _persistent_connections.get(device_id)

    if not conn or conn._closed:
        return False

    try:
        await conn.websocket.send_bytes(opus_data)
        return True
    except Exception as e:
        logger.warning(f"send_tts_frame({device_id}): invio fallito: {e}")
        return False


async def push_config_to_device(device_id: str, config: dict) -> bool:
    """
    Push configuration update to a connected device via WebSocket.
    The device applies the config at runtime without reboot.

    Args:
        device_id: MAC address of target device
        config: dict with config keys to update, e.g.:
                {"wake_word_sensitivity": 0.82}

    Returns True if sent, False if device not connected.
    """
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _persistent_connections.get(device_id)

    if not conn:
        logger.warning(f"push_config_to_device: device {device_id} not connected")
        return False

    result = await conn.send_command({
        "type": "config_update",
        **config
    })
    if result:
        logger.info(f"push_config_to_device({device_id}): {config}")
    return result


# ---------------------------------------------------------------------------
# Volume change handler (NabuVoice rotary encoder)
# ---------------------------------------------------------------------------
# Debounced: accumulates rapid clicks and sends a single volume_set after
# 400ms of quiet.  This avoids the "read stale state" problem where HA hasn't
# applied the previous volume_set yet when the next request arrives.
# ---------------------------------------------------------------------------

_volume_pending: dict[str, int] = {}          # device_id → accumulated steps (+/-)
_volume_tasks: dict[str, asyncio.Task] = {}   # device_id → pending flush task
_VOLUME_DEBOUNCE_S = 0.15                     # short debounce — just enough to batch rapid clicks
_VOLUME_MAX_STEPS = 5                         # flush immediately after N accumulated steps


async def _handle_volume_change(device_id: str, direction: str):
    """Accumulate a volume click.  Actual HA call is debounced via volume_up/down."""
    delta = +1 if direction == "up" else -1
    _volume_pending[device_id] = _volume_pending.get(device_id, 0) + delta

    # Flush immediately if we hit max steps (responsive feel)
    if abs(_volume_pending[device_id]) >= _VOLUME_MAX_STEPS:
        old = _volume_tasks.pop(device_id, None)
        if old and not old.done():
            old.cancel()
        await _flush_volume(device_id, immediate=True)
        return

    # Cancel previous timer for this device, start a new one
    old = _volume_tasks.pop(device_id, None)
    if old and not old.done():
        old.cancel()
    _volume_tasks[device_id] = asyncio.create_task(_flush_volume(device_id))


async def _flush_volume(device_id: str, immediate: bool = False):
    """Wait for debounce period, then send volume_up/down calls to HA."""
    if not immediate:
        await asyncio.sleep(_VOLUME_DEBOUNCE_S)

    steps = _volume_pending.pop(device_id, 0)
    _volume_tasks.pop(device_id, None)
    if steps == 0:
        return

    from device_api import get_device_speaker_config
    from multi_ha import multi_ha

    device_config = get_device_speaker_config(device_id)
    if not device_config:
        logger.warning(f"volume_change: device {device_id} not configured")
        return

    location_id = device_config.get("location_id")
    # Use volume_speaker if configured (e.g. Bose native entity), else fall back to output_speaker
    target_speaker = device_config.get("volume_speaker") or device_config.get("output_speaker")
    if not target_speaker or not location_id:
        logger.warning(f"volume_change: no speaker configured for {device_id}")
        return

    # Use relative volume_up/volume_down — no need to read current volume from HA
    service = "volume_up" if steps > 0 else "volume_down"
    count = abs(steps)

    for i in range(count):
        await multi_ha.call_service(
            location_id, "media_player", service,
            {"entity_id": target_speaker}
        )

    logger.info(f"volume_change: {target_speaker} {service} ×{count}")


# ---------------------------------------------------------------------------
# Live session helpers
# ---------------------------------------------------------------------------

def _is_device_in_live_session(device_id: str) -> bool:
    """Check if a device is currently in a live session (non-async, no import cycle)."""
    try:
        from main import get_live_session
        return get_live_session(device_id) is not None
    except Exception:
        return False


async def notify_live_session_start(device_id: str) -> bool:
    """Notify device (and relay) that a live session is starting."""
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _persistent_connections.get(device_id)
    if not conn:
        return False
    result = await conn.send_command({"type": "live_session_start"})
    if result:
        logger.info(f"notify_live_session_start({device_id}): sent")
    return result


async def notify_live_session_end(device_id: str) -> bool:
    """Notify device (and relay) that a live session has ended."""
    device_id = device_id.upper().strip()
    async with _connections_lock:
        conn = _persistent_connections.get(device_id)
    if not conn:
        return False
    result = await conn.send_command({"type": "live_session_end"})
    if result:
        logger.info(f"notify_live_session_end({device_id}): sent")
    return result
