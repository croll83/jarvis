"""
Registra l'audio dei turni vocali, per costruire un banco di prova realistico.

Ogni turno arrivato dal WebSocket diventa un WAV (PCM 16 kHz mono, com'e'
arrivato: prima di normalizzazione e denoise) piu' un JSON con quello che
l'orchestrator ne ha capito: esito e testo della STT, segmenti della
diarizzazione, chi e' stato riconosciuto. Serve a rispondere su dati veri a
domande come "quanto spesso nei comandi parla piu' di una persona".

La scrittura avviene a turno CHIUSO e in un thread: la risposta al device e'
gia' partita, la latenza non cambia. Spento per default (VOICE_CORPUS_ENABLED):
sono registrazioni della casa, restano sul volume dati (/app/data, ignorato da
git) e non escono da li'.
"""
import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger("JARVIS_VOICE_CORPUS")

# Il turno in corso. Una ContextVar e non un dizionario per device: i task
# creati durante il turno (STT e speaker ID girano in parallelo) ereditano il
# contesto, quindi annotano lo stesso record senza passarlo di mano in mano.
_current: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("voice_corpus_turn", default=None)
_last_cleanup = 0.0
_CLEANUP_EVERY_S = 3600


def start_turn(device_id: str, audio_bytes: bytes) -> Optional[contextvars.Token]:
    if not config.VOICE_CORPUS_ENABLED:
        return None
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "device_id": device_id,
        "duration_s": round(len(audio_bytes) / 32000, 2),  # 16 kHz, 16 bit, mono
        "_audio": audio_bytes,
        "_t0": time.monotonic(),
    }
    return _current.set(rec)


def note(**fields) -> None:
    """Annota il turno in corso. Fuori da un turno registrato non fa niente."""
    rec = _current.get()
    if rec is not None:
        rec.update(fields)


def note_stt(result, latency_ms: float) -> None:
    rec = _current.get()
    if rec is None:
        return
    segs = list(result.segments or [])
    rec["stt"] = {
        "status": result.status,
        "text": result.text,
        "detail": result.detail,
        "latency_ms": round(latency_ms),
        "diarization": segs,
        "speakers": len({s.get("speaker") for s in segs if isinstance(s, dict)}) if segs else None,
    }


async def finish_turn(token: Optional[contextvars.Token]) -> None:
    if token is None:
        return
    rec = _current.get()
    _current.reset(token)
    if rec is None:
        return
    rec["turn_ms"] = round((time.monotonic() - rec.pop("_t0")) * 1000)
    audio = rec.pop("_audio")
    try:
        await asyncio.to_thread(_write, rec, audio)
    except Exception as e:  # la registrazione non deve MAI disturbare il turno
        logger.warning(f"Voice corpus: turno {rec.get('id')} non salvato: {e}")


def _write(rec: dict, audio: bytes) -> None:
    global _last_cleanup
    day = rec["ts"][:10]
    folder = Path(config.VOICE_CORPUS_DIR) / day
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{rec['ts'][11:19].replace(':', '')}_{rec['device_id']}_{rec['id']}"
    with wave.open(str(folder / f"{stem}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio)
    rec.setdefault("label", None)  # da compilare a mano quando si costruisce il banco
    (folder / f"{stem}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str))
    if time.time() - _last_cleanup > _CLEANUP_EVERY_S:
        _last_cleanup = time.time()
        _enforce_size_cap()


def _enforce_size_cap() -> None:
    """Tetto sul disco: oltre VOICE_CORPUS_MAX_MB si cancellano i turni piu' vecchi."""
    root = Path(config.VOICE_CORPUS_DIR)
    files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    cap = config.VOICE_CORPUS_MAX_MB * 1024 * 1024
    removed = 0
    for p in files:
        if total <= cap:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info(f"Voice corpus oltre {config.VOICE_CORPUS_MAX_MB} MB: rimossi {removed} file piu' vecchi")
