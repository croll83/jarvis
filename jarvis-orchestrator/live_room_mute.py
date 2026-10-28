"""
JARVIS Live Room Mute
- Durante una sessione live sull'hub (Gemini full-duplex) la voce esce dal device stesso:
  ogni media_player della sua stanza (Echo, soundbar, TV) va zittito per tutta la sessione,
  altrimenti musica e annunci finiscono nel microfono e Gemini li prende per parlato.
- mute_room(device_id) all'apertura, unmute_room(device_id) a ogni chiusura.
- Per ogni media_player si salva lo stato ESATTO di prima e si ripristina solo quello
  che si e' cambiato: volume_mute dove l'entita' lo supporta (supported_features & 8),
  altrimenti volume a 0 e poi il volume di prima. Quello gia' mutato resta com'e' e non
  viene smutato alla fine. Le entita' non disponibili si saltano.
- Un media_player condiviso da due sessioni (due device nella stessa stanza) si smuta
  solo quando finisce l'ultima.
- Gli errori HA non fanno mai fallire la sessione: si logga e si va avanti.
- Interazione con speaker_suppress (wake word -> 10%, restore dopo lo STT, safety 30 s):
  gli speaker che il suppress tiene ancora al 10% li si prende in carico col loro volume
  originale (speaker_suppress.release_for_live_mute), cosi' ne' il suo restore ne' il suo
  safety timer li riportano udibili; il suppress a sua volta salta gli speaker mutati qui.
- Ripristino di sicurezza dopo VOICE_LIVE_MUTE_MAX_S se la fine non arriva mai; stato
  persistito su file, ripristinato all'avvio dopo un riavvio dell'orchestrator.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from multi_ha import multi_ha

logger = logging.getLogger("JARVIS_LIVE_ROOM_MUTE")

# media_player supported_features (homeassistant.components.media_player.MediaPlayerEntityFeature)
FEATURE_VOLUME_SET = 4
FEATURE_VOLUME_MUTE = 8

_SKIP_STATES = {"unavailable", "unknown", "off"}

# Ritardo prima del secondo tentativo di ripristino (patchabile nei test)
RESTORE_RETRY_DELAY_S = 2.0
# Ripristini falliti a fine sessione (HA giu' per poco): riprovati in background ogni
# FAILED_RETRY_DELAY_S per FAILED_RETRY_ATTEMPTS volte, senza aspettare un riavvio.
FAILED_RETRY_DELAY_S = 60.0
FAILED_RETRY_ATTEMPTS = 10
_retry_task: Optional[asyncio.Task] = None

# (location_id, entity_id) -> {"location_id", "entity_id", "method": "mute"|"volume",
#                              "prior_volume", "holders": [device_id], "since"}
_holds: Dict[Tuple[str, str], dict] = {}
# device_id -> {"location_id", "room", "keys": [(location_id, entity_id)], "since", "safety_task"}
_devices: Dict[str, dict] = {}
# Ripristini falliti (HA giu' alla fine della sessione): restano nel file di stato, cosi'
# li riprova restore_leftovers al prossimo avvio. (location_id, entity_id) -> hold
_failed: Dict[Tuple[str, str], dict] = {}

# Un solo lock per tutto il modulo (piu' forte del lock per device: le entita' possono
# essere condivise fra device della stessa stanza). Uno per event loop.
_lock: Optional[asyncio.Lock] = None
_lock_loop = None


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def _norm(device_id: str) -> str:
    return (device_id or "").upper().strip()


def is_entity_held(location_id: str, entity_id: str) -> bool:
    """True se l'entita' e' mutata da una sessione live in corso."""
    return (location_id, entity_id) in _holds


def is_room_muted(device_id: str) -> bool:
    return _norm(device_id) in _devices


def get_live_muted() -> dict:
    """Stato per debug/dashboard."""
    now = time.time()
    return {
        dev: {
            "location_id": info["location_id"],
            "room": info["room"],
            "muted_seconds": round(now - info["since"], 1),
            "entities": [
                {"entity_id": k[1], "method": _holds[k]["method"],
                 "prior_volume": _holds[k]["prior_volume"], "holders": list(_holds[k]["holders"])}
                for k in info["keys"] if k in _holds
            ],
        }
        for dev, info in _devices.items()
    }


# ---------------------------------------------------------------------------
# Stanza -> media_player
# ---------------------------------------------------------------------------

def _get_device_config(device_id: str) -> Optional[dict]:
    from device_api import get_device_speaker_config
    return get_device_speaker_config(device_id)


def _media_players(location_id: str, room: Optional[str]) -> List[str]:
    """media_player della stanza (entity_maps, come speaker_suppress) o, con scope
    location, di tutta la location."""
    if config.VOICE_LIVE_MUTE_SCOPE == "location":
        from database import _get_conn
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT entity_id FROM entity_maps WHERE location_id = ? "
                "AND entity_type = 'media_player' AND entity_id IS NOT NULL",
                (location_id,)).fetchall()
        finally:
            conn.close()
        return [r["entity_id"] for r in rows]
    if not room:
        return []
    from speaker_suppress import _get_room_media_players
    return _get_room_media_players(location_id, room)


def _excluded(entity_id: str) -> bool:
    eid = entity_id.lower()
    if eid in config.VOICE_LIVE_MUTE_EXCLUDE_ENTITIES:
        return True
    return any(p in eid for p in config.VOICE_LIVE_MUTE_EXCLUDE_PATTERNS)


def _resolve_targets(device_id: str, location_id: Optional[str],
                     room: Optional[str]) -> Tuple[Optional[str], Optional[str], List[str]]:
    cfg = None
    try:
        cfg = _get_device_config(device_id)
    except Exception as e:
        logger.warning(f"Live mute: config device {device_id} non leggibile: {e}")
    loc = (cfg or {}).get("location_id") or location_id
    room_name = (cfg or {}).get("friendly_name") or room
    if not loc:
        return None, room_name, []
    players = list(_media_players(loc, room_name))
    # Gli speaker associati al device contano sempre, anche se la mappa li mette altrove
    for key in ("output_speaker", "volume_speaker"):
        eid = (cfg or {}).get(key)
        if eid and eid.startswith("media_player.") and eid not in players:
            players.append(eid)
    seen = set()
    out = []
    for eid in players:
        if eid in seen or _excluded(eid):
            continue
        seen.add(eid)
        out.append(eid)
    return loc, room_name, out


# ---------------------------------------------------------------------------
# HA
# ---------------------------------------------------------------------------

async def _call(location_id: str, service: str, data: dict) -> bool:
    try:
        ok, msg = await multi_ha.call_service(location_id, "media_player", service, data)
    except Exception as e:
        ok, msg = False, str(e)
    if not ok:
        logger.error(f"Live mute: media_player.{service} fallito su {data.get('entity_id')}: {msg}")
    return bool(ok)


async def _set_volume(location_id: str, entity_id: str, volume: float) -> bool:
    return await _call(location_id, "volume_set", {"entity_id": entity_id, "volume_level": volume})


async def _restore_hold(hold: dict) -> bool:
    loc, eid = hold["location_id"], hold["entity_id"]
    for attempt in (1, 2):
        if hold["method"] == "mute":
            ok = await _call(loc, "volume_mute", {"entity_id": eid, "is_volume_muted": False})
        else:
            ok = await _set_volume(loc, eid, hold["prior_volume"])
        if ok:
            restore_volume = hold.get("restore_volume")
            if hold["method"] == "mute" and restore_volume is not None:
                # Volume tolto dal suppress della wake word: si rimette DOPO lo smute (alcune
                # integrazioni, Alexa, implementano il mute come volume 0 + volume salvato,
                # e un volume_set a entita' mutata la renderebbe udibile).
                await _set_volume(loc, eid, restore_volume)
            logger.info(f"🔊 Live mute: {eid} ripristinato ("
                        + ("smutato" if hold["method"] == "mute" else f"volume {hold['prior_volume']:.2f}")
                        + (f", volume {restore_volume:.2f}" if hold["method"] == "mute" and restore_volume is not None else "")
                        + ")")
            return True
        if attempt == 1:
            await asyncio.sleep(RESTORE_RETRY_DELAY_S)
    logger.error(f"Live mute: {eid} NON ripristinato dopo 2 tentativi")
    return False


# ---------------------------------------------------------------------------
# Persistenza (riavvio dell'orchestrator a stanza mutata)
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    if config.VOICE_LIVE_MUTE_STATE_FILE:
        return Path(config.VOICE_LIVE_MUTE_STATE_FILE)
    import database
    return Path(database.DB_PATH).parent / "live_room_mute.json"


def _persist():
    try:
        path = _state_file()
        entries = list(_holds.values()) + [h for k, h in _failed.items() if k not in _holds]
        if not entries:
            if path.exists():
                path.unlink()
            return
        data = [{k: v for k, v in h.items() if k != "holders"} for h in entries]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Live mute: stato non salvato: {e}")


async def restore_leftovers(delay: float = 5.0, attempts: int = 3, retry_delay: float = 30.0):
    """All'avvio: le sessioni live non sopravvivono al riavvio, quindi tutto quello che il
    file dice mutato e' un residuo da ripristinare."""
    try:
        path = _state_file()
        if not path.exists():
            return
        data = json.loads(path.read_text() or "[]")
    except Exception as e:
        logger.warning(f"Live mute: stato residuo illeggibile: {e}")
        return
    # Subito in _failed: una sessione che riparte sulla stessa stanza prima del ripristino
    # li adotta con lo stato "di prima" giusto invece di vederli "gia' mutati".
    for hold in data:
        if not isinstance(hold, dict) or not {"location_id", "entity_id", "method"} <= set(hold):
            continue
        key = (hold["location_id"], hold["entity_id"])
        hold.pop("holders", None)
        if key not in _holds:
            _failed.setdefault(key, hold)
    await asyncio.sleep(delay)
    for i in range(attempts):
        async with _get_lock():
            pending = [k for k in _failed if k not in _holds]
            if not pending:
                break
            logger.warning(f"Live mute: {len(pending)} media_player rimasti mutati da prima del riavvio, ripristino")
            for key in pending:
                hold = _failed.get(key)
                if hold and await _restore_hold(hold):
                    _failed.pop(key, None)
            _persist()
        if i < attempts - 1 and any(k not in _holds for k in _failed):
            await asyncio.sleep(retry_delay)
    async with _get_lock():
        _persist()
    left = [k[1] for k in _failed if k not in _holds]
    if left:
        logger.error(f"Live mute: residui NON ripristinati: {left}")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

async def mute_room(device_id: str, location_id: Optional[str] = None,
                    room: Optional[str] = None) -> dict:
    """Muta ogni media_player della stanza del device. Idempotente."""
    dev = _norm(device_id)
    if not config.VOICE_LIVE_MUTE_ENABLED:
        return {"status": "skip", "reason": "disabled"}

    async with _get_lock():
        if dev in _devices:
            return {"status": "already_muted", "device_id": dev}

        loc, room_name, players = _resolve_targets(dev, location_id, room)
        if not loc or not players:
            logger.info(f"Live mute: {dev} nessun media_player da mutare (location={loc}, stanza={room_name})")
            return {"status": "skip", "reason": "no_players"}

        # Speaker che il suppress della wake word tiene ancora al 10%: li si prende in carico
        # col volume originale, e il volume "di prima" e' quello, non il 10%.
        try:
            from speaker_suppress import release_for_live_mute, recent_original_volumes, SUPPRESS_VOLUME
            taken = await release_for_live_mute(loc, players)
            recent = recent_original_volumes(loc, players)
        except Exception as e:
            logger.warning(f"Live mute: interazione con speaker_suppress fallita: {e}")
            taken, recent, SUPPRESS_VOLUME = {}, {}, 0.10

        try:
            states = await multi_ha.get_states_bulk(loc, players) or {}
        except Exception as e:
            logger.error(f"Live mute: stati HA non letti per {players}: {e}")
            states = {}

        keys, muted = [], []
        for eid in players:
            key = (loc, eid)
            orig_suppress = taken.get(eid)

            hold = _holds.get(key)
            if hold:
                # Gia' mutato da un'altra sessione della stanza: ci si aggiunge
                if dev not in hold["holders"]:
                    hold["holders"].append(dev)
                keys.append(key)
                continue
            leftover = _failed.pop(key, None)
            if leftover:
                # Ripristino fallito di una sessione precedente: lo stato "di prima" vero e'
                # quello salvato allora, non quello (mutato) che HA riporta adesso.
                if leftover["method"] == "mute":
                    await _call(loc, "volume_mute", {"entity_id": eid, "is_volume_muted": True})
                else:
                    await _set_volume(loc, eid, 0)
                leftover.update(holders=[dev], since=time.time())
                _holds[key] = leftover
                keys.append(key)
                muted.append(eid)
                continue

            st = states.get(eid)
            state_value = ((st or {}).get("state") or "").lower()
            if not st or state_value in _SKIP_STATES:
                logger.info(f"Live mute: {eid} {state_value or 'senza stato'}, skip")
                if orig_suppress is not None:
                    await _set_volume(loc, eid, orig_suppress)
                continue

            attrs = st.get("attributes") or {}
            try:
                features = int(attrs.get("supported_features") or 0)
            except (TypeError, ValueError):
                features = 0
            was_muted = attrs.get("is_volume_muted") is True
            volume = attrs.get("volume_level")
            prior_volume = orig_suppress
            if prior_volume is None:
                prior_volume = volume
                # HA puo' riportare ancora il 10% del suppress appena ripristinato
                if (volume is not None and eid in recent
                        and abs(float(volume) - SUPPRESS_VOLUME) < 0.011):
                    prior_volume = recent[eid]

            if features & FEATURE_VOLUME_MUTE:
                if was_muted:
                    logger.info(f"Live mute: {eid} gia' mutato, non lo tocco")
                    if orig_suppress is not None:
                        await _set_volume(loc, eid, orig_suppress)
                    continue
                if await _call(loc, "volume_mute", {"entity_id": eid, "is_volume_muted": True}):
                    # Se il suppress l'aveva abbassato, il volume originale si rimette allo
                    # smute (vedi _restore_hold), non ora: a entita' mutata non e' sicuro.
                    _holds[key] = {"location_id": loc, "entity_id": eid, "method": "mute",
                                   "prior_volume": prior_volume, "restore_volume": orig_suppress,
                                   "holders": [dev], "since": time.time()}
                    keys.append(key)
                    muted.append(eid)
                    continue
                # mute rifiutato: si prova col volume

            if features & FEATURE_VOLUME_SET and prior_volume is not None:
                if float(prior_volume) <= 0:
                    logger.info(f"Live mute: {eid} gia' a volume 0, non lo tocco")
                    continue
                if await _set_volume(loc, eid, 0):
                    _holds[key] = {"location_id": loc, "entity_id": eid, "method": "volume",
                                   "prior_volume": float(prior_volume), "holders": [dev],
                                   "since": time.time()}
                    keys.append(key)
                    muted.append(eid)
                    continue
            else:
                logger.info(f"Live mute: {eid} non supporta mute ne' volume noto, skip")

            # Non zittito: almeno gli si rende il volume che il suppress gli aveva tolto
            if orig_suppress is not None:
                await _set_volume(loc, eid, orig_suppress)

        if not keys:
            _persist()
            return {"status": "skip", "reason": "nothing_muted"}

        _devices[dev] = {"location_id": loc, "room": room_name, "keys": keys,
                         "since": time.time(), "safety_task": None}
        _devices[dev]["safety_task"] = asyncio.create_task(
            _safety_restore(dev, config.VOICE_LIVE_MUTE_MAX_S))
        _persist()
        logger.info(f"🔇 Live mute: {dev} ({room_name}, {loc}) -> mutati {muted}"
                    + (f", condivisi {[k[1] for k in keys if k[1] not in muted]}" if len(keys) > len(muted) else ""))
        return {"status": "muted", "device_id": dev, "entities": muted,
                "shared": [k[1] for k in keys if k[1] not in muted]}


async def unmute_room(device_id: str, reason: str = "session_end") -> dict:
    """Ripristina quello che mute_room ha cambiato per il device. Idempotente."""
    dev = _norm(device_id)
    async with _get_lock():
        info = _devices.pop(dev, None)
        if not info:
            return {"status": "skip", "reason": "not_muted"}
        task = info.get("safety_task")
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

        restored, failed = [], []
        for key in info["keys"]:
            hold = _holds.get(key)
            if not hold:
                continue
            if dev in hold["holders"]:
                hold["holders"].remove(dev)
            if hold["holders"]:
                continue  # un'altra sessione della stanza lo tiene ancora mutato
            _holds.pop(key, None)
            if await _restore_hold(hold):
                restored.append(key[1])
                _failed.pop(key, None)
            else:
                failed.append(key[1])
                _failed[key] = hold
        _persist()
        if failed:
            _schedule_failed_retry()
        logger.info(f"🔊 Live mute: {dev} ripristino ({reason}) dopo {time.time() - info['since']:.0f}s "
                    f"-> {restored}" + (f", FALLITI {failed}" if failed else ""))
        return {"status": "restored", "device_id": dev, "entities": restored, "failed": failed}


def _schedule_failed_retry() -> None:
    """Un solo task di riprova alla volta per i ripristini falliti (_failed)."""
    global _retry_task
    if _retry_task is not None and not _retry_task.done():
        return
    _retry_task = asyncio.create_task(_retry_failed())


async def _retry_failed() -> None:
    for _ in range(FAILED_RETRY_ATTEMPTS):
        await asyncio.sleep(FAILED_RETRY_DELAY_S)
        async with _get_lock():
            pending = [k for k in _failed if k not in _holds]
            if not pending:
                return
            for key in pending:
                hold = _failed.get(key)
                if hold and await _restore_hold(hold):
                    _failed.pop(key, None)
            _persist()
            if not any(k not in _holds for k in _failed):
                return
    left = [k[1] for k in _failed if k not in _holds]
    if left:
        logger.error(f"Live mute: ripristini ancora falliti dopo {FAILED_RETRY_ATTEMPTS} tentativi: {left} "
                     f"(restano nel file di stato per il prossimo avvio)")


async def release_all(reason: str = "shutdown") -> None:
    for dev in list(_devices):
        try:
            await unmute_room(dev, reason)
        except Exception as e:
            logger.error(f"Live mute: ripristino {dev} ({reason}) fallito: {e}")


async def _safety_restore(device_id: str, timeout: float):
    try:
        await asyncio.sleep(timeout)
        if device_id in _devices:
            logger.warning(f"⚠️ Live mute: {device_id} ancora mutato dopo {timeout:.0f}s, ripristino di sicurezza")
            await unmute_room(device_id, "safety_timeout")
    except asyncio.CancelledError:
        pass
