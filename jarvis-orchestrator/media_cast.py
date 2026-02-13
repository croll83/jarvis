"""
JARVIS Media Cast — Cast media (video/images) to Samsung TVs

Due modalità:
A) URL diretto: l'URL pubblico viene passato direttamente a play_media.
   La TV fetcha direttamente dall'URL. Supporta anche streaming (m3u8, ts).
B) Upload file: i bytes vengono uploadati su HA via media_source API,
   poi play_media con media-source:// URI → HA genera signed URL sulla LAN.

Strategia DLNA-first: cerca DLNA DMR nella stanza, fallback SamsungTV Smart.

Provider:
- DLNA DMR: play_media con MIME type standard (video/mp4, image/png, image/jpeg)
- SamsungTV Smart: play_media con type="url" (video) o type="browser" (immagini)
  Per immagini SamsungTV: auto-close browser via KEY_EXIT dopo duration
"""

import os
import uuid
import logging
import asyncio
import aiohttp
import time
from datetime import datetime
from typing import Tuple, Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger("JARVIS_MEDIA_CAST")

# Formati supportati per upload file: estensione → (tipo media, MIME type)
UPLOAD_EXTENSIONS = {
    ".mp4": ("video", "video/mp4"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
}

# Mapping estensione → MIME type per hint (best-effort, non bloccante)
# Usato per dare un content_type sensato a play_media quando si conosce l'estensione
KNOWN_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m3u8": "application/x-mpegURL",
    ".ts": "video/MP2T",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".flv": "video/x-flv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# Cast attivi (per tracciare timer KEY_EXIT su SamsungTV Smart)
# {tv_entity: {"task": asyncio.Task, "cast_id": str, "started_at": float}}
_active_casts: Dict[str, Dict[str, Any]] = {}

# Subfolder su HA /media/local/ dove salviamo i file cast
HA_MEDIA_CAST_FOLDER = "cast"


# ==========================================================================
# FILE HANDLING
# ==========================================================================

def detect_media_type(filename: str) -> Optional[str]:
    """Rileva tipo media dall'estensione (solo formati uploadabili). Ritorna 'video' o 'image' o None."""
    ext = os.path.splitext(filename)[1].lower()
    info = UPLOAD_EXTENSIONS.get(ext)
    return info[0] if info else None


def detect_media_type_url(url: str) -> str:
    """
    Rileva tipo media da URL. Best-effort basato su estensione.
    Se non riconosciuto, default 'video' (caso più comune per URL).
    """
    path = url.split("?")[0].split("#")[0]
    filename = path.split("/")[-1]
    ext = os.path.splitext(filename)[1].lower()
    # Immagini note
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"):
        return "image"
    # Tutto il resto (video, streaming, sconosciuto) → video
    return "video"


def _get_mime_type_for_url(url: str) -> str:
    """
    Rileva MIME type da URL. Best-effort basato su estensione.
    Se non riconosciuto, default 'video/mp4'.
    """
    path = url.split("?")[0].split("#")[0]
    filename = path.split("/")[-1]
    ext = os.path.splitext(filename)[1].lower()
    return KNOWN_MIME_TYPES.get(ext, "video/mp4")


def _get_mime_type(filename: str) -> Optional[str]:
    """Rileva MIME type dall'estensione (solo formati uploadabili)."""
    ext = os.path.splitext(filename)[1].lower()
    info = UPLOAD_EXTENSIONS.get(ext)
    return info[1] if info else None


def _generate_cast_filename(original_filename: str) -> str:
    """Genera filename univoco preservando l'estensione originale."""
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in UPLOAD_EXTENSIONS:
        ext = ".mp4"
    return f"cast_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"


async def download_url(url: str) -> Tuple[bool, Optional[bytes], Optional[str], str]:
    """
    Scarica contenuto da URL.

    Returns:
        (success, file_bytes, filename, message)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    return False, None, None, f"Download fallito: HTTP {resp.status}"

                content_length = resp.content_length or 0
                if content_length > config.CAST_MAX_FILE_SIZE:
                    mb = config.CAST_MAX_FILE_SIZE // (1024 * 1024)
                    return False, None, None, f"File troppo grande (max {mb} MB)"

                # Determina filename da URL o Content-Type
                filename = url.split("/")[-1].split("?")[0]
                if not detect_media_type(filename):
                    ct = resp.headers.get("Content-Type", "")
                    if "video/mp4" in ct:
                        filename = "download.mp4"
                    elif "image/png" in ct:
                        filename = "download.png"
                    elif "image/jpeg" in ct or "image/jpg" in ct:
                        filename = "download.jpg"
                    else:
                        return False, None, None, f"Tipo contenuto non supportato: {ct}"

                # Scarica a chunk per rispettare il limite
                chunks = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > config.CAST_MAX_FILE_SIZE:
                        return False, None, None, "File troppo grande durante il download"
                    chunks.append(chunk)

                return True, b"".join(chunks), filename, "OK"

    except asyncio.TimeoutError:
        return False, None, None, "Download timeout (60s)"
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False, None, None, str(e)


# ==========================================================================
# HA MEDIA SOURCE UPLOAD
# ==========================================================================

async def upload_to_ha(
    file_bytes: bytes,
    original_filename: str,
    location_id: str = None,
) -> Tuple[bool, Optional[str], str]:
    """
    Uploada file su HA via /api/media_source/local_source/upload.

    Il file finisce in /media/local/cast/{filename} su HA.
    Ritorna il media_content_id da usare con play_media:
      media-source://media_source/local/cast/{filename}

    Args:
        file_bytes: Contenuto binario del file
        original_filename: Nome originale (per estensione)
        location_id: ID location HA

    Returns:
        (success, media_content_id, message)
    """
    from database import get_location

    if len(file_bytes) > config.CAST_MAX_FILE_SIZE:
        mb = config.CAST_MAX_FILE_SIZE // (1024 * 1024)
        return False, None, f"File troppo grande (max {mb} MB)"

    mime_type = _get_mime_type(original_filename)
    if not mime_type:
        ext = os.path.splitext(original_filename)[1]
        return False, None, f"Formato non supportato: {ext}. Usa mp4, png, jpg."

    # Genera filename univoco
    filename = _generate_cast_filename(original_filename)

    # Recupera URL e token HA
    location = get_location(location_id)
    if location:
        ha_url = location.hass_url.rstrip('/')
        ha_token = location.hass_token
    else:
        ha_url = config.HASS_URL_DEFAULT.rstrip('/')
        ha_token = ""

    if not ha_token:
        return False, None, "Token HA non configurato per questa location"

    # Upload via media_source API
    upload_url = f"{ha_url}/api/media_source/local_source/upload"
    headers = {"Authorization": f"Bearer {ha_token}"}

    form = aiohttp.FormData()
    form.add_field(
        "media_content_id",
        f"media-source://media_source/local/{HA_MEDIA_CAST_FOLDER}"
    )
    form.add_field(
        "file",
        file_bytes,
        filename=filename,
        content_type=mime_type,
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                upload_url,
                headers=headers,
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    media_content_id = data.get("media_content_id", "")
                    logger.info(
                        f"Uploaded to HA: {filename} ({len(file_bytes)} bytes) "
                        f"→ {media_content_id}"
                    )
                    return True, media_content_id, "OK"
                else:
                    text = await resp.text()
                    logger.error(f"HA upload failed: HTTP {resp.status} - {text[:200]}")
                    return False, None, f"Upload HA fallito: HTTP {resp.status}"

    except Exception as e:
        logger.error(f"HA upload error: {e}")
        return False, None, f"Errore upload HA: {str(e)}"


# ==========================================================================
# HA MEDIA CLEANUP (via WebSocket)
# ==========================================================================

async def delete_from_ha(media_content_id: str, location_id: str = None) -> bool:
    """Elimina file da HA via media_source/local_source/remove."""
    from multi_ha import multi_ha

    try:
        client = multi_ha.clients.get(location_id)
        if not client:
            return False

        # WebSocket command
        await client.connect()
        msg_id = client._msg_id
        client._msg_id += 1

        await client._ws.send(
            __import__("json").dumps({
                "id": msg_id,
                "type": "media_source/local_source/remove",
                "media_content_id": media_content_id,
            })
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to delete from HA: {e}")
        return False


# ==========================================================================
# TV ENTITY RESOLUTION
# ==========================================================================

@dataclass
class CastTarget:
    """Target TV risolto con provider info."""
    entity_id: str
    provider: str  # "dlna" o "samsungtv"
    room: Optional[str] = None


def resolve_tv_entity(
    tv_entity: str = None,
    room: str = None,
    location_id: str = None
) -> Optional[CastTarget]:
    """
    Risolve entity_id TV da entity_id diretto o nome stanza.

    Se viene fornito un entity_id diretto, rileva il provider dall'entity_id.
    Se viene fornita una room, cerca prima DLNA DMR, poi SamsungTV Smart.

    Priorità per room:
    1. Entity DLNA DMR nella stanza (entity_id contiene "dlna")
    2. Entity SamsungTV Smart nella stanza (entity_id contiene "tv"/"samsung")
    3. Qualsiasi media_player nella stanza

    Returns:
        CastTarget con entity_id e provider, o None
    """
    if tv_entity:
        if not tv_entity.startswith("media_player."):
            tv_entity = f"media_player.{tv_entity.lower().replace(' ', '_')}"
        provider = "dlna" if "dlna" in tv_entity.lower() else "samsungtv"
        return CastTarget(entity_id=tv_entity, provider=provider)

    if room:
        return _find_tv_in_room(room, location_id)

    return None


def _find_tv_in_room(room: str, location_id: str = None) -> Optional[CastTarget]:
    """
    Cerca TV nella stanza con strategia DLNA-first.

    1. Cerca entity DLNA DMR (entity_id contiene "dlna") nella room
    2. Fallback: entity SamsungTV Smart (entity_id contiene "tv"/"samsung")
    """
    from database import get_entities_by_type

    try:
        media_players = get_entities_by_type(location_id, "media_player")
        room_lower = room.lower()

        dlna_match = None
        samsung_match = None
        any_match = None

        for mp in media_players:
            mp_room = mp.get("room", "").lower()
            mp_name = mp.get("name", "").lower()
            mp_entity = mp.get("entity_id", "").lower()

            # Match per room
            if room_lower not in mp_room and mp_room not in room_lower:
                continue

            entity_id = mp.get("entity_id") or f"media_player.{mp_name.replace(' ', '_')}"

            # DLNA DMR: priorità massima
            if "dlna" in mp_entity or "dlna" in mp_name:
                dlna_match = CastTarget(entity_id=entity_id, provider="dlna", room=room)

            # SamsungTV Smart: seconda priorità
            elif ("tv" in mp_name or "samsung" in mp_name) and not dlna_match:
                samsung_match = CastTarget(entity_id=entity_id, provider="samsungtv", room=room)

            # Qualsiasi media_player: ultima spiaggia
            elif not any_match:
                any_match = CastTarget(entity_id=entity_id, provider="samsungtv", room=room)

        result = dlna_match or samsung_match or any_match
        if result:
            logger.info(f"Resolved TV for room '{room}': {result.entity_id} (provider={result.provider})")
        return result

    except Exception as e:
        logger.warning(f"Could not find TV for room {room}: {e}")
        return None


# ==========================================================================
# TV PLAYBACK
# ==========================================================================

async def cast_to_tv(
    media_content_id: str,
    target: CastTarget,
    media_type: str,
    duration: int = 30,
    location_id: str = None
) -> Tuple[bool, str]:
    """
    Cast media su TV via HA media_player.play_media.

    Per DLNA DMR:
    - play_media con media-source:// URI + MIME type standard
    - HA risolve in signed URL → TV fetcha dalla LAN
    - Supporta video + immagini nativamente

    Per SamsungTV Smart:
    - play_media con media-source:// URI
    - media_content_type: "video/mp4" per video, "image/png"/"image/jpeg" per immagini
    - Per immagini: programma KEY_EXIT dopo duration per chiudere

    Args:
        media_content_id: URI media-source:// (da upload_to_ha)
        target: CastTarget con entity_id e provider
        media_type: "video" o "image"
        duration: Durata display in secondi per immagini (0=indefinito)
        location_id: ID location HA

    Returns:
        (success, message)
    """
    from integrations import call_hass_service

    tv_entity = target.entity_id

    # Cancella cast attivo su questa TV (es: timer KEY_EXIT precedente)
    await _cancel_active_cast(tv_entity)

    # Rileva MIME type dal media_content_id
    if media_type == "video":
        content_type = "video/mp4"
    elif media_type == "image":
        if media_content_id.lower().endswith(".png"):
            content_type = "image/png"
        else:
            content_type = "image/jpeg"
    else:
        return False, f"Tipo media non supportato: {media_type}"

    # play_media con media-source:// URI — HA risolve in signed URL
    service_data = {
        "entity_id": tv_entity,
        "media_content_type": content_type,
        "media_content_id": media_content_id,
    }

    success, message = await call_hass_service(
        location_id, "media_player", "play_media", service_data
    )

    if not success:
        logger.warning(f"Cast failed on {tv_entity} (provider={target.provider}): {message}")
        return False, f"Errore cast su TV: {message}"

    logger.info(f"Cast {media_type} on {tv_entity} (provider={target.provider}): {media_content_id}")

    # Per immagini su SamsungTV Smart: programma KEY_EXIT per chiudere il browser
    # DLNA non ne ha bisogno — le immagini restano finché non viene inviato stop
    if target.provider == "samsungtv" and media_type == "image" and duration > 0:
        task = asyncio.create_task(
            _delayed_close_browser(tv_entity, duration, location_id)
        )
        _active_casts[tv_entity] = {
            "task": task,
            "cast_id": media_content_id,
            "started_at": time.time(),
        }

    return True, "OK"


async def cast_url_to_tv(
    url: str,
    target: CastTarget,
    media_type: str,
    duration: int = 30,
    location_id: str = None
) -> Tuple[bool, str]:
    """
    Cast URL pubblico direttamente su TV via HA media_player.play_media.

    L'URL viene passato as-is a play_media — la TV lo fetcha direttamente.
    Funziona per qualsiasi URL raggiungibile dalla TV (internet pubblico).
    Supporta anche streaming HLS (.m3u8) e MPEG-TS (.ts).

    Args:
        url: URL pubblico del media
        target: CastTarget con entity_id e provider
        media_type: "video" o "image"
        duration: Durata display in secondi per immagini (0=indefinito)
        location_id: ID location HA

    Returns:
        (success, message)
    """
    from integrations import call_hass_service

    tv_entity = target.entity_id

    # Cancella cast attivo su questa TV
    await _cancel_active_cast(tv_entity)

    # Rileva content_type dall'URL (best-effort, default video/mp4)
    content_type = _get_mime_type_for_url(url)

    # play_media con URL diretto
    service_data = {
        "entity_id": tv_entity,
        "media_content_type": content_type,
        "media_content_id": url,
    }

    success, message = await call_hass_service(
        location_id, "media_player", "play_media", service_data
    )

    if not success:
        logger.warning(f"URL cast failed on {tv_entity} (provider={target.provider}): {message}")
        return False, f"Errore cast su TV: {message}"

    logger.info(f"URL cast {media_type} on {tv_entity} (provider={target.provider}): {url}")

    # Per immagini su SamsungTV Smart: programma KEY_EXIT per chiudere il browser
    if target.provider == "samsungtv" and media_type == "image" and duration > 0:
        task = asyncio.create_task(
            _delayed_close_browser(tv_entity, duration, location_id)
        )
        _active_casts[tv_entity] = {
            "task": task,
            "cast_id": url,
            "started_at": time.time(),
        }

    return True, "OK"


async def stop_cast(
    tv_entity: str,
    location_id: str = None
) -> Tuple[bool, str]:
    """
    Ferma cast attivo su una TV: invia KEY_EXIT e cancella il timer.

    Returns:
        (success, message)
    """
    # Cancella timer se presente
    await _cancel_active_cast(tv_entity)

    # Invia KEY_EXIT per chiudere browser/player
    success, msg = await _send_key_exit(tv_entity, location_id)
    if success:
        return True, "Cast fermato"
    return False, f"Errore stop cast: {msg}"


async def _delayed_close_browser(
    tv_entity: str,
    delay_seconds: int,
    location_id: str
):
    """
    Attende delay_seconds, poi invia KEY_EXIT per chiudere il browser Tizen,
    riportando la TV al contenuto precedente.
    """
    try:
        await asyncio.sleep(delay_seconds)
        success, msg = await _send_key_exit(tv_entity, location_id)
        if success:
            logger.info(f"Browser closed on {tv_entity} after {delay_seconds}s")
        else:
            logger.warning(f"Failed to close browser on {tv_entity}: {msg}")
    except asyncio.CancelledError:
        logger.debug(f"Browser close cancelled for {tv_entity}")
    except Exception as e:
        logger.error(f"Error closing browser on {tv_entity}: {e}")
    finally:
        _active_casts.pop(tv_entity, None)


async def _send_key_exit(tv_entity: str, location_id: str) -> Tuple[bool, str]:
    """
    Invia KEY_EXIT alla TV via remote.send_command.

    Usa l'entity remote.{tv_name} corrispondente al media_player.{tv_name}.
    """
    from integrations import call_hass_service

    # Deriva remote entity da media_player entity
    remote_entity = tv_entity.replace("media_player.", "remote.")

    service_data = {
        "entity_id": remote_entity,
        "command": "KEY_EXIT",
    }
    return await call_hass_service(
        location_id, "remote", "send_command", service_data
    )


async def _cancel_active_cast(tv_entity: str):
    """Cancella timer cast attivo per una TV."""
    active = _active_casts.pop(tv_entity, None)
    if active and active.get("task"):
        active["task"].cancel()
        try:
            await active["task"]
        except asyncio.CancelledError:
            pass
        logger.debug(f"Cancelled previous cast on {tv_entity}")


# ==========================================================================
# CLEANUP
# ==========================================================================

async def cleanup_cast_files(max_age_seconds: int = None):
    """Rimuove file cast locali più vecchi del TTL (staging temporaneo)."""
    max_age = max_age_seconds or config.CAST_FILE_TTL

    try:
        cast_dir = Path(config.CAST_DIR)
        if not cast_dir.exists():
            return

        now = time.time()
        removed = 0

        for filepath in cast_dir.glob("*"):
            if filepath.is_file():
                try:
                    if now - filepath.stat().st_mtime > max_age:
                        filepath.unlink()
                        removed += 1
                except Exception as e:
                    logger.warning(f"Could not delete {filepath}: {e}")

        if removed > 0:
            logger.info(f"Cast cleanup: removed {removed} old files")

    except Exception as e:
        logger.error(f"Cast cleanup error: {e}")
