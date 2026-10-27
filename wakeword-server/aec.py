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

    def __init__(self, backend):
        self._backend = backend
        self._passthrough = isinstance(backend, _PassthroughBackend)
        self._render = bytearray()
        self._near = bytearray()

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
            out += self._backend.process(near, far)
        del self._near[:n]
        return bytes(out)

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
