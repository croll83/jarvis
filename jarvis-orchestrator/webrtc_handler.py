"""
WebRTC Handler — Server-side audio reception via Opus + Silero VAD.

Flusso:
  1. Firmware POST /webrtc/offer  (SDP offer JSON)
  2. Server crea RTCPeerConnection, aggiunge transceiver recvonly
  3. Risponde con SDP answer
  4. Audio Opus arriva via RTP/UDP — aiortc decodifica a PCM 48 kHz
  5. Resample 48 kHz → 16 kHz, accumula buffer
  6. Silero VAD (ONNX) detecta speech-start → speech-end
  7. callback on_speech_complete(device_id, pcm_bytes_16k)
  8. Server chiude PeerConnection

Note:
  - Silero VAD caricato direttamente via onnxruntime (no torch.hub, no torchaudio)
    per evitare problemi con libtorchaudio.so / CUDA stub nel container CPU-only.
  - Ogni sessione ha il proprio stato RNN (h/c) per isolamento thread-safe.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Callable, Awaitable, Optional, Dict

import numpy as np
import onnxruntime as ort
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from scipy.signal import resample_poly

import config

logger = logging.getLogger("JARVIS_WEBRTC")

# Prefissi IP da escludere dai candidate ICE nella SDP answer.
# Docker bridge (172.17.x) e Tailscale (100.x) non sono raggiungibili
# dal firmware AtomS3R che è dietro NAT residenziale.
_ICE_CANDIDATE_BLOCKED_PREFIXES = ("172.17.", "172.18.", "100.")


def _filter_sdp_candidates(sdp: str) -> str:
    """
    Rimuove dalla SDP le righe a=candidate con IP Docker/Tailscale.
    Mantiene solo candidate con IP pubblico (realmente raggiungibili).
    """
    import re
    filtered_lines = []
    removed = 0
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            # Formato: a=candidate:... <priority> <ip> <port> ...
            # L'IP è il 5° campo (indice 4) separato da spazi
            parts = line.split()
            if len(parts) >= 5:
                ip = parts[4]
                if any(ip.startswith(prefix) for prefix in _ICE_CANDIDATE_BLOCKED_PREFIXES):
                    removed += 1
                    logger.debug(f"Filtered SDP candidate: {ip}")
                    continue
        filtered_lines.append(line)

    if removed:
        logger.info(f"Filtered {removed} unreachable ICE candidates from SDP answer")
    return "\r\n".join(filtered_lines)


# ---------------------------------------------------------------------------
# Silero VAD via ONNX Runtime (no torch dependency)
# ---------------------------------------------------------------------------
_vad_onnx_path: Optional[str] = None


class SileroVADOnnx:
    """
    Wrapper leggero per Silero VAD ONNX.
    Mantiene lo stato RNN (h, c) internamente.
    Ogni sessione deve avere la propria istanza.
    """

    def __init__(self, model_path: str):
        self._sess = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
        )
        # Stato RNN iniziale: shape (2, 1, 128) — [h, c] per 1 batch
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def __call__(self, audio_chunk: np.ndarray) -> float:
        """
        Esegui VAD su un chunk audio float32 (512 samples @ 16kHz).
        Ritorna probabilità di speech [0.0, 1.0].
        """
        # Input shape: (1, num_samples)
        inp = audio_chunk.reshape(1, -1).astype(np.float32)

        out, new_state = self._sess.run(
            ["output", "stateN"],
            {
                "input": inp,
                "state": self._state,
                "sr": self._sr,
            },
        )
        self._state = new_state
        return float(out[0, 0])

    def reset(self):
        """Reset stato RNN (per nuova sessione/utterance)."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)


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
_active_sessions: Dict[str, "WebRTCSession"] = {}
_sessions_lock = asyncio.Lock()


async def _cleanup_previous_session(device_id: str):
    """Chiude sessione precedente dello stesso device (se esiste)."""
    async with _sessions_lock:
        old = _active_sessions.pop(device_id, None)
    if old:
        logger.info(f"Closing previous WebRTC session for device {device_id}")
        await old.close()


# ---------------------------------------------------------------------------
# WebRTCSession
# ---------------------------------------------------------------------------
class WebRTCSession:
    """
    Gestisce una singola sessione WebRTC recvonly.

    - Riceve audio Opus via RTP (aiortc decodifica a PCM 48 kHz s16)
    - Resample 48→16 kHz
    - Silero VAD su chunk da 512 samples @ 16 kHz
    - Detecta speech start (min_speech_ms) → speech end (min_silence_ms)
    - Chiama callback con PCM 16 kHz concatenato
    """

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
        self._timeout = config.WEBRTC_SESSION_TIMEOUT
        self._vad_threshold = config.WEBRTC_VAD_THRESHOLD
        self._min_silence_ms = config.WEBRTC_VAD_MIN_SILENCE_MS
        self._min_speech_ms = config.WEBRTC_VAD_MIN_SPEECH_MS

        # VAD state (per-session ONNX instance per RNN state isolation)
        self._vad = _new_vad_instance()

        # Audio buffer (16 kHz PCM float32)
        self._audio_buffer: list[np.ndarray] = []
        self._vad_chunk_buffer = np.array([], dtype=np.float32)

        # Speech state
        self._speech_started = False
        self._speech_frames = 0  # frames con speech consecutivo
        self._silence_frames = 0  # frames con silenzio consecutivo dopo speech
        self._speech_start_time: Optional[float] = None

        # Timing
        self._created_at = time.time()
        self._last_audio_at: Optional[float] = None

        # PeerConnection
        stun = RTCIceServer(urls=[config.STUN_SERVER])
        self._pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=[stun])
        )

        # Lifecycle
        self._closed = False
        self._timeout_task: Optional[asyncio.Task] = None
        self._consume_task: Optional[asyncio.Task] = None

        # Setup event handlers
        self._pc.on("connectionstatechange", self._on_connection_state_change)

    async def handle_offer(self, sdp_offer: str) -> str:
        """
        Processa SDP offer, configura transceiver recvonly, ritorna SDP answer.
        Full ICE gathering (no trickle) — SDP completo in un singolo scambio.
        """
        offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
        await self._pc.setRemoteDescription(offer)

        # Il firmware invia audio (sendrecv), il server riceve solo.
        # setRemoteDescription crea già il transceiver dall'offer,
        # NON creare un secondo — cambia direction su quello esistente.
        for transceiver in self._pc.getTransceivers():
            if transceiver.kind == "audio":
                transceiver.direction = "recvonly"
                break

        # Crea answer
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        # Aspetta ICE gathering complete (full ICE, no trickle)
        await self._wait_ice_gathering()

        # Filtra SDP: rimuovi candidate Docker bridge (172.17.x) e Tailscale (100.x)
        # che non sono raggiungibili dal firmware dietro NAT
        sdp = self._pc.localDescription.sdp
        sdp = _filter_sdp_candidates(sdp)

        # Avvia timeout task
        self._timeout_task = asyncio.create_task(self._session_timeout())

        logger.info(f"WebRTC session {self.session_id} created for device {self.device_id}")
        return sdp

    async def _wait_ice_gathering(self):
        """Aspetta che ICE gathering sia completo (max 10s)."""
        if self._pc.iceGatheringState == "complete":
            return

        ice_complete = asyncio.Event()

        @self._pc.on("icegatheringstatechange")
        def on_ice():
            if self._pc.iceGatheringState == "complete":
                ice_complete.set()

        try:
            await asyncio.wait_for(ice_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(f"ICE gathering timeout for session {self.session_id}, proceeding with partial candidates")

    def _on_connection_state_change(self):
        """Handler per cambiamenti di stato della connessione."""
        state = self._pc.connectionState
        logger.info(f"WebRTC session {self.session_id} connection state: {state}")

        if state in ("failed", "disconnected", "closed"):
            asyncio.ensure_future(self._handle_disconnect(state))

    async def _handle_disconnect(self, reason: str):
        """Gestisce disconnessione — invia audio accumulato se c'era speech."""
        if self._closed:
            return

        logger.info(f"WebRTC session {self.session_id} disconnected: {reason}")

        # Se c'era speech in corso, invia comunque il buffer
        if self._speech_started and self._audio_buffer:
            await self._deliver_speech()

        await self.close()

    async def _session_timeout(self):
        """Safety timeout — chiude sessione dopo WEBRTC_SESSION_TIMEOUT secondi."""
        try:
            await asyncio.sleep(self._timeout)
            if not self._closed:
                logger.warning(f"WebRTC session {self.session_id} timed out after {self._timeout}s")
                # Se c'era speech in corso, invia il buffer
                if self._speech_started and self._audio_buffer:
                    await self._deliver_speech()
                await self.close()
        except asyncio.CancelledError:
            pass

    def setup_track_handler(self):
        """Registra handler per track audio in arrivo."""

        @self._pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                logger.info(f"WebRTC session {self.session_id} received audio track")
                self._consume_task = asyncio.ensure_future(self._consume_audio(track))

    async def _consume_audio(self, track):
        """Loop principale: riceve frame audio, resampla, esegue VAD."""
        # VAD chunk size: 512 samples @ 16 kHz = 32ms
        VAD_CHUNK_SIZE = 512
        # Calcolo frame duration a 16 kHz per speech/silence detection
        chunk_duration_ms = (VAD_CHUNK_SIZE / 16000) * 1000  # 32ms

        try:
            while not self._closed:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Nessun audio per 5s — potrebbe essere disconnesso
                    if self._speech_started and self._audio_buffer:
                        logger.info(f"No audio for 5s, delivering accumulated speech")
                        await self._deliver_speech()
                        await self.close()
                    continue
                except Exception as e:
                    if not self._closed:
                        logger.debug(f"Track recv error: {e}")
                    break

                self._last_audio_at = time.time()

                # aiortc AudioFrame → numpy float32
                # frame.to_ndarray() ritorna int16, shape (samples, channels)
                pcm_48k = frame.to_ndarray().astype(np.float32) / 32768.0

                # Mono (media canali se stereo)
                if pcm_48k.ndim > 1:
                    pcm_48k = pcm_48k.mean(axis=1)

                # Resample 48kHz → 16kHz (ratio 1:3)
                pcm_16k = resample_poly(pcm_48k, up=1, down=3).astype(np.float32)

                # Accumula nel buffer VAD
                self._vad_chunk_buffer = np.concatenate([self._vad_chunk_buffer, pcm_16k])

                # Processa chunk da 512 samples
                while len(self._vad_chunk_buffer) >= VAD_CHUNK_SIZE:
                    chunk = self._vad_chunk_buffer[:VAD_CHUNK_SIZE]
                    self._vad_chunk_buffer = self._vad_chunk_buffer[VAD_CHUNK_SIZE:]

                    # Silero VAD inference (ONNX, no torch)
                    speech_prob = self._vad(chunk)

                    is_speech = speech_prob > self._vad_threshold

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
                                logger.debug(f"Speech started in session {self.session_id}")
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
                                await self._deliver_speech()
                                await self.close()
                                return
                        else:
                            # Pre-speech: non accumulare, ma mantieni ultimi 300ms come lead-in
                            # per non tagliare l'inizio della parola
                            lead_in_chunks = int(300 / chunk_duration_ms)
                            self._audio_buffer.append(chunk)
                            if len(self._audio_buffer) > lead_in_chunks:
                                self._audio_buffer = self._audio_buffer[-lead_in_chunks:]

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error consuming audio in session {self.session_id}: {e}")
        finally:
            if self._speech_started and self._audio_buffer and not self._closed:
                await self._deliver_speech()
            if not self._closed:
                await self.close()

    async def _deliver_speech(self):
        """Concatena buffer, converte a PCM 16-bit bytes, chiama callback."""
        if not self._audio_buffer:
            return

        # Concatena tutti i chunk
        full_pcm = np.concatenate(self._audio_buffer)
        duration_s = len(full_pcm) / 16000

        logger.info(f"Delivering speech from session {self.session_id}: "
                    f"{duration_s:.1f}s, {len(full_pcm)} samples")

        # Converti float32 → int16 → bytes (formato atteso dalla pipeline STT)
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

    async def close(self):
        """Chiude PeerConnection e pulisce risorse."""
        if self._closed:
            return
        self._closed = True

        # Cancel tasks
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        if self._consume_task and not self._consume_task.done():
            self._consume_task.cancel()

        # Close PeerConnection
        try:
            await self._pc.close()
        except Exception as e:
            logger.debug(f"Error closing PeerConnection: {e}")

        # Rimuovi da active sessions
        async with _sessions_lock:
            if _active_sessions.get(self.device_id) is self:
                del _active_sessions[self.device_id]

        duration = time.time() - self._created_at
        logger.info(f"WebRTC session {self.session_id} closed (duration: {duration:.1f}s)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def handle_offer(
    device_id: str,
    sdp_offer: str,
    on_speech_complete: Callable[[str, bytes], Awaitable[None]],
) -> tuple[str, str]:
    """
    Entry point per nuovo SDP offer.

    Returns:
        (sdp_answer, session_id)
    """
    # Chiudi sessione precedente dello stesso device
    await _cleanup_previous_session(device_id)

    # Crea nuova sessione
    session = WebRTCSession(
        device_id=device_id,
        on_speech_complete=on_speech_complete,
    )

    # Registra handler track PRIMA di processare l'offer
    session.setup_track_handler()

    # Processa offer e ottieni answer
    sdp_answer = await session.handle_offer(sdp_offer)

    # Registra nella mappa active sessions
    async with _sessions_lock:
        _active_sessions[device_id] = session

    return sdp_answer, session.session_id


async def get_active_session_count() -> int:
    """Ritorna numero di sessioni WebRTC attive (per health/metrics)."""
    async with _sessions_lock:
        return len(_active_sessions)
