"""
WebSocket Audio Handler -- Server-side Opus reception via WebSocket + Silero VAD.

Flusso:
  1. Firmware connette a ws://.../ws/audio?device_id=...&token=...
  2. Server accetta, invia {"type":"ready","session_id":"..."}
  3. Firmware invia frame Opus binari (20ms each, ~30-80 bytes)
  4. Server decodifica Opus a PCM 16 kHz via opuslib (NO resampling)
  5. Silero VAD (ONNX) detecta speech-start -> speech-end
  6. callback on_speech_complete(device_id, pcm_bytes_16k)
  7. Server invia {"type":"speech_end"}, chiude WebSocket

Note:
  - Silero VAD caricato direttamente via onnxruntime (no torch.hub, no torchaudio)
    per evitare problemi con libtorchaudio.so / CUDA stub nel container CPU-only.
  - Ogni sessione ha il proprio stato RNN (h/c) per isolamento thread-safe.
  - Opus decodificato a 16 kHz direttamente (il firmware encoda a 16 kHz).
    Con WebRTC/aiortc serviva resample 48->16 kHz via scipy. Eliminato.
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
# Silero VAD ONNX implementation for speech detection
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
# Active sessions tracking
# ---------------------------------------------------------------------------
_active_sessions: Dict[str, "WsAudioSession"] = {}
_sessions_lock = asyncio.Lock()


async def _cleanup_previous_session(device_id: str):
    """Chiude sessione precedente dello stesso device (se esiste)."""
    async with _sessions_lock:
        old = _active_sessions.pop(device_id, None)
    if old:
        logger.info(f"Closing previous WS audio session for device {device_id}")
        old._closed = True


# ---------------------------------------------------------------------------
# WsAudioSession
# ---------------------------------------------------------------------------
class WsAudioSession:
    """
    Gestisce una singola sessione audio WebSocket.

    - Riceve frame Opus binari via WebSocket
    - Decodifica Opus -> PCM 16 kHz mono (opuslib)
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
    ):
        self.device_id = device_id
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.on_speech_complete = on_speech_complete

        # Config
        self._timeout = getattr(config, 'WS_AUDIO_SESSION_TIMEOUT', 60)
        self._vad_threshold = config.VAD_THRESHOLD
        self._min_silence_ms = config.VAD_MIN_SILENCE_MS
        self._min_speech_ms = config.VAD_MIN_SPEECH_MS

        # Opus decoder: 16 kHz mono (matches firmware encoder)
        if _OPUS_AVAILABLE:
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
        self._opus_frames_received = 0

    def decode_opus_frame(self, opus_data: bytes) -> Optional[np.ndarray]:
        """
        Decode one Opus frame to PCM float32 at 16 kHz.
        Returns None if decoder not available or decode error.
        """
        if not self._opus_decoder:
            return None
        try:
            # opuslib.Decoder.decode() returns bytes (int16 PCM)
            # frame_size=320 (20ms @ 16kHz)
            pcm_bytes = self._opus_decoder.decode(opus_data, self.OPUS_FRAME_SAMPLES)
            pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
            pcm_float = pcm_int16.astype(np.float32) / 32768.0

            # Debug: log RMS of decoded audio every 50 frames
            if self._opus_frames_received % 50 == 1:
                rms = float(np.sqrt(np.mean(pcm_float ** 2)))
                logger.info(f"[{self.session_id}] Decoded frame #{self._opus_frames_received}: "
                            f"{len(pcm_bytes)} bytes -> {len(pcm_int16)} samples, "
                            f"RMS={rms:.4f}, max={float(np.max(np.abs(pcm_float))):.4f}")

            return pcm_float
        except Exception as e:
            if self._opus_frames_received <= 3:
                logger.warning(f"[{self.session_id}] Opus decode error on frame "
                               f"#{self._opus_frames_received}: {e}")
            return None

    def process_audio(self, pcm_float: np.ndarray) -> bool:
        """
        Feed PCM float32 to VAD. Returns True when speech is complete.

        VAD logic identical to WebRTCSession._consume_audio inner loop:
        - Pre-speech lead-in (300ms buffer)
        - min_speech_ms consecutive speech to confirm start
        - min_silence_ms consecutive silence after speech to confirm end
        """
        chunk_duration_ms = (self.VAD_CHUNK_SIZE / 16000) * 1000  # 32ms

        # Accumulate in VAD chunk buffer
        self._vad_chunk_buffer = np.concatenate([self._vad_chunk_buffer, pcm_float])

        # Process all available 512-sample chunks
        while len(self._vad_chunk_buffer) >= self.VAD_CHUNK_SIZE:
            chunk = self._vad_chunk_buffer[:self.VAD_CHUNK_SIZE]
            self._vad_chunk_buffer = self._vad_chunk_buffer[self.VAD_CHUNK_SIZE:]

            # Silero VAD inference (ONNX)
            speech_prob = self._vad(chunk)
            is_speech = speech_prob > self._vad_threshold

            # Debug: log every 50th VAD chunk (~1.6s) to monitor
            if self._opus_frames_received % 50 == 1:
                logger.info(f"[{self.session_id}] VAD prob={speech_prob:.3f} "
                            f"thresh={self._vad_threshold} is_speech={is_speech} "
                            f"started={self._speech_started} "
                            f"speech_f={self._speech_frames} sil_f={self._silence_frames}")

            if is_speech:
                self._silence_frames = 0
                self._speech_frames += 1

                # Accumula audio
                self._audio_buffer.append(chunk)

                # Verifica speech start (min durata consecutiva)
                if not self._speech_started:
                    total_speech_ms = self._speech_frames * chunk_duration_ms
                    if total_speech_ms >= self._min_speech_ms:
                        self._speech_started = True
                        self._speech_start_time = time.time()
                        logger.info(f"Speech started in session {self.session_id}")
            else:
                self._speech_frames = 0

                if self._speech_started:
                    # Accumula anche il silenzio (padding naturale)
                    self._audio_buffer.append(chunk)
                    self._silence_frames += 1

                    # Verifica speech end (silenzio sufficiente)
                    total_silence_ms = self._silence_frames * chunk_duration_ms
                    if total_silence_ms >= self._min_silence_ms:
                        logger.info(f"Speech ended in session {self.session_id} "
                                    f"(silence {total_silence_ms:.0f}ms)")
                        return True  # Speech complete
                else:
                    # Pre-speech: mantieni ultimi 300ms come lead-in
                    lead_in_chunks = int(300 / chunk_duration_ms)
                    self._audio_buffer.append(chunk)
                    if len(self._audio_buffer) > lead_in_chunks:
                        self._audio_buffer = self._audio_buffer[-lead_in_chunks:]

        return False  # Speech not complete yet

    async def deliver_speech(self):
        """Concatena buffer, converte a PCM 16-bit bytes, chiama callback."""
        if not self._audio_buffer:
            return

        # Concatena tutti i chunk
        full_pcm = np.concatenate(self._audio_buffer)
        duration_s = len(full_pcm) / 16000

        logger.info(f"Delivering speech from session {self.session_id}: "
                    f"{duration_s:.1f}s, {len(full_pcm)} samples, "
                    f"opus_frames_received={self._opus_frames_received}")

        # Converti float32 -> int16 -> bytes (formato atteso dalla pipeline STT)
        pcm_int16 = (full_pcm * 32767).clip(-32768, 32767).astype(np.int16)
        pcm_bytes = pcm_int16.tobytes()

        # Svuota buffer
        self._audio_buffer = []
        self._speech_started = False
        self._speech_frames = 0
        self._silence_frames = 0

        # Callback asincrono
        try:
            await self.on_speech_complete(self.device_id, pcm_bytes)
        except Exception as e:
            logger.error(f"Error in speech callback for session {self.session_id}: {e}")


# ---------------------------------------------------------------------------
# WebSocket endpoint handler
# ---------------------------------------------------------------------------
async def ws_audio_endpoint(
    websocket,
    device_id: str,
    token: str,
    on_speech_complete: Callable[[str, bytes], Awaitable[None]],
):
    """
    FastAPI WebSocket endpoint handler for Opus audio streaming.

    Protocol:
      - Client sends binary messages (raw Opus frames, 20ms each)
      - Server sends JSON text messages for control
      - Server closes WebSocket after speech detected or timeout
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

    if not _OPUS_AVAILABLE:
        logger.error("WS audio: opuslib not available")
        await websocket.close(code=4003, reason="Opus decoder not available")
        return

    # 2. Cleanup previous session for this device
    await _cleanup_previous_session(device_id)

    # 3. Accept WebSocket and create session
    await websocket.accept()

    session = WsAudioSession(
        device_id=device_id,
        on_speech_complete=on_speech_complete,
    )

    # Register in active sessions
    async with _sessions_lock:
        _active_sessions[device_id] = session

    logger.info(f"WS audio session {session.session_id} started for device {device_id}")

    # 4. Send ready signal
    ready_msg = {"type": "ready", "session_id": session.session_id}
    logger.info(f"Sending ready signal to {device_id}: {ready_msg}")
    await websocket.send_json(ready_msg)
    logger.info(f"Ready signal sent to {device_id}")

    # 5. Receive and process audio
    try:
        start_time = time.time()
        last_audio_time = time.time()

        while not session._closed:
            # Check session timeout
            elapsed = time.time() - start_time
            if elapsed > session._timeout:
                logger.warning(f"WS audio session {session.session_id} timed out "
                               f"after {elapsed:.0f}s")
                break

            # Check no-audio timeout (10 seconds)
            silence = time.time() - last_audio_time
            if silence > 10.0:
                logger.warning(f"WS audio session {session.session_id}: no audio "
                               f"for {silence:.0f}s")
                break

            # Receive message with timeout
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue

            # Handle WebSocket disconnect
            if message.get("type") == "websocket.disconnect":
                logger.info(f"WS audio session {session.session_id}: client disconnected")
                break

            # Handle binary message (Opus frame)
            if "bytes" in message and message["bytes"]:
                opus_data = message["bytes"]
                last_audio_time = time.time()
                session._opus_frames_received += 1

                # Log first few frames for debug
                if session._opus_frames_received <= 3 or session._opus_frames_received % 500 == 0:
                    logger.info(f"[{session.session_id}] Opus frame "
                                f"#{session._opus_frames_received}: "
                                f"{len(opus_data)} bytes")

                # Decode Opus -> PCM float32 @ 16kHz
                pcm_float = session.decode_opus_frame(opus_data)
                if pcm_float is None:
                    continue

                # Feed to VAD
                speech_complete = session.process_audio(pcm_float)

                if speech_complete:
                    # Notify client
                    try:
                        await websocket.send_json({"type": "speech_end"})
                    except Exception:
                        pass  # Client may have already disconnected

                    # Deliver speech to pipeline
                    await session.deliver_speech()
                    break

            # Handle text message (control)
            elif "text" in message and message["text"]:
                try:
                    ctrl = json.loads(message["text"])
                    msg_type = ctrl.get("type", "")
                    if msg_type == "end":
                        logger.info(f"WS audio session {session.session_id}: "
                                    f"client requested end")
                        break
                except json.JSONDecodeError:
                    pass

    except Exception as e:
        if not session._closed:
            logger.error(f"WS audio error for session {session.session_id}: {e}")

    finally:
        # If speech was in progress, deliver it
        if session._speech_started and session._audio_buffer and not session._closed:
            logger.info(f"WS audio session {session.session_id}: delivering "
                        f"partial speech on disconnect")
            try:
                await websocket.send_json({"type": "speech_end"})
            except Exception:
                pass
            await session.deliver_speech()

        # Mark closed
        session._closed = True

        # Remove from active sessions
        async with _sessions_lock:
            if _active_sessions.get(device_id) is session:
                del _active_sessions[device_id]

        # Close WebSocket if still open
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
        except Exception:
            pass

        duration = time.time() - session._created_at
        logger.info(f"WS audio session {session.session_id} closed "
                    f"(duration: {duration:.1f}s, "
                    f"opus_frames: {session._opus_frames_received})")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def get_active_session_count() -> int:
    """Ritorna numero di sessioni WS audio attive (per health/metrics)."""
    async with _sessions_lock:
        return len(_active_sessions)
