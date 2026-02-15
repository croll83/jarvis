"""
JARVIS Speaker Suppress Module
- Abbassa il volume dello speaker Echo associato a un AtomS3R quando viene rilevata la wake word
- Ripristina il volume originale dopo lo STT (o dopo un safety timeout)
- Evita di toccare speaker che non stanno riproducendo nulla
- Lock per-speaker per gestire concorrenza multi-device
"""

import asyncio
import logging
import time
from typing import Optional, Dict

from multi_ha import multi_ha
from device_api import get_device_speaker_config

logger = logging.getLogger("JARVIS_SPEAKER_SUPPRESS")

# Volume target durante la registrazione (10% = 0.1)
SUPPRESS_VOLUME = 0.10

# Safety timeout: auto-restore dopo N secondi se il firmware non completa
SAFETY_TIMEOUT_SECONDS = 30

# Stati interni dei speaker soppressi
# Chiave: entity_id dello speaker
# Valore: dict con info per il restore
_suppressed_speakers: Dict[str, dict] = {}

# Lock per-speaker per evitare race condition
_speaker_locks: Dict[str, asyncio.Lock] = {}


def _get_speaker_lock(entity_id: str) -> asyncio.Lock:
    """Ottiene (o crea) un lock per uno specifico speaker."""
    if entity_id not in _speaker_locks:
        _speaker_locks[entity_id] = asyncio.Lock()
    return _speaker_locks[entity_id]


async def suppress_speaker(device_id: str) -> dict:
    """
    Abbassa il volume dello speaker associato a un device AtomS3R.

    Flusso:
    1. Lookup device_id → output_speaker + location_id
    2. Fetch stato attuale dello speaker da HA
    3. Se lo speaker è idle/off/standby → skip (niente da abbassare)
    4. Salva volume corrente
    5. Invia volume_set a 10%
    6. Avvia safety timeout per auto-restore

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

    entity_id = device_config.get("output_speaker")
    location_id = device_config.get("location_id")

    if not entity_id or not location_id:
        logger.warning(f"Suppress: device {device_id} senza speaker/location")
        return {"status": "skip", "reason": "no_speaker_configured"}

    # Lock per questo speaker
    lock = _get_speaker_lock(entity_id)

    async with lock:
        # Se già soppresso, skip (evita doppio suppress)
        if entity_id in _suppressed_speakers:
            logger.debug(f"Suppress: {entity_id} già soppresso, skip")
            return {"status": "already_suppressed", "entity_id": entity_id}

        # 2. Fetch stato attuale dello speaker
        try:
            states = await multi_ha.get_states_bulk(location_id, [entity_id])
            speaker_state = states.get(entity_id)
        except Exception as e:
            logger.error(f"Suppress: errore fetch stato {entity_id}: {e}")
            return {"status": "error", "reason": f"state_fetch_failed: {e}"}

        if not speaker_state:
            logger.warning(f"Suppress: stato non trovato per {entity_id}")
            return {"status": "skip", "reason": "state_not_found"}

        # 3. Controlla se lo speaker sta riproducendo qualcosa
        state_value = speaker_state.get("state", "").lower()
        if state_value in ("off", "idle", "standby", "unavailable", "unknown"):
            logger.info(f"Suppress: {entity_id} è {state_value}, skip (niente in riproduzione)")
            return {"status": "skip", "reason": f"speaker_{state_value}"}

        # 4. Salva volume corrente
        attributes = speaker_state.get("attributes", {})
        original_volume = attributes.get("volume_level")

        if original_volume is None:
            logger.warning(f"Suppress: {entity_id} senza volume_level, default 0.5")
            original_volume = 0.5

        # Non sopprimere se il volume è già molto basso
        if original_volume <= SUPPRESS_VOLUME:
            logger.info(f"Suppress: {entity_id} volume già {original_volume:.2f} <= {SUPPRESS_VOLUME}, skip")
            return {"status": "skip", "reason": "volume_already_low"}

        # 5. Abbassa il volume
        logger.info(f"🔉 Suppress: {entity_id} volume {original_volume:.2f} → {SUPPRESS_VOLUME}")

        success, msg = await multi_ha.call_service(
            location_id,
            "media_player",
            "volume_set",
            {
                "entity_id": entity_id,
                "volume_level": SUPPRESS_VOLUME
            }
        )

        if not success:
            logger.error(f"Suppress: volume_set fallito su {entity_id}: {msg}")
            return {"status": "error", "reason": f"volume_set_failed: {msg}"}

        # 6. Registra lo stato per il restore
        _suppressed_speakers[entity_id] = {
            "original_volume": original_volume,
            "location_id": location_id,
            "device_id": device_id,
            "suppressed_at": time.time(),
            "safety_task": None
        }

        # 7. Avvia safety timeout
        safety_task = asyncio.create_task(
            _safety_restore(entity_id, SAFETY_TIMEOUT_SECONDS)
        )
        _suppressed_speakers[entity_id]["safety_task"] = safety_task

        logger.info(f"🔉 Suppress completato: {entity_id} ({original_volume:.2f} → {SUPPRESS_VOLUME})")

        return {
            "status": "suppressed",
            "entity_id": entity_id,
            "original_volume": original_volume,
            "suppress_volume": SUPPRESS_VOLUME
        }


async def restore_speaker(device_id: str) -> dict:
    """
    Ripristina il volume dello speaker associato a un device AtomS3R.

    Args:
        device_id: MAC address del device AtomS3R

    Returns:
        dict con status e dettagli
    """
    device_id = device_id.upper().strip()

    # Lookup configurazione device
    device_config = get_device_speaker_config(device_id)
    if not device_config:
        return {"status": "skip", "reason": "device_not_configured"}

    entity_id = device_config.get("output_speaker")
    if not entity_id:
        return {"status": "skip", "reason": "no_speaker_configured"}

    return await restore_speaker_by_entity(entity_id)


async def restore_speaker_by_entity(entity_id: str) -> dict:
    """
    Ripristina il volume di uno speaker dato il suo entity_id.

    Args:
        entity_id: HA media_player entity_id

    Returns:
        dict con status e dettagli
    """
    lock = _get_speaker_lock(entity_id)

    async with lock:
        if entity_id not in _suppressed_speakers:
            logger.debug(f"Restore: {entity_id} non è soppresso, skip")
            return {"status": "skip", "reason": "not_suppressed"}

        info = _suppressed_speakers.pop(entity_id)

        # Cancella safety timeout
        safety_task = info.get("safety_task")
        if safety_task and not safety_task.done():
            safety_task.cancel()

        original_volume = info["original_volume"]
        location_id = info["location_id"]
        suppressed_duration = time.time() - info["suppressed_at"]

        # Ripristina il volume
        logger.info(f"🔊 Restore: {entity_id} volume {SUPPRESS_VOLUME} → {original_volume:.2f} "
                     f"(dopo {suppressed_duration:.1f}s)")

        success, msg = await multi_ha.call_service(
            location_id,
            "media_player",
            "volume_set",
            {
                "entity_id": entity_id,
                "volume_level": original_volume
            }
        )

        if not success:
            logger.error(f"Restore: volume_set fallito su {entity_id}: {msg}")
            return {"status": "error", "reason": f"volume_set_failed: {msg}"}

        logger.info(f"🔊 Restore completato: {entity_id} → {original_volume:.2f}")

        return {
            "status": "restored",
            "entity_id": entity_id,
            "original_volume": original_volume,
            "suppressed_seconds": round(suppressed_duration, 1)
        }


async def _safety_restore(entity_id: str, timeout: float):
    """
    Safety timeout: ripristina il volume dopo N secondi se non è stato fatto esplicitamente.
    Protegge da crash del firmware, WiFi drop, ecc.
    """
    try:
        await asyncio.sleep(timeout)

        # Se dopo il timeout lo speaker è ancora soppresso, ripristina
        if entity_id in _suppressed_speakers:
            logger.warning(f"⚠️ Safety restore: {entity_id} ancora soppresso dopo {timeout}s, "
                           f"ripristino automatico")
            await restore_speaker_by_entity(entity_id)
    except asyncio.CancelledError:
        # Il restore è stato fatto normalmente prima del timeout
        pass


def get_suppressed_speakers() -> dict:
    """Ritorna lo stato di tutti gli speaker soppressi (per debug/dashboard)."""
    result = {}
    now = time.time()
    for entity_id, info in _suppressed_speakers.items():
        result[entity_id] = {
            "original_volume": info["original_volume"],
            "device_id": info["device_id"],
            "location_id": info["location_id"],
            "suppressed_seconds": round(now - info["suppressed_at"], 1)
        }
    return result
