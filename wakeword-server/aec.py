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
# Riferimento allineato (firmware con loopback ES8311, cap "aec_ref"): mic e riferimento escono
# dallo stesso frame del codec, quindi tra riferimento ed eco restano solo pochi ms costanti
# (filtri del codec, amplificatore, aria) e il suggerimento all'AEC3 scende a ~0.
_ALIGNED_DELAY_HINT_MS = int(os.getenv("AEC_ALIGNED_DELAY_MS", "0") or "0")
# Cancello d'eco (solo col riferimento dal device). Sull'Atom lo speaker arriva al mic ~30 dB sopra
# la voce dell'utente e in modo poco lineare (coerenza ~0.8): anche con 30 dB tolti dall'AEC3 i
# picchi del residuo (p99 ~ -20 dB sotto il riferimento) superano la voce a un metro (~ -31 dB),
# e Gemini si interrompeva da solo. Finche' il riferimento e' attivo, piu' una coda, a Gemini va
# silenzio; passa solo cio' che supera il livello del riferimento + AEC_GATE_BARGE_DB.
_GATE = os.getenv("AEC_GATE", "1").strip().lower() not in ("0", "false", "no", "off")
_GATE_HOLD_MS = int(os.getenv("AEC_GATE_HOLD_MS", "200") or "0")
_GATE_BARGE_DB = float(os.getenv("AEC_GATE_BARGE_DB", "-10") or "-10")
_GATE_OPEN_HOLD_MS = 300      # un barge-in riconosciuto tiene aperto il cancello per questo tempo
_GATE_FAR_ACTIVE_DB = -50.0   # riferimento sopra questo livello = Jarvis sta suonando


def _dbfs(sq_sum: float, n: int) -> float:
    if n == 0 or sq_sum <= 0:
        return -120.0
    return 10 * math.log10(sq_sum / n / (32768.0 ** 2))


def _scale(pcm: bytes, gain: float) -> bytes:
    vals = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return struct.pack(f"<{len(vals)}h", *(max(-32768, min(32767, int(v * gain))) for v in vals))


def _sq_sum(pcm: bytes) -> float:
    vals = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return float(sum(v * v for v in vals))


class _WebRtcBackend:
    name = "webrtc"

    def __init__(self, agc: bool = True):
        from livekit import rtc
        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(
            echo_cancellation=True, noise_suppression=True,
            high_pass_filter=True, auto_gain_control=agc)
        self.delay_ms = _WEBRTC_DELAY_HINT_MS

    def process(self, near: bytes, far: bytes) -> bytes:
        rtc = self._rtc
        far_frame = rtc.AudioFrame(bytearray(far), SAMPLE_RATE, 1, CHUNK_SAMPLES)
        self._apm.process_reverse_stream(far_frame)
        near_frame = rtc.AudioFrame(bytearray(near), SAMPLE_RATE, 1, CHUNK_SAMPLES)
        self._apm.set_stream_delay_ms(self.delay_ms)
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


# Allineamento e stima del ritardo (vedi EchoCanceller)
_RESYNC_GAP_S = 0.5           # mic fermo oltre questo: si riancora la sua timeline
# Il mic legge il riferimento con questo margine: frame scritto all'invio e mic letto all'arrivo
# avvengono quasi nello stesso istante, e senza margine il mic leggeva la zona non ancora scritta
# (in produzione il riferimento arrivava all'AEC sempre vuoto). Il ritardo eco reale misurato a
# inizio sessione e' ~80-140 ms: resta positivo anche togliendo il margine.
_READ_LAG_MS = 40
_AUTO_DELAY = os.getenv("AEC_AUTO_DELAY", "1").strip().lower() not in ("0", "false", "no", "off")
_AUTO_MAX_AEC_MS = 400        # fin qui l'AEC3 ce la fa da solo: nessuna linea
_AUTO_TARGET_MS = 150         # con la linea, ritardo residuo lasciato all'AEC3
_AUTO_MAX_LINE_MS = 900
_EST_WINDOW = 400             # 4 s di inviluppi da 10 ms
_EST_LAGS = 100               # ritardi cercati: 0..990 ms
_EST_EVERY = 200              # una stima ogni 2 s
_EST_MIN_CORR = 0.5
_EST_FAR_ACTIVE = 2.5         # log10 rms: riferimento "attivo" sopra ~-30 dBFS
_RING_KEEP_S = 3


class _EchoGate:
    """Silenzio verso Gemini mentre il riferimento e' attivo (e per _GATE_HOLD_MS dopo), salvo
    blocchi puliti piu' forti del riferimento + barge_db (una voce molto vicina o forte)."""

    def __init__(self, hold_ms: int = _GATE_HOLD_MS, barge_db: float = _GATE_BARGE_DB):
        self._hold = max(1, hold_ms // 10)
        self._barge_db = barge_db
        self._far_db = []             # livello del riferimento degli ultimi blocchi da 10 ms
        self._open = 0                # blocchi di apertura residui dopo un barge-in
        self.gated = 0
        self.passed = 0

    def apply(self, far: bytes, clean: bytes) -> bytes:
        self._far_db.append(_dbfs(_sq_sum(far), CHUNK_SAMPLES))
        if len(self._far_db) > self._hold:
            del self._far_db[0]
        env = max(self._far_db)
        if env < _GATE_FAR_ACTIVE_DB:
            self._open = 0
            return clean
        if _dbfs(_sq_sum(clean), CHUNK_SAMPLES) > env + self._barge_db:
            self._open = _GATE_OPEN_HOLD_MS // 10
        if self._open > 0:
            self._open -= 1
            self.passed += 1
            return clean
        self.gated += 1
        return _SILENCE


class EchoCanceller:
    """AEC di una sessione: render() col PCM mandato all'Atom, process() col mic.

    Allineamento per TEMPO, non per ordine d'arrivo: ogni frame di riferimento va sulla
    timeline all'istante in cui il pacer l'ha programmato, il mic per conteggio di campioni dal
    suo arrivo (il jitter del WiFi non sposta nulla). Prima riferimento e mic erano accoppiati in
    ordine d'arrivo, con silenzio infilato quando il mic arrivava prima: il ritardo eco visto
    dall'AEC saltava da 70 a 670 ms dentro la stessa sessione e l'AEC3 non si agganciava.

    Con l'allineamento per tempo il ritardo e' stabile; la stima automatica (correlazione degli
    inviluppi, ogni 2 s) mette una linea di ritardo sul riferimento se supera quello che l'AEC3
    gestisce da solo (~500 ms). AEC_RENDER_DELAY_MS e' la linea iniziale (la stima parte da
    li'); AEC_AUTO_DELAY=0 la tiene fissa.
    """

    def __init__(self, backend, render_delay_ms: int = _RENDER_DELAY_MS, auto_delay: bool = _AUTO_DELAY):
        self._backend = backend
        self._passthrough = isinstance(backend, _PassthroughBackend)
        self._t0 = None
        self._ring = bytearray()      # timeline del riferimento a partire da _ring_start (campioni)
        self._ring_start = 0
        self._cap = None              # indice (campioni) del prossimo campione di mic
        self._last_cap_t = 0.0
        self._near = bytearray()
        self._line = max(0, render_delay_ms) * SAMPLE_RATE // 1000
        self._auto = auto_delay  # la linea iniziale e' solo il punto di partenza
        self._env_far = []            # inviluppi da 10 ms: riferimento senza linea, allineato al mic
        self._env_near = []
        self._chunks = 0
        self._proposal = None
        self._aligned = False         # process_aligned(): riferimento dal device, niente timeline
        self._far = bytearray()
        self._gate = _EchoGate() if _GATE else None
        self._gain = 1.0              # guadagno fisso dopo l'AEC (process_aligned)
        self._debug = bool(_DEBUG_DIR)
        if self._debug:
            self._dbg = {"near": bytearray(), "far": bytearray(), "out": bytearray()}
            self._stats = {"near": 0.0, "far": 0.0, "out": 0.0, "n": 0, "clip": 0}
            self._stats_t = time.monotonic()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def render_delay_ms(self) -> int:
        return self._line * 1000 // SAMPLE_RATE

    @property
    def aligned(self) -> bool:
        return self._aligned

    def set_aligned_gain_db(self, db: float):
        """Guadagno fisso dopo l'AEC, al posto dell'AGC: restituisce quello tolto al mic."""
        self._gain = 10 ** (db / 20)

    @property
    def gate_stats(self):
        return (self._gate.gated, self._gate.passed) if self._gate else (0, 0)

    @property
    def render_backlog_bytes(self) -> int:
        """Riferimento in timeline non ancora letto dal mic."""
        if self._aligned:
            return 0
        end = self._ring_start + len(self._ring) // 2
        lag = _READ_LAG_MS * SAMPLE_RATE // 1000
        start = self._ring_start if self._cap is None else max(self._ring_start, self._cap - lag - self._line)
        return max(0, end - start) * 2

    def _index(self, t: float) -> int:
        return int(round((t - self._t0) * SAMPLE_RATE))

    def render(self, pcm: bytes, t: float = None):
        """PCM 16 kHz appena mandato al device (t = istante d'invio, default adesso).

        Una frase si ancora all'istante d'invio del suo primo frame e i frame seguenti si accodano
        di seguito, come l'Atom li suona: il pre-buffer mandato a raffica resta in avanti sulla
        timeline invece di finire nel passato (dove il mic l'aveva gia' superato)."""
        if self._passthrough or self._aligned or not pcm:
            return
        t = time.monotonic() if t is None else t
        if self._t0 is None:
            self._t0 = t
        now_idx = self._index(t)
        end = self._ring_start + len(self._ring) // 2
        if not self._ring:
            self._ring_start = now_idx
        elif end < now_idx:
            self._ring += bytes((now_idx - end) * 2)  # silenzio tra le frasi: nuova ancora
        self._ring += pcm
        excess = len(self._ring) - _RING_KEEP_S * SAMPLE_RATE * 2
        if excess > 0:
            excess -= excess % 2
            del self._ring[:excess]
            self._ring_start += excess // 2

    def reset_render(self):
        """Svuota il riferimento (non usato al barge-in: l'audio gia' consegnato suona ancora)."""
        self._ring.clear()

    def _read(self, idx: int) -> bytes:
        off = idx - self._ring_start
        a, b = max(0, off), max(0, off + CHUNK_SAMPLES)
        have = len(self._ring) // 2
        if b <= 0 or a >= have:
            return _SILENCE
        chunk = bytes(self._ring[a * 2:min(b, have) * 2])
        return bytes((a - off) * 2) + chunk + bytes((off + CHUNK_SAMPLES - min(b, have)) * 2)

    def process(self, pcm: bytes, now: float = None) -> bytes:
        """Mic 16 kHz -> mic ripulito; lavora a blocchi da 10 ms, il resto resta in coda."""
        now = time.monotonic() if now is None else now
        if self._t0 is None:
            self._t0 = now
        if self._cap is None or now - self._last_cap_t > _RESYNC_GAP_S:
            # (ri)ancoraggio: questi campioni sono appena arrivati
            self._cap = self._index(now) - (len(self._near) + len(pcm)) // 2
        self._last_cap_t = now
        self._near += pcm
        n = len(self._near) - len(self._near) % _CHUNK_BYTES
        if n == 0:
            return b""
        out = bytearray()
        for i in range(0, n, _CHUNK_BYTES):
            near = bytes(self._near[i:i + _CHUNK_BYTES])
            lag = _READ_LAG_MS * SAMPLE_RATE // 1000
            far = self._read(self._cap - lag - self._line)
            if self._auto:
                self._track(self._read(self._cap - lag), near)
            self._cap += CHUNK_SAMPLES
            clean = self._backend.process(near, far)
            out += clean
            if self._debug:
                self._debug_chunk(near, far, clean)
        del self._near[:n]
        return bytes(out)

    def process_aligned(self, near: bytes, far: bytes) -> bytes:
        """Mic e riferimento letti dallo stesso frame del codec (loopback ES8311), stessa
        lunghezza: vanno all'AEC cosi' come sono, a blocchi da 10 ms, il resto resta in coda.
        Dal primo pacchetto allineato render() non serve piu' e la timeline viene ignorata."""
        if not self._aligned:
            self._aligned = True
            self._near.clear()
            self._ring.clear()
            if hasattr(self._backend, "delay_ms"):
                self._backend.delay_ms = _ALIGNED_DELAY_HINT_MS
        self._near += near
        self._far += far
        n = min(len(self._near), len(self._far))
        n -= n % _CHUNK_BYTES
        if n == 0:
            return b""
        out = bytearray()
        for i in range(0, n, _CHUNK_BYTES):
            near_c = bytes(self._near[i:i + _CHUNK_BYTES])
            far_c = bytes(self._far[i:i + _CHUNK_BYTES])
            clean = self._backend.process(near_c, far_c)
            if self._gate is not None:  # confronto col riferimento prima del guadagno
                clean = self._gate.apply(far_c, clean)
            if self._gain != 1.0:
                clean = _scale(clean, self._gain)
            out += clean
            if self._debug:
                self._debug_chunk(near_c, far_c, clean)
        del self._near[:n]
        del self._far[:n]
        return bytes(out)

    def _track(self, far0: bytes, near: bytes):
        """Stima il ritardo eco (riferimento senza linea -> mic) e, se serve, sposta la linea."""
        self._env_far.append(math.log10(math.sqrt(_sq_sum(far0) / CHUNK_SAMPLES) + 1))
        self._env_near.append(math.log10(math.sqrt(_sq_sum(near) / CHUNK_SAMPLES) + 1))
        keep = _EST_WINDOW + _EST_LAGS
        if len(self._env_far) > keep:
            del self._env_far[:-keep]
            del self._env_near[:-keep]
        self._chunks += 1
        if self._chunks % _EST_EVERY or len(self._env_far) < keep:
            return
        import numpy as np
        ef = np.array(self._env_far[:_EST_WINDOW])
        en = np.array(self._env_near)
        active = ef > _EST_FAR_ACTIVE
        if active.mean() < 0.3:
            return
        best_c, best_lag = -1.0, 0
        for lag in range(_EST_LAGS):
            b = en[lag:lag + _EST_WINDOW]
            a, bb = ef[active], b[active]
            if a.std() == 0 or bb.std() == 0:
                continue
            c = float(np.corrcoef(a, bb)[0, 1])
            if c > best_c:
                best_c, best_lag = c, lag
        if best_c < _EST_MIN_CORR:
            return
        delay_ms = best_lag * 10
        want = 0 if delay_ms <= _AUTO_MAX_AEC_MS else min(_AUTO_MAX_LINE_MS, delay_ms - _AUTO_TARGET_MS)
        if self._proposal is not None and abs(want - self._proposal) <= 40:
            if abs(want - self.render_delay_ms) > 60:
                logger.info(f"AEC: ritardo eco {delay_ms} ms (corr {best_c:.2f}), linea di ritardo "
                            f"{self.render_delay_ms} -> {want} ms")
                self._line = want * SAMPLE_RATE // 1000
        self._proposal = want

    def _debug_chunk(self, near: bytes, far: bytes, clean: bytes):
        for key, pcm in (("near", near), ("far", far), ("out", clean)):
            if len(self._dbg[key]) < _DEBUG_MAX_BYTES:
                self._dbg[key] += pcm
            self._stats[key] += _sq_sum(pcm)
        self._stats["n"] += CHUNK_SAMPLES
        self._stats["clip"] += sum(1 for v in struct.unpack(f"<{CHUNK_SAMPLES}h", near)
                                   if v >= 32767 or v <= -32768)
        now = time.monotonic()
        if now - self._stats_t >= _DEBUG_STATS_S:
            n = self._stats["n"]
            far_db, near_db, out_db = (_dbfs(self._stats[k], n) for k in ("far", "near", "out"))
            clip = 100.0 * self._stats["clip"] / n
            g = self._gate
            where = ((f"riferimento dal device, cancello {g.gated} chiusi / {g.passed} barge-in" if g
                      else "riferimento dal device") if self._aligned else
                     f"riferimento in coda {self.render_backlog_bytes // 32} ms, linea {self.render_delay_ms} ms")
            logger.info(f"AEC stats: far {far_db:.1f} dBFS, mic {near_db:.1f} (clip {clip:.1f}%), "
                        f"pulito {out_db:.1f} (tolti {near_db - out_db:.1f} dB), {where}")
            self._stats = {"near": 0.0, "far": 0.0, "out": 0.0, "n": 0, "clip": 0}
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


def create_echo_canceller(preferred: str, agc: bool = True) -> EchoCanceller:
    """Primo backend disponibile secondo AEC_BACKEND, altrimenti passthrough. agc=False per il
    riferimento dal device: l'AGC rialzava il residuo d'eco insieme alla voce (mic abbassato)."""
    order = _PREFERENCE.get(preferred)
    if order is None:
        logger.warning(f"AEC_BACKEND={preferred!r} sconosciuto, uso webrtc")
        order = _PREFERENCE["webrtc"]
    for name in order:
        try:
            return EchoCanceller(_BACKENDS[name](agc=agc) if name == "webrtc" else _BACKENDS[name]())
        except Exception as e:
            logger.warning(f"AEC backend {name} non disponibile: {e!r}")
    if preferred != "off":
        logger.warning("Nessun AEC disponibile: mic inoltrato senza cancellazione d'eco")
    return EchoCanceller(_PassthroughBackend())
