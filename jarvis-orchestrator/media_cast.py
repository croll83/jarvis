"""
JARVIS Media Cast — Cast media (video/images) to Samsung TVs

- Riceve file binari o URL
- Salva in /app/data/www/cast/ (accessibile via HA /local/cast/)
- Riproduce su Samsung TV via ha-samsungtv-smart integration
- Video: media_content_type="url" (UPnP nativo, browser fallback)
- Immagini: media_content_type="browser" (Tizen browser, auto-close via KEY_EXIT)
- Cleanup automatico file scaduti
"""

import os
import uuid
import logging
import asyncio
import aiohttp
import time
from datetime import datetime
from typing import Tuple, Optional, Dict, Any
from pathlib import Path

import config

logger = logging.getLogger("JARVIS_MEDIA_CAST")

# Formati supportati: estensione → tipo media
ALLOWED_EXTENSIONS = {
    ".mp4": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}

# Cast attivi (per tracciare timer KEY_EXIT)
# {tv_entity: {"task": asyncio.Task, "cast_id": str, "started_at": float}}
_active_casts: Dict[str, Dict[str, Any]] = {}


# ==========================================================================
# FILE MANAGEMENT
# ==========================================================================

def _ensure_cast_dir():
    """Crea directory cast se non esiste."""
    Path(config.CAST_DIR).mkdir(parents=True, exist_ok=True)


def detect_media_type(filename: str) -> Optional[str]:
    """Rileva tipo media dall'estensione. Ritorna 'video' o 'image' o None."""
    ext = os.path.splitext(filename)[1].lower()
    return ALLOWED_EXTENSIONS.get(ext)


def _generate_cast_filename(original_filename: str) -> str:
    """Genera filename univoco preservando l'estensione originale."""
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".mp4"
    return f"cast_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"


async def save_cast_file(
    file_bytes: bytes,
    original_filename: str
) -> Tuple[bool, Optional[str], str]:
    """
    Salva file uploadato nella directory cast.

    Returns:
        (success, local_path, message)
    """
    _ensure_cast_dir()

    if len(file_bytes) > config.CAST_MAX_FILE_SIZE:
        mb = config.CAST_MAX_FILE_SIZE // (1024 * 1024)
        return False, None, f"File troppo grande (max {mb} MB)"

    media_type = detect_media_type(original_filename)
    if not media_type:
        ext = os.path.splitext(original_filename)[1]
        return False, None, f"Formato non supportato: {ext}. Usa mp4, png, jpg."

    filename = _generate_cast_filename(original_filename)
    local_path = os.path.join(config.CAST_DIR, filename)

    try:
        with open(local_path, 'wb') as f:
            f.write(file_bytes)
        logger.info(f"Cast file saved: {local_path} ({len(file_bytes)} bytes)")
        return True, local_path, "OK"
    except Exception as e:
        logger.error(f"Failed to save cast file: {e}")
        return False, None, str(e)


async def download_and_save(url: str) -> Tuple[bool, Optional[str], str]:
    """
    Scarica contenuto da URL e salva nella directory cast.

    Returns:
        (success, local_path, message)
    """
    _ensure_cast_dir()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    return False, None, f"Download fallito: HTTP {resp.status}"

                content_length = resp.content_length or 0
                if content_length > config.CAST_MAX_FILE_SIZE:
                    mb = config.CAST_MAX_FILE_SIZE // (1024 * 1024)
                    return False, None, f"File troppo grande (max {mb} MB)"

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
                        return False, None, f"Tipo contenuto non supportato: {ct}"

                # Scarica a chunk per rispettare il limite
                chunks = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > config.CAST_MAX_FILE_SIZE:
                        return False, None, "File troppo grande durante il download"
                    chunks.append(chunk)

                file_bytes = b"".join(chunks)

        return await save_cast_file(file_bytes, filename)

    except asyncio.TimeoutError:
        return False, None, "Download timeout (60s)"
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False, None, str(e)


# ==========================================================================
# HA URL RESOLUTION
# ==========================================================================

def get_cast_url(local_path: str, location_id: str = None) -> str:
    """
    Converte path locale in URL accessibile dalla TV via HA.

    /app/data/www/cast/file.mp4 → http://HA:8123/local/cast/file.mp4

    Stesso pattern di image_generation.get_ha_accessible_url().
    """
    from database import get_location

    filename = os.path.basename(local_path)

    location = get_location(location_id)
    if location and location.hass_url:
        ha_url = location.hass_url.rstrip('/')
    else:
        ha_url = config.HASS_URL_DEFAULT.rstrip('/')

    return f"{ha_url}/local/cast/{filename}"


# ==========================================================================
# TV ENTITY RESOLUTION
# ==========================================================================

def resolve_tv_entity(
    tv_entity: str = None,
    room: str = None,
    location_id: str = None
) -> Optional[str]:
    """
    Risolve entity_id TV da entity_id diretto o nome stanza.

    Priorità:
    1. tv_entity diretto (normalizzato a media_player.xxx)
    2. Risoluzione per stanza via extract_tv_from_room()
    """
    if tv_entity:
        if not tv_entity.startswith("media_player."):
            tv_entity = f"media_player.{tv_entity.lower().replace(' ', '_')}"
        return tv_entity

    if room:
        from image_generation import extract_tv_from_room
        return extract_tv_from_room(room, location_id)

    return None


# ==========================================================================
# SAMSUNG TV PLAYBACK
# ==========================================================================

async def cast_to_tv(
    media_url: str,
    tv_entity: str,
    media_type: str,
    duration: int = 30,
    location_id: str = None
) -> Tuple[bool, str]:
    """
    Cast media su Samsung TV via HA media_player.play_media.

    Strategia:
    - Video (mp4): media_content_type="url" → UPnP AVTransport (player nativo,
      fullscreen, no switch sorgente). Fallback a browser Tizen su TV 2022+.
    - Immagine (png/jpg): media_content_type="browser" → apre browser Tizen.
      Programma KEY_EXIT dopo `duration` secondi per chiudere il browser.

    Args:
        media_url: URL HTTP raggiungibile dalla TV (via HA /local/)
        tv_entity: Entity ID del media_player (es: media_player.tv_soggiorno)
        media_type: "video" o "image"
        duration: Durata display in secondi per immagini (0=indefinito)
        location_id: ID location HA

    Returns:
        (success, message)
    """
    from integrations import call_hass_service

    # Cancella cast attivo su questa TV (es: timer KEY_EXIT precedente)
    await _cancel_active_cast(tv_entity)

    if media_type == "video":
        service_data = {
            "entity_id": tv_entity,
            "media_content_type": "url",
            "media_content_id": media_url,
        }
    elif media_type == "image":
        service_data = {
            "entity_id": tv_entity,
            "media_content_type": "browser",
            "media_content_id": media_url,
        }
    else:
        return False, f"Tipo media non supportato: {media_type}"

    success, message = await call_hass_service(
        location_id, "media_player", "play_media", service_data
    )

    if not success:
        logger.warning(f"Cast failed on {tv_entity}: {message}")
        return False, f"Errore cast su TV: {message}"

    logger.info(f"Cast {media_type} on {tv_entity}: {media_url}")

    # Per immagini: programma KEY_EXIT per chiudere il browser dopo duration
    if media_type == "image" and duration > 0:
        task = asyncio.create_task(
            _delayed_close_browser(tv_entity, duration, location_id)
        )
        _active_casts[tv_entity] = {
            "task": task,
            "cast_id": os.path.basename(media_url),
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
    """Rimuove file cast più vecchi del TTL."""
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
