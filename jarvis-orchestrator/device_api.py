"""
JARVIS Device API
- Endpoints per voice devices (AtomS3R, NabuVoice): config, heartbeat
- Auto-discovery di nuovi device
- Gestione configurazione dinamica
"""

import logging
from typing import Optional
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

import database

logger = logging.getLogger("JARVIS_DEVICE_API")

router = APIRouter()


# ===========================================================================
# REQUEST/RESPONSE MODELS
# ===========================================================================

class HeartbeatRequest(BaseModel):
    device_id: str = Field(..., description="MAC address senza separatori (AABBCCDDEEFF)")
    firmware_version: Optional[str] = None
    ip_address: Optional[str] = None
    uptime_seconds: Optional[int] = None


class DeviceConfigResponse(BaseModel):
    status: str  # "configured" o "unknown"
    friendly_name: Optional[str] = None
    location_id: Optional[str] = None


class HeartbeatResponse(BaseModel):
    friendly_name: Optional[str] = None
    location_id: Optional[str] = None


# ===========================================================================
# ENDPOINTS PER VOICE DEVICES
# ===========================================================================

@router.get("/device/config", response_model=DeviceConfigResponse)
@router.get("/device_config", response_model=DeviceConfigResponse, include_in_schema=False)
async def get_device_config(device_id: str, request: Request):
    """
    Endpoint chiamato dal voice device al boot per ottenere la sua configurazione.

    Se il device è sconosciuto, viene registrato automaticamente per auto-discovery
    e ritorna status="unknown" con friendly_name=null.

    Args:
        device_id: MAC address del device (formato AABBCCDDEEFF, 12 caratteri hex)

    Returns:
        - status: "configured" se ha un friendly_name, "unknown" altrimenti
        - friendly_name: nome configurato o null
        - location_id: ID della location associata
    """
    device_id = device_id.upper().strip()

    # Valida formato MAC address (12 caratteri hex)
    if len(device_id) != 12 or not all(c in "0123456789ABCDEF" for c in device_id):
        logger.warning(f"Invalid device_id format: {device_id}")
        return DeviceConfigResponse(status="invalid", friendly_name=None, location_id=None)

    # Cerca il device nel database
    device = database.get_voice_device(device_id)

    if device is None:
        # Device sconosciuto - registra per auto-discovery
        # Estrai IP dalla request se disponibile
        client_ip = request.client.host if request.client else None

        device = database.register_unknown_voice_device(
            device_id=device_id,
            ip_address=client_ip
        )
        logger.info(f"Auto-discovered new voice device: {device_id} from {client_ip}")

        return DeviceConfigResponse(
            status="unknown",
            friendly_name=None,
            location_id=device.location_id
        )

    # Device esistente
    if device.is_configured:
        logger.debug(f"Device {device_id} configured as '{device.friendly_name}'")
        return DeviceConfigResponse(
            status="configured",
            friendly_name=device.friendly_name,
            location_id=device.location_id
        )
    else:
        logger.debug(f"Device {device_id} known but not configured")
        return DeviceConfigResponse(
            status="unknown",
            friendly_name=None,
            location_id=device.location_id
        )


@router.post("/device/heartbeat", response_model=HeartbeatResponse)
@router.post("/heartbeat", response_model=HeartbeatResponse, include_in_schema=False)
async def device_heartbeat(data: HeartbeatRequest, request: Request):
    """
    Heartbeat periodico dal voice device (ogni 5 minuti).

    Aggiorna last_seen_at e ritorna la configurazione corrente.
    Permette al device di ricevere aggiornamenti senza reboot.

    Args:
        device_id: MAC address del device
        firmware_version: Versione firmware (opzionale)
        ip_address: IP address del device (opzionale, sovrascrive quello dalla request)
        uptime_seconds: Tempo di uptime in secondi (opzionale, per diagnostica)

    Returns:
        - friendly_name: nome configurato corrente
        - location_id: ID della location corrente
    """
    device_id = data.device_id.upper().strip()

    # Usa l'IP dalla request se non fornito nel body
    ip_address = data.ip_address
    if not ip_address and request.client:
        ip_address = request.client.host

    # Aggiorna heartbeat
    device = database.update_voice_device_heartbeat(
        device_id=device_id,
        firmware_version=data.firmware_version,
        ip_address=ip_address
    )

    if device is None:
        # Device non esiste - registra per auto-discovery
        device = database.register_unknown_voice_device(
            device_id=device_id,
            firmware_version=data.firmware_version,
            ip_address=ip_address
        )
        logger.info(f"Heartbeat from unknown device, registered: {device_id}")

    if data.uptime_seconds:
        logger.debug(f"Heartbeat from {device_id}: uptime={data.uptime_seconds}s, fw={data.firmware_version}")

    return HeartbeatResponse(
        friendly_name=device.friendly_name,
        location_id=device.location_id
    )


# ===========================================================================
# UTILITY FUNCTIONS (usate da main.py per fallback chain)
# ===========================================================================

def get_device_speaker_config(device_id: str) -> Optional[dict]:
    """
    Recupera la configurazione speaker per un device.

    Usato da /voice_stream per determinare dove inviare l'output audio.

    Returns:
        Dict con:
        - output_speaker: entity_id del media_player principale
        - fallback_speaker: entity_id del media_player fallback (opzionale)
        - fallback_telegram: bool, se usare Telegram come fallback
        - fallback_local_speaker: bool, se usare speaker locale come ultimo fallback
        - location_id: ID della location
        - enabled: se il device è abilitato

        None se il device non esiste o non è configurato
    """
    device = database.get_voice_device(device_id)

    if device is None:
        return None

    if not device.is_configured:
        return None

    if not device.enabled:
        return None

    return {
        "output_speaker": device.output_speaker,
        "fallback_speaker": device.fallback_speaker,
        "fallback_telegram": device.fallback_telegram,
        "fallback_local_speaker": device.fallback_local_speaker,
        "use_internal_speaker": device.use_internal_speaker,
        "speaker_volume": device.speaker_volume,
        "volume_speaker": device.volume_speaker,
        "location_id": device.location_id,
        "enabled": device.enabled,
        "friendly_name": device.friendly_name,
        "device_type": device.device_type,
        "forced_user_id": device.forced_user_id
    }
