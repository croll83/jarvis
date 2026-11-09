"""Pre-roll della wake word, archivio delle clip e VAD a energia del comando (design 19, 19.12).

- PcmRing: ultimi N secondi di PCM 16 kHz per device, riempito in IDLE dopo il decode Opus.
  Lo snapshot al rilevamento finisce sul rilevamento e contiene la wake word.
- ClipArchive: un WAV per rilevamento (istante UTC, device, punteggio nel nome), con tetto per
  numero e byte. Serve a riaddestrare il modello con i falsi positivi (hard negative).
- speech_ms: quanto parlato c'e' nel buffer del comando (dalla fine della wake word, o dal
  tap, all'accept del hub): sopra LIVE_COMMAND_MIN_SPEECH_MS l'offer porta command=true.
"""

import logging
import os
import re
import time
import wave
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger("wakeword-server")

RATE = 16000
_FRAME = 320  # 20 ms


class PcmRing:
    """Ring di PCM int16. Se tra due append passa troppo tempo (il device e' uscito da IDLE per
    un turno o una sessione) il contenuto vecchio si butta: il pre-roll non deve mescolare
    l'audio di prima del turno con quello nuovo."""

    STALE_GAP_S = 0.5

    def __init__(self, seconds: float, rate: int = RATE):
        self.capacity = max(_FRAME, int(seconds * rate))
        self._buf = np.zeros(self.capacity, dtype=np.int16)
        self._pos = 0       # prossimo indice da scrivere
        self._filled = 0
        self._last = 0.0

    def clear(self):
        self._pos = 0
        self._filled = 0

    def append(self, pcm: np.ndarray, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        if self._last and now - self._last > self.STALE_GAP_S:
            self.clear()
        self._last = now
        pcm = np.asarray(pcm, dtype=np.int16)
        n = len(pcm)
        if n == 0:
            return
        if n >= self.capacity:
            self._buf[:] = pcm[-self.capacity:]
            self._pos = 0
            self._filled = self.capacity
            return
        end = self._pos + n
        if end <= self.capacity:
            self._buf[self._pos:end] = pcm
        else:
            k = self.capacity - self._pos
            self._buf[self._pos:] = pcm[:k]
            self._buf[:n - k] = pcm[k:]
        self._pos = end % self.capacity
        self._filled = min(self.capacity, self._filled + n)

    def snapshot(self) -> bytes:
        """PCM s16le in ordine cronologico, l'ultimo campione e' il piu' recente."""
        if self._filled < self.capacity:
            # Non ancora pieno: dopo clear() si scrive da 0 senza giri, quindi _pos == _filled
            return self._buf[:self._pos].tobytes()
        return np.concatenate((self._buf[self._pos:], self._buf[:self._pos])).tobytes()

    def __len__(self):
        return self._filled


def write_wav(path: str, pcm: bytes, rate: int = RATE):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


_SAFE = re.compile(r"[^A-Za-z0-9]+")


def clip_name(device_id: str, score: float, when: Optional[float] = None) -> str:
    """<UTC>_<device>_s<score>.wav: l'istante in testa, cosi' l'ordine dei nomi e' quello del
    tempo e la pulizia toglie le piu' vecchie."""
    ts = datetime.fromtimestamp(time.time() if when is None else when, tz=timezone.utc)
    stamp = ts.strftime("%Y%m%dT%H%M%S") + f"{ts.microsecond // 1000:03d}Z"
    device = _SAFE.sub("-", device_id).strip("-") or "unknown"
    return f"{stamp}_{device}_s{score:.3f}.wav"


class ClipArchive:
    """Archivio limitato delle clip di pre-roll. save() fa I/O su disco: va chiamato fuori
    dall'event loop (asyncio.to_thread)."""

    def __init__(self, directory: str, max_files: int, max_bytes: int):
        self.directory = directory
        self.max_files = max_files
        self.max_bytes = max_bytes

    def save(self, device_id: str, score: float, pcm: bytes, when: Optional[float] = None) -> Optional[str]:
        if not self.directory or not pcm:
            return None
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, clip_name(device_id, score, when))
        tmp = path + ".tmp"
        write_wav(tmp, pcm)
        os.replace(tmp, path)
        self.prune()
        return path

    def prune(self):
        entries = []
        with os.scandir(self.directory) as it:
            for e in it:
                if e.is_file() and e.name.endswith(".wav"):
                    try:
                        entries.append((e.name, e.stat().st_size))
                    except OSError:
                        pass
        entries.sort()  # il nome inizia con l'istante UTC
        total = sum(size for _, size in entries)
        drop = 0
        while drop < len(entries) and (
                (self.max_files > 0 and len(entries) - drop > self.max_files)
                or (self.max_bytes > 0 and total > self.max_bytes)):
            name, size = entries[drop]
            try:
                os.remove(os.path.join(self.directory, name))
            except OSError:
                pass
            total -= size
            drop += 1
        if drop:
            logger.info(f"Archivio clip wake: tolte {drop} clip vecchie")


def _frame_db(pcm: np.ndarray) -> np.ndarray:
    n = len(pcm) // _FRAME
    if n == 0:
        return np.zeros(0)
    x = pcm[:n * _FRAME].astype(np.float64).reshape(n, _FRAME)
    rms = np.sqrt(np.mean(x * x, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-3) / 32768.0)


def speech_ms(buffer: bytes, reference: bytes = b"", skip: tuple = (0, 0), *,
              margin_db: float = 10.0, min_dbfs: float = -65.0) -> int:
    """Millisecondi di parlato nel buffer del comando, VAD a energia su frame da 20 ms.

    Il fondo si stima sul 10imo percentile dei frame di reference (il pre-roll, che ha la
    stanza prima della wake word) piu' il buffer; e' parlato un frame sopra fondo + margin_db
    e sopra min_dbfs. skip = intervallo di byte del buffer da non contare (il chirp)."""
    if len(buffer) < 2:
        return 0
    buf = np.frombuffer(buffer[:len(buffer) - len(buffer) % 2], dtype="<i2")
    db = _frame_db(buf)
    if len(db) == 0:
        return 0
    ref = _frame_db(np.frombuffer(reference[:len(reference) - len(reference) % 2], dtype="<i2")) \
        if reference else np.zeros(0)
    floor = float(np.percentile(np.concatenate((ref, db)), 10))
    thr = max(floor + margin_db, min_dbfs)
    voiced = db > thr
    a, b = skip
    if b > a:
        fa, fb = a // (2 * _FRAME), -(-b // (2 * _FRAME))
        voiced[max(0, fa):max(0, fb)] = False
    return int(np.count_nonzero(voiced)) * _FRAME * 1000 // RATE
