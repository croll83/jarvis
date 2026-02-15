"""
JARVIS Speaker Suppress Module
- Abbassa il volume di TUTTI i media_player nella stanza dell'AtomS3R quando viene rilevata la wake word
- Ripristina i volumi originali dopo lo STT (o dopo un safety timeout)
- Evita di toccare speaker che non stanno riproducendo nulla
- Lock per-device per gestire concorrenza
"""

import asyncio
import logging
import time
from typing import Optional, Dict, List

from multi_ha import multi_ha
from device_api import get_device_speaker_config

logger = logging.getLogger("JARVIS_SPEAKER_SUPPRESS")

# Volume target durante la registrazione (10% = 0.1)
SUPPRESS_VOLUME = 0.10

# Safety timeout: auto-restore dopo N secondi se il firmware non completa
SAFETY_TIMEOUT_SECONDS = 30

# Stati interni dei device soppressi
# Chiave: device_id dell'AtomS3R
# Valore: dict con lista di speaker soppressi e info per il restore
_suppressed_devices: Dict[str, dict] = {}

# Lock per-device per evitare race condition
_device_locks: Dict[str, asyncio.Lock] = {}


def _get_device_lock(device_id: str) -> asyncio.Lock:
    """Ottiene (o crea) un lock per uno specifico device."""
    if device_id not in _device_locks:
        _device_locks[device_id] = asyncio.Lock()
    return _device_locks[device_id]


def _get_room_media_players(location_id: str, room_name: str) -> List[str]:
    """
    Trova tutti i media_player entity_id nella stanza specificata.
    Usa l'entity_map del database.
    """
    from database import _get_conn

    room_lower = room_name.lower().strip()
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT entity_id FROM entity_maps
        WHERE location_id = ? AND LOWER(room) = ? AND entity_type = 'media_player'
        AND entity_id IS NOT NULL
    """, (location_id, room_lower))
    rows = c.fetchall()
    conn.close()

    return [row['entity_id'] for row in rows]


async def suppress_speaker(device_id: str) -> dict:
    """
    Abbassa il volume di TUTTI i media_player nella stanza dell'AtomS3R.

    Flusso:
    1. Lookup device_id → room (friendly_name) + location_id
    2. Query entity_map per tutti i media_player nella stanza
    3. Fetch stato di tutti gli speaker da HA
    4. Per ogni speaker in play: salva volume originale + imposta a 10%
    5. Avvia safety timeout per auto-restore

    Args:
        device_id: MAC address del device AtomS3R (formato AABBCCDDEEFF)

    Returns:
        dict con status e dettagli
    """
    device_id = device_id.upper().strip()

    # 1. Lookup configurazione device
    device_config = get_device_speaker_config(device_id)
    if not device_config:
        logger.warning(f"Suppress: device {device_id} non configurato")
        return {"status": "skip", "reason": "device_not_configured"}

    room_name = device_config.get("friendly_name")
    location_id = device_config.get("location_id")

    if not room_name or not location_id:
        logger.warning(f"Suppress: device {device_id} senza room/location")
        return {"status": "skip", "reason": "no_room_configured"}

    # Lock per questo device
    lock = _get_device_lock(device_id)

    async with lock:
        # Se già soppresso, skip (evita doppio suppress)
        if device_id in _suppressed_devices:
            logger.debug(f"Suppress: {device_id} già soppresso, skip")
            return {"status": "already_suppressed", "device_id": device_id}

        # 2. Trova tutti i media_player nella stanza
        room_players = _get_room_media_players(location_id, room_name)
        if not room_players:
            logger.info(f"Suppress: nessun media_player trovato nella stanza '{room_name}'")
            return {"status": "skip", "reason": "no_players_in_room"}

        logger.info(f"Suppress: stanza '{room_name}' ha {len(room_players)} media_player: {room_players}")

        # 3. Fetch stato di tutti gli speaker
        try:
            states = await multi_ha.get_states_bulk(location_id, room_players)
        except Exception as e:
            logger.error(f"Suppress: errore fetch stati per stanza '{room_name}': {e}")
            return {"status": "error", "reason": f"state_fetch_failed: {e}"}

        # 4. Per ogni speaker in play, abbassa il volume
        suppressed_list = []
        for entity_id in room_players:
            speaker_state = states.get(entity_id)
            if not speaker_state:
                continue

            state_value = (speaker_state.get("state") or "").lower()
            if state_value in ("off", "idle", "standby", "unavailable", "unknown"):
                logger.debug(f"Suppress: {entity_id} è {state_value}, skip")
                continue

            attributes = speaker_state.get("attributes", {})
            original_volume = attributes.get("volume_level")
            if original_volume is None:
                original_volume = 0.5

            # Non sopprimere se il volume è già molto basso
            if original_volume <= SUPPRESS_VOLUME:
                logger.debug(f"Suppress: {entity_id} volume già {original_volume:.2f}, skip")
                continue

            # Abbassa il volume
            logger.info(f"🔉 Suppress: {entity_id} volume {original_volume:.2f} → {SUPPRESS_VOLUME}")
            success, msg = await multi_ha.call_service(
                location_id, "media_player", "volume_set",
                {"entity_id": entity_id, "volume_level": SUPPRESS_VOLUME}
            )

            if success:
                suppressed_list.append({
                    "entity_id": entity_id,
                    "original_volume": original_volume
                })
            else:
                logger.error(f"Suppress: volume_set fallito su {entity_id}: {msg}")

        if not suppressed_list:
            logger.info(f"Suppress: nessun speaker in play nella stanza '{room_name}'")
            return {"status": "skip", "reason": "no_playing_speakers"}

        # 5. Registra lo stato per il restore
        _suppressed_devices[device_id] = {
            "speakers": suppressed_list,
            "location_id": location_id,
            "room_name": room_name,
            "suppressed_at": time.time(),
            "safety_task": None
        }

        # 6. Avvia safety timeout
        safety_task = asyncio.create_task(
            _safety_restore(device_id, SAFETY_TIMEOUT_SECONDS)
        )
        _suppressed_devices[device_id]["safety_task"] = safety_task

        entity_ids = [s["entity_id"] for s in suppressed_list]
        logger.info(f"🔉 Suppress completato: {device_id} ({room_name}) → {len(suppressed_list)} speaker: {entity_ids}")

        return {
            "status": "suppressed",
            "device_id": device_id,
            "room": room_name,
            "speakers_suppressed": len(suppressed_list),
            "entities": entity_ids
        }


async def restore_speaker(device_id: str) -> dict:
    """
    Ripristina il volume di tutti gli speaker soppressi per un device AtomS3R.

    Args:
        device_id: MAC address del device AtomS3R

    Returns:
        dict con status e dettagli
    """
    device_id = device_id.upper().strip()

    lock = _get_device_lock(device_id)

    async with lock:
        if device_id not in _suppressed_devices:
            logger.debug(f"Restore: {device_id} non è soppresso, skip")
            return {"status": "skip", "reason": "not_suppressed"}

        info = _suppressed_devices.pop(device_id)

        # Cancella safety timeout
        safety_task = info.get("safety_task")
        if safety_task and not safety_task.done():
            safety_task.cancel()

        location_id = info["location_id"]
        speakers = info["speakers"]
        room_name = info["room_name"]
        suppressed_duration = time.time() - info["suppressed_at"]

        # Ripristina tutti gli speaker
        restored_count = 0
        for speaker in speakers:
            entity_id = speaker["entity_id"]
            original_volume = speaker["original_volume"]

            logger.info(f"🔊 Restore: {entity_id} volume → {original_volume:.2f}")
            success, msg = await multi_ha.call_service(
                location_id, "media_player", "volume_set",
                {"entity_id": entity_id, "volume_level": original_volume}
            )

            if success:
                restored_count += 1
            else:
                logger.error(f"Restore: volume_set fallito su {entity_id}: {msg}")

        entity_ids = [s["entity_id"] for s in speakers]
        logger.info(f"🔊 Restore completato: {device_id} ({room_name}) → "
                     f"{restored_count}/{len(speakers)} speaker (dopo {suppressed_duration:.1f}s)")

        return {
            "status": "restored",
            "device_id": device_id,
            "room": room_name,
            "speakers_restored": restored_count,
            "entities": entity_ids,
            "suppressed_seconds": round(suppressed_duration, 1)
        }


async def restore_speaker_by_entity(entity_id: str) -> dict:
    """
    Ripristina un singolo speaker dato il suo entity_id.
    Cerca in tutti i device soppressi e rimuove solo quello specifico.
    Mantenuto per backward compatibility.
    """
    for device_id, info in list(_suppressed_devices.items()):
        for speaker in info["speakers"]:
            if speaker["entity_id"] == entity_id:
                # Trovato — fai restore dell'intero device
                return await restore_speaker(info.get("device_id", device_id))

    return {"status": "skip", "reason": "not_suppressed"}


async def _safety_restore(device_id: str, timeout: float):
    """
    Safety timeout: ripristina i volumi dopo N secondi se non è stato fatto esplicitamente.
    Protegge da crash del firmware, WiFi drop, ecc.
    """
    try:
        await asyncio.sleep(timeout)

        # Se dopo il timeout il device è ancora soppresso, ripristina
        if device_id in _suppressed_devices:
            logger.warning(f"⚠️ Safety restore: {device_id} ancora soppresso dopo {timeout}s, "
                           f"ripristino automatico")
            await restore_speaker(device_id)
    except asyncio.CancelledError:
        # Il restore è stato fatto normalmente prima del timeout
        pass


def get_suppressed_speakers() -> dict:
    """Ritorna lo stato di tutti i device soppressi (per debug/dashboard)."""
    result = {}
    now = time.time()
    for device_id, info in _suppressed_devices.items():
        result[device_id] = {
            "room": info["room_name"],
            "location_id": info["location_id"],
            "speakers": [
                {"entity_id": s["entity_id"], "original_volume": s["original_volume"]}
                for s in info["speakers"]
            ],
            "suppressed_seconds": round(now - info["suppressed_at"], 1)
        }
    return result
