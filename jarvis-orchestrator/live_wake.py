"""
JARVIS Live Wake - Fase 3, punto 1 del Voice Live Hub (design-voice-live-hub.md, sez. 19 e 19.12).

Dalla wake word, o dal tap, direttamente alla sessione Gemini Live, senza il turno
"avvia una sessione live". Qui la logica che non tocca le sessioni:
- decodifica del pre-roll (PCM s16le 16 kHz in base64) con tetto di durata;
- voice ID su clip corte: punteggi contro OGNI iscritto, in executor e con tetto di tempo
  (il risultato tardivo si scarta), poi soglia E distacco dal secondo candidato. Il nome
  dichiarato e' solo un indizio: sceglie con chi confrontare, la soglia non scende mai;
- deduplica per casa (due device che sentono la stessa wake: vince il primo);
- circuit breaker dell'hub (hub_unreachable, provider_unavailable): niente live per un
  minuto, poi un tentativo di prova;
- blocco del live per un device dopo un ripiego da wake/tap fallita;
- shadow del pre-roll (solo punteggi nei log, confronto col voice ID sulla frase) e
  cancello vocale sulle wake (spento per default, nel dubbio la wake passa).
Registro delle sessioni, call_id, mute della stanza e ripiego restano in main.py.
"""

import asyncio
import base64
import binascii
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import config

logger = logging.getLogger("JARVIS_LIVE_WAKE")

PCM_RATE = 16000
BYTES_PER_S = PCM_RATE * 2
# Pre-roll e coda di comando insieme: oltre questa durata si tengono gli ultimi secondi.
MAX_START_CLIP_S = 6.0
# Clip del tool di identificazione in sessione (ring di 15 s dell'hub).
MAX_IDENTIFY_CLIP_S = 15.0
MIN_CLIP_S = 0.3


def decode_pcm(obj, max_s: float = MAX_START_CLIP_S) -> Optional[bytes]:
    """PCM s16le 16 kHz da {"codec","rate","b64"} (o dalla sola stringa base64).

    None se manca, se il formato non e' quello atteso o se e' troppo corto. Oltre max_s
    si tengono gli ultimi max_s secondi: il pre-roll FINISCE al rilevamento.
    """
    if not obj:
        return None
    if isinstance(obj, str):
        b64, codec, rate = obj, "pcm_s16le", PCM_RATE
    elif isinstance(obj, dict):
        b64 = obj.get("b64") or ""
        codec = str(obj.get("codec") or "pcm_s16le").lower()
        try:
            rate = int(obj.get("rate") or PCM_RATE)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if codec != "pcm_s16le" or rate != PCM_RATE or not isinstance(b64, str):
        logger.warning(f"Clip audio non valida: codec={codec} rate={rate}")
        return None
    try:
        pcm = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        logger.warning("Clip audio: base64 illeggibile")
        return None
    pcm = pcm[: len(pcm) - (len(pcm) % 2)]
    limit = int(max_s * BYTES_PER_S)
    if len(pcm) > limit:
        pcm = pcm[-limit:]
    if len(pcm) < int(MIN_CLIP_S * BYTES_PER_S):
        return None
    return pcm


def clip_seconds(pcm: Optional[bytes]) -> float:
    return round(len(pcm or b"") / BYTES_PER_S, 2)


# ---------------------------------------------------------------------------
# Voice ID su clip corte
# ---------------------------------------------------------------------------

@dataclass
class VoiceVerdict:
    """Punteggi del voice ID su una clip. status: ok | timeout | no_models | no_audio | error."""
    status: str
    scores: Dict[int, float] = field(default_factory=dict)
    elapsed_ms: int = 0
    clip_s: float = 0.0

    def ranked(self) -> list:
        return sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)

    @property
    def best_uid(self) -> Optional[int]:
        r = self.ranked()
        return r[0][0] if r else None

    @property
    def best(self) -> float:
        r = self.ranked()
        return float(r[0][1]) if r else 0.0

    @property
    def second(self) -> float:
        r = self.ranked()
        return float(r[1][1]) if len(r) > 1 else 0.0

    def summary(self) -> str:
        if self.status != "ok":
            return f"{self.status} ({self.elapsed_ms}ms, {self.clip_s}s)"
        return (f"best={self.best_uid} {self.best:.3f} second={self.second:.3f} "
                f"scores={dict(self.ranked())} ({self.elapsed_ms}ms, {self.clip_s}s)")


def _score_sync(pcm: bytes) -> Optional[Dict[int, float]]:
    """Punteggi resemblyzer contro ogni iscritto (CPU, da eseguire in un thread)."""
    from voice_recognition import voice_recognizer
    return voice_recognizer.score_speakers(pcm, PCM_RATE)


async def score_clip(pcm: Optional[bytes], cap_s: float) -> VoiceVerdict:
    """Voice ID in executor con tetto di tempo: oltre il tetto il risultato si scarta."""
    if not pcm:
        return VoiceVerdict(status="no_audio")
    t0 = time.monotonic()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, _score_sync, pcm)
    try:
        scores = await asyncio.wait_for(fut, timeout=max(0.05, cap_s))
    except asyncio.TimeoutError:
        return VoiceVerdict(status="timeout", elapsed_ms=int((time.monotonic() - t0) * 1000),
                            clip_s=clip_seconds(pcm))
    except Exception as e:
        logger.warning(f"Voice ID sulla clip fallito: {e}")
        return VoiceVerdict(status="error", elapsed_ms=int((time.monotonic() - t0) * 1000),
                            clip_s=clip_seconds(pcm))
    elapsed = int((time.monotonic() - t0) * 1000)
    if not scores:
        return VoiceVerdict(status="no_models", elapsed_ms=elapsed, clip_s=clip_seconds(pcm))
    return VoiceVerdict(status="ok", scores={int(k): float(v) for k, v in scores.items()},
                        elapsed_ms=elapsed, clip_s=clip_seconds(pcm))


def decide(verdict: VoiceVerdict, threshold: float, margin: float,
           claimed_uid: Optional[int] = None) -> Optional[int]:
    """user_id riconosciuto, o None.

    Il migliore deve superare la soglia e staccare il secondo di almeno margin. Con un nome
    dichiarato (claimed_uid) il dichiarato deve essere proprio il primo fra TUTTI gli
    iscritti: la soglia non scende, niente verifica 1:1 piu' permissiva.
    """
    if verdict.status != "ok" or verdict.best_uid is None:
        return None
    if verdict.best < threshold or verdict.best - verdict.second < margin:
        return None
    if claimed_uid is not None and verdict.best_uid != claimed_uid:
        return None
    return verdict.best_uid


# ---------------------------------------------------------------------------
# Deduplica per casa
# ---------------------------------------------------------------------------
# location_id -> (device_id, istante). Il NabuVoice rileva in locale e il cooldown
# multiroom vale solo dentro un wakeword-server: la deduplica fra device la fa l'orchestrator.
_recent_by_home: Dict[str, Tuple[str, float]] = {}


def claim_home(location_id: str, device_id: str, now: Optional[float] = None) -> bool:
    """True se il device e' il primo della casa a chiedere il live nella finestra."""
    now = time.monotonic() if now is None else now
    key = (location_id or "").lower()
    prev = _recent_by_home.get(key)
    if prev and prev[0] != device_id and now - prev[1] < config.LIVE_WAKE_DEDUPE_S:
        return False
    _recent_by_home[key] = (device_id, now)
    return True


# ---------------------------------------------------------------------------
# Circuit breaker dell'hub
# ---------------------------------------------------------------------------
# Scatta solo su hub_unreachable e provider_unavailable (hub o Gemini giu'), mai su "error"
# di un singolo device ne' su "too many calls".
BREAKER_DETAILS = {"provider_unavailable"}
# Un tentativo di prova che non riferisce entro questo tempo libera il posto a un altro.
_TRIAL_TIMEOUT_S = 15.0


class HubBreaker:
    def __init__(self):
        self.open_until = 0.0
        self.trial_at = 0.0
        self.failures = 0

    def reset(self):
        self.open_until = 0.0
        self.trial_at = 0.0
        self.failures = 0

    def is_open(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return now < self.open_until

    def allow(self, now: Optional[float] = None) -> bool:
        """True se si puo' tentare il live. Dopo l'apertura, scaduto il minuto, passa UN
        tentativo di prova; gli altri restano fuori finche' la prova non riferisce."""
        now = time.monotonic() if now is None else now
        if not self.failures:
            return True
        if now < self.open_until:
            return False
        if self.trial_at and now - self.trial_at < _TRIAL_TIMEOUT_S:
            return False
        self.trial_at = now
        return True

    def record_failure(self, reason: str, detail: Optional[str] = None,
                       now: Optional[float] = None) -> bool:
        """Registra l'esito di un'apertura fallita. True se il breaker scatta."""
        trips = reason == "hub_unreachable" or (
            reason == "hub_rejected" and (detail or "").strip().lower() in BREAKER_DETAILS)
        if not trips:
            return False
        now = time.monotonic() if now is None else now
        self.failures += 1
        self.trial_at = 0.0
        self.open_until = now + config.LIVE_HUB_BREAKER_S
        logger.warning(f"🔌 Circuit breaker hub APERTO per {config.LIVE_HUB_BREAKER_S:.0f}s "
                       f"({reason}{'/' + detail if detail else ''}, guasti di fila {self.failures})")
        return True

    def record_success(self) -> None:
        if self.failures:
            logger.info("🔌 Circuit breaker hub chiuso: sessione aperta")
        self.reset()


breaker = HubBreaker()


# ---------------------------------------------------------------------------
# Blocco dopo un ripiego
# ---------------------------------------------------------------------------
# Dentro il turno classico di ripiego da una wake fallita non si riprova il live per quel
# device: "avvia una sessione live" in quel minuto va sulla sessione di oggi.
_fallback_until: Dict[str, float] = {}


def _norm(device_id: str) -> str:
    return (device_id or "").upper().strip()


def block_after_fallback(device_id: str, now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    _fallback_until[_norm(device_id)] = now + config.LIVE_FALLBACK_BLOCK_S


def fallback_blocked(device_id: str, now: Optional[float] = None) -> bool:
    until = _fallback_until.get(_norm(device_id))
    if until is None:
        return False
    now = time.monotonic() if now is None else now
    if now >= until:
        _fallback_until.pop(_norm(device_id), None)
        return False
    return True


# ---------------------------------------------------------------------------
# Shadow del pre-roll e cancello vocale
# ---------------------------------------------------------------------------
# device_id -> ultimo voice ID sul pre-roll, per confrontarlo con quello sulla frase del
# turno classico che segue (19.8, passo 1). Solo punteggi, mai audio.
_shadow: Dict[str, dict] = {}
_SHADOW_TTL_S = 30.0


def remember_shadow(device_id: str, verdict: VoiceVerdict, meta: Optional[dict] = None) -> None:
    _shadow[_norm(device_id)] = {"at": time.monotonic(), "verdict": verdict, "meta": meta or {}}


def compare_with_phrase(device_id: str, speaker_ctx: dict) -> Optional[dict]:
    """Confronta il voice ID sul pre-roll con quello sulla frase. Log e dict per il corpus."""
    rec = _shadow.pop(_norm(device_id), None)
    if not rec or time.monotonic() - rec["at"] > _SHADOW_TTL_S:
        return None
    v: VoiceVerdict = rec["verdict"]
    pre_uid = decide(v, config.LIVE_WAKE_ID_THRESHOLD, config.LIVE_WAKE_ID_MARGIN)
    phrase_uid = speaker_ctx.get("speaker_id") if speaker_ctx.get("speaker_identified") else None
    try:
        phrase_uid = int(phrase_uid) if phrase_uid is not None else None
    except (TypeError, ValueError):
        pass
    out = {
        "preroll_status": v.status, "preroll_best_uid": v.best_uid,
        "preroll_best": round(v.best, 4), "preroll_second": round(v.second, 4),
        "preroll_decision": pre_uid, "phrase_uid": phrase_uid,
        "phrase_confidence": speaker_ctx.get("speaker_confidence"),
        "agree": pre_uid == phrase_uid, **{k: rec["meta"].get(k) for k in ("score", "model")},
    }
    logger.info(f"🎯 Shadow pre-roll {device_id}: pre-roll -> {pre_uid} ({v.summary()}); "
                f"frase -> {phrase_uid} (conf={speaker_ctx.get('speaker_confidence')}); "
                f"{'concordi' if out['agree'] else 'DISCORDI'}")
    return out


def gate_decision(verdict: VoiceVerdict) -> Tuple[bool, str]:
    """(tieni la wake?, motivo). Nel dubbio la wake passa: il cancello scarta solo un
    pre-roll letto bene che non somiglia ad alcun iscritto oltre la soglia."""
    if not config.WAKE_VOICE_GATE:
        return True, "gate_off"
    if verdict.status != "ok":
        return True, verdict.status
    if verdict.best >= config.WAKE_VOICE_GATE_THRESHOLD:
        return True, "match"
    return False, "no_match"
