"""Media di una sessione live hub per un Atom (solo tra accept del hub e fine sessione).

Uplink: mic Opus dell'Atom (decodificato da DeviceConnection) -> AEC -> PCM 16 kHz al hub,
piu' {"type":"vu"} al display ogni ~100 ms.
Downlink: voce del modello dal hub (PCM 24 kHz) -> 16 kHz -> coda -> pacer in tempo reale ->
render AEC + Opus -> Atom come i frame TTS di oggi. Il pacer serve all'AEC: il riferimento
deve entrare quando l'audio parte davvero verso l'Atom, non a raffica quando arriva dal hub.
"""

import asyncio
import collections
import logging
import math
import time
from typing import Awaitable, Callable, Optional

import numpy as np
import opuslib

from aec import EchoCanceller, create_echo_canceller
from config import OPUS_SAMPLE_RATE, OPUS_CHANNELS, OPUS_FRAME_SAMPLES
from hubrelay import HubRelay

logger = logging.getLogger("wakeword-server")

# Encoder come internal_tts.py dell'orchestrator (il firmware decodifica quei frame)
_OPUS_BITRATE = 30000
_OPUS_COMPLEXITY = 5
_FRAME_BYTES = OPUS_FRAME_SAMPLES * 2
_FRAME_S = OPUS_FRAME_SAMPLES / OPUS_SAMPLE_RATE  # 20 ms
# Pre-buffer sul device alla ripartenza del parlato (come BURST_FRAMES di internal_tts)
_DOWNLINK_PREBUFFER_FRAMES = 5
# Tetto della coda downlink (~60 s): Gemini manda piu' veloce del tempo reale
_DOWNLINK_MAX_FRAMES = 3000
_VU_INTERVAL_S = 0.1
_VU_FLOOR_DB = -60.0


class Resampler24kTo16k:
    """Resampler polifase 2/3 in streaming (24 kHz -> 48 kHz -> 16 kHz), solo numpy."""

    def __init__(self, taps: int = 96):
        n = np.arange(taps) - (taps - 1) / 2
        fc = 7000.0 / 48000.0  # passa-basso sotto i 8 kHz di Nyquist a 16 kHz
        h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(taps, 8.0)
        h *= 2.0 / h.sum()  # guadagno 2: compensa gli zeri inseriti nell'upsampling
        self._h = h.astype(np.float32)
        self._hist = np.zeros(taps - 1, dtype=np.float32)
        self._phase = 0  # indice, nel prossimo blocco a 48 kHz, del primo campione da tenere

    def process(self, pcm24: np.ndarray) -> np.ndarray:
        if len(pcm24) == 0:
            return np.zeros(0, dtype=np.int16)
        up = np.zeros(2 * len(pcm24), dtype=np.float32)
        up[::2] = pcm24
        buf = np.concatenate((self._hist, up))
        y = np.convolve(buf, self._h, mode="valid")  # len(y) == len(up)
        out = y[self._phase::3]
        self._phase = (self._phase - len(up)) % 3
        self._hist = buf[len(buf) - len(self._hist):]
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def vu_level(rms: float) -> int:
    """RMS int16 -> 0..255 su scala dB (-60 dBFS = 0, 0 dBFS = 255)."""
    if rms <= 0:
        return 0
    db = 20 * math.log10(rms / 32768.0)
    return int(round(max(0.0, min(1.0, (db - _VU_FLOOR_DB) / -_VU_FLOOR_DB)) * 255))


class HubLiveSession:
    """Pipeline audio di una sessione hub; i controlli restano in server.py."""

    def __init__(self, device_id: str, call_id: str, relay: HubRelay,
                 send_json: Callable[[dict], Awaitable[None]],
                 send_binary: Callable[[bytes], Awaitable[None]],
                 aec_backend: str, aec: Optional[EchoCanceller] = None):
        self.device_id = device_id
        self.call_id = call_id
        self.relay = relay
        self._send_json = send_json
        self._send_binary = send_binary
        # aec gia' pronto se il chiamante l'ha creato fuori dall'event loop (to_thread)
        self.aec = aec if aec is not None else create_echo_canceller(aec_backend)
        self._encoder = opuslib.Encoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS, opuslib.APPLICATION_VOIP)
        self._encoder.bitrate = _OPUS_BITRATE
        self._encoder.complexity = _OPUS_COMPLEXITY
        self._resampler = Resampler24kTo16k()
        self._down_tail = b""       # byte 24 kHz dispari in sospeso
        self._down_pcm = bytearray()  # 16 kHz non ancora a frame intero
        self._down = collections.deque()
        self._down_ready = asyncio.Event()
        self._pacer: asyncio.Task | None = None
        # Istante d'invio del prossimo frame downlink; 0 = ripartenza con pre-buffer
        self._due = 0.0
        self._vu_peak = 0.0
        self._vu_last = time.monotonic()
        self._closed = False
        logger.info(f"[{device_id}] Hub live media ready (call {call_id}, AEC {self.aec.backend_name})")

    def start(self):
        if self._closed or self._pacer is not None:
            return  # chiusa prima di partire: niente pacer appeso su _down_ready
        self._pacer = asyncio.create_task(self._pace_downlink())

    # --- uplink -----------------------------------------------------------

    async def uplink(self, pcm16k: np.ndarray):
        """Mic dell'Atom gia' decodificato (16 kHz)."""
        if self._closed:
            return
        clean = self.aec.process(pcm16k.tobytes())
        if clean:
            self.relay.send_audio(clean)
            samples = np.frombuffer(clean, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples)))
            self._vu_peak = max(self._vu_peak, rms)
        now = time.monotonic()
        if now - self._vu_last >= _VU_INTERVAL_S:
            self._vu_last = now
            level = vu_level(self._vu_peak)
            self._vu_peak = 0.0
            await self._send_json({"type": "vu", "mic": level})

    # --- downlink ---------------------------------------------------------

    def on_hub_audio(self, pcm24k: bytes):
        """Voce del modello dal hub (24 kHz s16le) -> frame 16 kHz da 20 ms in coda."""
        if self._closed:
            return
        data = self._down_tail + pcm24k
        if len(data) % 2:
            self._down_tail, data = data[-1:], data[:-1]
        else:
            self._down_tail = b""
        self._down_pcm += self._resampler.process(np.frombuffer(data, dtype=np.int16)).tobytes()
        n = len(self._down_pcm) - len(self._down_pcm) % _FRAME_BYTES
        for i in range(0, n, _FRAME_BYTES):
            self._down.append(bytes(self._down_pcm[i:i + _FRAME_BYTES]))
        del self._down_pcm[:n]
        overflow = len(self._down) - _DOWNLINK_MAX_FRAMES
        for _ in range(max(0, overflow)):
            self._down.popleft()
        if self._down:
            self._down_ready.set()

    async def _pace_downlink(self):
        # _due = istante d'invio del prossimo frame; si tiene il device avanti di
        # _DOWNLINK_PREBUFFER_FRAMES frame, senza aggiungere altro anticipo se il hub
        # riprende a parlare prima che il buffer del device sia finito. clear() lo azzera:
        # il device ha appena buttato il suo buffer, quindi si rimanda il pre-buffer.
        try:
            while True:
                if not self._down:
                    self._down_ready.clear()
                    await self._down_ready.wait()
                    self._due = max(self._due,
                                    time.monotonic() - _DOWNLINK_PREBUFFER_FRAMES * _FRAME_S)
                delay = self._due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if not self._down:  # svuotata da un clear durante l'attesa
                    continue
                frame = self._down.popleft()
                self.aec.render(frame, self._due)  # istante programmato: niente jitter
                await self._send_binary(self._encoder.encode(frame, OPUS_FRAME_SAMPLES))
                self._due += _FRAME_S
                # In ritardo di molto (event loop lento): si riallinea invece di sparare a raffica
                self._due = max(self._due, time.monotonic() - _DOWNLINK_PREBUFFER_FRAMES * _FRAME_S)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.device_id}] Hub downlink pacer error: {e!r}")

    async def clear(self):
        """Barge-in dal hub: via tutto il parlato non ancora mandato all'Atom, subito.

        Il riferimento dell'AEC NON si azzera: l'audio gia' consegnato all'Atom (buffer di
        playback, ring I2S) puo' ancora uscire dallo speaker per qualche centinaio di ms, e senza
        riferimento quell'eco arrivava intero a Gemini, che si interrompeva di nuovo (misurato:
        ~400 ms a -3/-7 dBFS dopo ogni clear, ciclo di auto-interruzioni)."""
        self._down.clear()
        self._down_pcm.clear()
        self._down_tail = b""
        self._due = 0.0
        await self._send_json({"type": "audio_clear"})

    @property
    def queued_frames(self) -> int:
        return len(self._down)

    async def close(self, send_ended: bool):
        if self._closed:
            return
        self._closed = True
        if self._pacer and not self._pacer.done() and self._pacer is not asyncio.current_task():
            self._pacer.cancel()
            try:
                await self._pacer
            except asyncio.CancelledError:
                pass
        if send_ended:
            await self.relay.send_json({"type": "ended", "call_id": self.call_id})
        await self.relay.close()
        try:
            await asyncio.to_thread(self.aec.write_debug, f"{self.call_id}_{self.device_id}")
        except Exception as e:
            logger.warning(f"[{self.device_id}] AEC debug non salvato: {e!r}")
        self.aec.close()
        self._down.clear()
