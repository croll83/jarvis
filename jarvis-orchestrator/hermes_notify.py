"""Notifiche asincrone di Hermes verso i device di casa (timer, sveglie, monitor).

Hermes possiede timer e monitor; l'orchestrator possiede i device. Quando un job
di Hermes scade chiama POST /api/tools/notify_device e l'orchestrator fa uscire
il testo dall'audio giusto del device:

  - speaker integrato (use_internal_speaker=1: Atom con speaker, telefono,
    orologio) -> speak_to_device, Opus sul websocket del device;
  - senza speaker (Atom con Echo, NabuVoice con soundbar) -> TTS sul
    media_player associato (output_speaker) via Home Assistant.

Non si passa da deliver_final_response di proposito: e' la chiusura di un TURNO
vocale (riapre il microfono sulle domande, gestisce lo speaking state, in DND
devia sul bot Telegram di sistema) e non dice se l'audio e' partito. Un avviso
asincrono ha bisogno dell'esito: se l'audio non parte, lo si dice a Hermes
(delivered=false + motivo) e Hermes ripiega sul canale testuale dell'utente
(Telegram per Marco, WhatsApp per Ada). Chi non ha un canale testuale (Giorgio,
sconosciuto) perde la notifica, per scelta.

Qui c'e' solo la parte pura (niente I/O), testabile senza l'orchestrator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional

MAX_TEXT_CHARS = 400          # un avviso vocale, non un discorso
_MAC_RE = re.compile(r"^[0-9A-F]{12}$")
SOUNDS = {"positive", "neutral", "negative"}


class NotifyError(ValueError):
    """Richiesta non servibile: il chiamante risponde con un 4xx, non ritenta."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    device_id: str
    friendly_name: Optional[str]
    location_id: Optional[str]
    use_internal_speaker: bool
    output_speaker: Optional[str]
    device_type: str
    connected: bool

    @property
    def audio_path(self) -> str:
        """Dove andra' l'audio: utile al chiamante e ai log."""
        if self.use_internal_speaker:
            return "internal"
        return "media_player" if self.output_speaker else "none"

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "name": self.friendly_name,
                "location_id": self.location_id, "device_type": self.device_type,
                "audio_path": self.audio_path, "output_speaker": self.output_speaker,
                "connected": self.connected}


def normalize_device_id(raw: Any) -> str:
    """MAC senza separatori, maiuscolo. Accetta anche AA:BB:... e aa-bb-..."""
    if not isinstance(raw, str) or not raw.strip():
        raise NotifyError("device_id mancante")
    dev = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
    if not _MAC_RE.match(dev):
        raise NotifyError(f"device_id non valido: {raw!r}")
    return dev


def clean_text(raw: Any) -> str:
    """Testo parlabile: una riga, limitato, mai vuoto. Il punto interrogativo
    finale diventa un punto: un avviso non e' una domanda."""
    if not isinstance(raw, str) or not raw.strip():
        raise NotifyError("text vuoto")
    text = " ".join(raw.split())
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rsplit(" ", 1)[0] + "…"
    return re.sub(r"[?？]+\s*$", ".", text)


def clean_sound(raw: Any) -> Optional[str]:
    if raw in (None, ""):
        return None
    if raw not in SOUNDS:
        raise NotifyError(f"sound non valido: {raw!r} (ammessi: {sorted(SOUNDS)})")
    return raw


def _target(dev: Any, connected: set) -> Target:
    return Target(
        device_id=dev.device_id,
        friendly_name=dev.friendly_name,
        location_id=getattr(dev, "location_id", None),
        use_internal_speaker=bool(getattr(dev, "use_internal_speaker", False)),
        output_speaker=getattr(dev, "output_speaker", None),
        device_type=getattr(dev, "device_type", None) or "AtomS3R",
        connected=dev.device_id in connected,
    )


def _usable(dev: Any) -> bool:
    return bool(getattr(dev, "friendly_name", None)) and bool(getattr(dev, "enabled", True))


def resolve_target(*, device_id: Any = None, name: Any = None, location_id: Any = None,
                   get_device: Callable[[str], Any], all_devices: Callable[[], Iterable[Any]],
                   connected_ids: Iterable[str]) -> Target:
    """Device per MAC oppure per nome (+ location se il nome e' ambiguo).

    `get_device` = database.get_voice_device, `all_devices` =
    database.get_all_voice_devices, `connected_ids` = websocket aperti ora.
    """
    connected = {str(c).upper().strip() for c in connected_ids}
    if device_id not in (None, ""):
        dev_id = normalize_device_id(device_id)
        dev = get_device(dev_id)
        if dev is None:
            raise NotifyError(f"device {dev_id} sconosciuto", 404)
        if not _usable(dev):
            raise NotifyError(f"device {dev_id} non configurato o disabilitato", 404)
        return _target(dev, connected)

    if not isinstance(name, str) or not name.strip():
        raise NotifyError("serve device_id oppure name")
    wanted = name.strip().lower()
    loc = location_id.strip().lower() if isinstance(location_id, str) and location_id.strip() else None
    matches: List[Any] = [d for d in all_devices() if _usable(d)
                          and d.friendly_name.strip().lower() == wanted
                          and (loc is None or (d.location_id or "").lower() == loc)]
    if not matches:
        raise NotifyError(f"nessun device chiamato {name!r}" + (f" in {location_id}" if loc else ""), 404)
    if len(matches) > 1:
        where = ", ".join(sorted(f"{d.location_id}" for d in matches))
        raise NotifyError(f"{name!r} e' ambiguo ({where}): specifica location_id", 409)
    return _target(matches[0], connected)


def precheck(target: Target) -> Optional[str]:
    """Motivo per cui l'audio sicuramente non partira', o None se vale la pena
    provare. Evita di mettere in coda un TTS che finirebbe nel vuoto."""
    if target.use_internal_speaker and not target.connected:
        return "device non connesso"
    if not target.use_internal_speaker and not target.output_speaker:
        return "device senza uscita audio configurata"
    return None


def in_silent_hours(hour: int, start: int, end: int) -> bool:
    """Stessa regola di deliver_final_response (intervallo che scavalca la mezzanotte)."""
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def quiet_reason(*, dnd: bool, silent_hours: bool, urgent: bool) -> Optional[str]:
    """Un timer o una sveglia chiesti dall'utente (urgent) parlano sempre; un
    avviso di monitor rispetta non disturbare e ore silenziose, e in quel caso
    Hermes ripiega sul testo."""
    if urgent:
        return None
    if dnd:
        return "non disturbare attivo"
    if silent_hours:
        return "ore silenziose"
    return None
