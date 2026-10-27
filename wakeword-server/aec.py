"""Cancellazione d'eco per l'Atom nelle sessioni live full-duplex (Voice Live Hub).

Esiste solo dentro una sessione hub: fuori non si importa nessun backend.
near-end = mic dell'Atom decodificato (16 kHz), far-end/render = lo stesso PCM 16 kHz che
viene codificato e mandato all'Atom. Il render passa da una coda che il capture consuma
10 ms alla volta (zeri se vuota): render e capture restano intercalati 1:1 in tempo reale e
il ritardo acustico (buffer di playback dell'Atom + rete) lo stima il filtro adattivo.

Backend, in ordine di preferenza:
- "webrtc": WebRTC APM (AEC3 + HPF + NS + AGC) dal pacchetto pip livekit (wheel manylinux,
  nessuna build nativa); i binding pip "webrtc-audio-processing" non compilano su 3.12.
- "speex": libspeexdsp di sistema via ctypes (echo canceller MDF + preprocess con
  soppressione dell'eco residua e AGC).
- passthrough con warning se nessuno dei due e' disponibile (o AEC_BACKEND=off).
"""

import ctypes
import ctypes.util
import logging
import math
import os
import struct
import time
import wave

logger = logging.getLogger("wakeword-server")

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 160  # 10 ms, richiesto da entrambi i backend
_CHUNK_BYTES = CHUNK_SAMPLES * 2
_SILENCE = bytes(_CHUNK_BYTES)
# Render in coda oltre 1 s = il mic dell'Atom non arriva piu': si scarta il piu' vecchio
_RENDER_MAX_BYTES = SAMPLE_RATE * 2
# Coda del filtro speex: copre playback dell'Atom + rete andata/ritorno
_SPEEX_TAIL_SAMPLES = SAMPLE_RATE // 2  # 500 ms
# Suggerimento di ritardo per l'APM (AEC3 lo stima comunque da se')
_WEBRTC_DELAY_HINT_MS = 150
# Debug (spento di default): con AEC_DEBUG_DIR ogni sessione salva mic grezzo, riferimento e
# mic pulito cosi' come li vede l'AEC (stessa timeline), per misurare ritardo d'eco e
# cancellazione e riprovare offline le varianti; e logga i livelli ogni 2 s.
_DEBUG_DIR = os.getenv("AEC_DEBUG_DIR", "").strip()
_DEBUG_MAX_BYTES = SAMPLE_RATE * 2 * 300  # 5 minuti per traccia
_DEBUG_STATS_S = 2.0
# Linea di ritardo sul riferimento. L'AEC3 stima da solo il ritardo eco solo fin verso i
# 400-500 ms (misurato: 400 ms -> 65 dB tolti, 600 ms -> nulla), mentre sull'Atom tra anticipo
# del pacer, coda di playback, ring I2S e WiFi l'eco rientra piu' tardi. Ritardando il
# riferimento di una quota fissa (sotto il minimo reale, mai sopra: altrimenti arriva dopo
# l'eco) all'AEC resta solo un residuo piccolo. 0 = nessuna linea.
_RENDER_DELAY_MS = int(os.getenv("AEC_RENDER_DELAY_MS", "0") or "0")


def _dbfs(sq_sum: float, n: int) -> float:
    if n == 0 or sq_sum <= 0:
        return -120.0
    return 10 * math.log10(sq_sum / n / (32768.0 ** 2))


def _sq_sum(pcm: bytes) -> float:
    vals = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return float(sum(v * v for v in vals))


class _WebRtcBackend:
    name = "webrtc"

    def __init__(self):
        from livekit import rtc
        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(
            echo_cancellation=True, noise_suppression=True,
            high_pass_filter=True, auto_gain_control=True)

    def process(self, near: bytes, far: bytes) -> bytes:
        rtc = self._rtc
        far_frame = rtc.AudioFrame(bytearray(far), SAMPLE_RATE, 1, CHUNK_SAMPLES)
        self._apm.process_reverse_stream(far_frame)
        near_frame = rtc.AudioFrame(bytearray(near), SAMPLE_RATE, 1, CHUNK_SAMPLES)
        self._apm.set_stream_delay_ms(_WEBRTC_DELAY_HINT_MS)
        self._apm.process_stream(near_frame)
        return near_frame.data.tobytes()

    def close(self):
        self._apm = None


# Costanti da speex/speex_echo.h e speex/speex_preprocess.h
_SPEEX_ECHO_SET_SAMPLING_RATE = 24
_SPEEX_PREPROCESS_SET_AGC = 2
_SPEEX_PREPROCESS_SET_ECHO_STATE = 24


def _load_speexdsp():
    name = ctypes.util.find_library("speexdsp") or "libspeexdsp.so.1"
    lib = ctypes.CDLL(name)
    lib.speex_echo_state_init.restype = ctypes.c_void_p
    lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
    lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                            ctypes.c_char_p, ctypes.c_char_p]
    lib.speex_preprocess_state_init.restype = ctypes.c_void_p
    lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]
    lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    return lib


class _SpeexBackend:
    name = "speex"

    def __init__(self):
        lib = _load_speexdsp()
        self._lib = lib
        self._echo = lib.speex_echo_state_init(CHUNK_SAMPLES, _SPEEX_TAIL_SAMPLES)
        rate = ctypes.c_int(SAMPLE_RATE)
        lib.speex_echo_ctl(self._echo, _SPEEX_ECHO_SET_SAMPLING_RATE, ctypes.byref(rate))
        self._pre = lib.speex_preprocess_state_init(CHUNK_SAMPLES, SAMPLE_RATE)
        lib.speex_preprocess_ctl(self._pre, _SPEEX_PREPROCESS_SET_ECHO_STATE,
                                 ctypes.c_void_p(self._echo))
        agc = ctypes.c_int(1)
        lib.speex_preprocess_ctl(self._pre, _SPEEX_PREPROCESS_SET_AGC, ctypes.byref(agc))
        self._out = ctypes.create_string_buffer(_CHUNK_BYTES)

    def process(self, near: bytes, far: bytes) -> bytes:
        self._lib.speex_echo_cancellation(self._echo, near, far, self._out)
        self._lib.speex_preprocess_run(self._pre, self._out)
        return self._out.raw

    def close(self):
        if self._pre:
            self._lib.speex_preprocess_state_destroy(self._pre)
            self._pre = None
        if self._echo:
            self._lib.speex_echo_state_destroy(self._echo)
            self._echo = None


class _PassthroughBackend:
    name = "off"

    def process(self, near: bytes, far: bytes) -> bytes:
        return near

    def close(self):
        pass


_BACKENDS = {"webrtc": _WebRtcBackend, "speex": _SpeexBackend}
_PREFERENCE = {"webrtc": ("webrtc", "speex"), "speex": ("speex",), "off": ()}


class EchoCanceller:
    """AEC di una sessione: render() col PCM mandato all'Atom, process() col mic."""

    def __init__(self, backend, render_delay_ms: int = _RENDER_DELAY_MS):
        self._backend = backend
        self._passthrough = isinstance(backend, _PassthroughBackend)
        self._render = bytearray()
        self._near = bytearray()
        # FIFO a lunghezza costante: avanza di 10 ms per ogni 10 ms di mic, anche nei silenzi
        self._delay_bytes = max(0, render_delay_ms) * SAMPLE_RATE // 1000 * 2
        self._delay_bytes -= self._delay_bytes % 2
        self._delay = bytearray(self._delay_bytes)
        self._debug = bool(_DEBUG_DIR)
        if self._debug:
            self._dbg = {"near": bytearray(), "far": bytearray(), "out": bytearray()}
            self._stats = {"near": 0.0, "far": 0.0, "out": 0.0, "n": 0}
            self._stats_t = time.monotonic()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def render_backlog_bytes(self) -> int:
        return len(self._render)

    def render(self, pcm: bytes):
        """PCM 16 kHz appena mandato al device (riferimento dell'eco)."""
        if self._passthrough:
            return
        self._render += pcm
        excess = len(self._render) - _RENDER_MAX_BYTES
        if excess > 0:
            del self._render[:excess]

    def reset_render(self):
        """Barge-in: il device butta l'audio non ancora suonato, quindi anche il riferimento."""
        self._render.clear()
        self._delay = bytearray(self._delay_bytes)

    def process(self, pcm: bytes) -> bytes:
        """Mic 16 kHz -> mic ripulito; lavora a blocchi da 10 ms, il resto resta in coda."""
        self._near += pcm
        n = len(self._near) - len(self._near) % _CHUNK_BYTES
        if n == 0:
            return b""
        out = bytearray()
        for i in range(0, n, _CHUNK_BYTES):
            near = bytes(self._near[i:i + _CHUNK_BYTES])
            if len(self._render) >= _CHUNK_BYTES:
                far = bytes(self._render[:_CHUNK_BYTES])
                del self._render[:_CHUNK_BYTES]
            else:
                far = bytes(self._render) + _SILENCE[len(self._render):]
                self._render.clear()
            if self._delay_bytes:
                self._delay += far
                far = bytes(self._delay[:_CHUNK_BYTES])
                del self._delay[:_CHUNK_BYTES]
            clean = self._backend.process(near, far)
            out += clean
            if self._debug:
                self._debug_chunk(near, far, clean)
        del self._near[:n]
        return bytes(out)

    def _debug_chunk(self, near: bytes, far: bytes, clean: bytes):
        for key, pcm in (("near", near), ("far", far), ("out", clean)):
            if len(self._dbg[key]) < _DEBUG_MAX_BYTES:
                self._dbg[key] += pcm
            self._stats[key] += _sq_sum(pcm)
        self._stats["n"] += CHUNK_SAMPLES
        now = time.monotonic()
        if now - self._stats_t >= _DEBUG_STATS_S:
            n = self._stats["n"]
            far_db, near_db, out_db = (_dbfs(self._stats[k], n) for k in ("far", "near", "out"))
            logger.info(f"AEC stats: far {far_db:.1f} dBFS, mic {near_db:.1f}, pulito {out_db:.1f} "
                        f"(tolti {near_db - out_db:.1f} dB), render in coda {len(self._render) // 32} ms")
            self._stats = {"near": 0.0, "far": 0.0, "out": 0.0, "n": 0}
            self._stats_t = now

    def write_debug(self, name: str):
        """Con AEC_DEBUG_DIR: salva <name>_{near,far,out}.wav (16 kHz mono)."""
        if not self._debug:
            return
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        for key, pcm in self._dbg.items():
            path = os.path.join(_DEBUG_DIR, f"{name}_{key}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(bytes(pcm))
        logger.info(f"AEC debug salvato: {_DEBUG_DIR}/{name}_*.wav")

    def close(self):
        self._backend.close()


def create_echo_canceller(preferred: str) -> EchoCanceller:
    """Primo backend disponibile secondo AEC_BACKEND, altrimenti passthrough."""
    order = _PREFERENCE.get(preferred)
    if order is None:
        logger.warning(f"AEC_BACKEND={preferred!r} sconosciuto, uso webrtc")
        order = _PREFERENCE["webrtc"]
    for name in order:
        try:
            return EchoCanceller(_BACKENDS[name]())
        except Exception as e:
            logger.warning(f"AEC backend {name} non disponibile: {e!r}")
    if preferred != "off":
        logger.warning("Nessun AEC disponibile: mic inoltrato senza cancellazione d'eco")
    return EchoCanceller(_PassthroughBackend())
