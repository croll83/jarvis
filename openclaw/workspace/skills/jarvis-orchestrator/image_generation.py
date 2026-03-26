"""
JARVIS Image Generation + TV Display
- Genera immagini via Google Gemini 3 Pro
- Salva in /config/www/generated/ (accessibile via HA)
- Mostra su TV via media_player.play_media
- Opzionale: invia a Telegram
"""

import os
import uuid
import logging
import base64
import asyncio
from datetime import datetime
from typing import Tuple, Optional
from pathlib import Path

logger = logging.getLogger("JARVIS_IMAGE_GEN")

# Lazy import per evitare errori se google-generativeai non installato
genai = None


def _ensure_genai():
    """Lazy init del modulo google.generativeai."""
    global genai
    if genai is None:
        try:
            import google.generativeai as _genai
            import config
            if config.GEMINI_API_KEY:
                _genai.configure(api_key=config.GEMINI_API_KEY)
            genai = _genai
        except ImportError:
            logger.error("google-generativeai not installed. Run: pip install google-generativeai")
            raise ImportError("google-generativeai required for image generation")
    return genai


# ===========================================================================
# CONFIGURATION
# ===========================================================================

def _get_config():
    """Recupera configurazione per image generation."""
    import config
    return {
        "api_key": config.GEMINI_API_KEY,
        "model": config.GEMINI_IMAGE_MODEL,
        "images_dir": config.GENERATED_IMAGES_DIR,
        "ha_base_url": config.HASS_URL_DEFAULT,
    }


# ===========================================================================
# IMAGE GENERATION
# ===========================================================================

async def generate_image(prompt: str) -> Tuple[bool, Optional[str], str]:
    """
    Genera un'immagine da un prompt testuale usando Gemini 3 Pro.

    Args:
        prompt: Descrizione dell'immagine da generare

    Returns:
        Tuple[success: bool, local_path: Optional[str], message: str]
    """
    try:
        _ensure_genai()
        cfg = _get_config()

        if not cfg["api_key"]:
            return False, None, "GEMINI_API_KEY non configurata"

        model = genai.GenerativeModel(
            model_name=cfg["model"],
            generation_config={
                "response_modalities": ["TEXT", "IMAGE"]
            }
        )

        # Genera l'immagine (run in executor per non bloccare event loop)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(f"Create an image: {prompt}")
        )

        # Estrai l'immagine dalla risposta
        image_data = None
        mime_type = "image/png"

        for part in response.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                image_data = part.inline_data.data
                mime_type = getattr(part.inline_data, 'mime_type', 'image/png')
                break

        if not image_data:
            # Prova a vedere se c'è un messaggio di errore
            text_response = ""
            for part in response.parts:
                if hasattr(part, 'text'):
                    text_response += part.text

            if text_response:
                return False, None, f"Nessuna immagine generata: {text_response[:200]}"
            return False, None, "Nessuna immagine generata nella risposta"

        # Se i dati sono base64 encoded
        if isinstance(image_data, str):
            image_data = base64.b64decode(image_data)

        # Salva l'immagine
        success, local_path = await save_image_locally(image_data, mime_type)
        if not success:
            return False, None, "Errore nel salvare l'immagine"

        logger.info(f"Image generated successfully: {local_path}")
        return True, local_path, "Immagine generata con successo"

    except ImportError as e:
        logger.error(f"Import error: {e}")
        return False, None, str(e)
    except Exception as e:
        logger.error(f"Errore generazione immagine: {e}")
        return False, None, str(e)


async def save_image_locally(image_bytes: bytes, mime_type: str = "image/png", filename: str = None) -> Tuple[bool, Optional[str]]:
    """
    Salva l'immagine nella directory accessibile da HA.

    Returns:
        Tuple[success: bool, local_path: Optional[str]]
    """
    try:
        cfg = _get_config()
        images_dir = cfg["images_dir"]

        # Crea directory se non esiste
        Path(images_dir).mkdir(parents=True, exist_ok=True)

        # Determina estensione dal mime type
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        ext = ext_map.get(mime_type, ".png")

        # Genera filename univoco se non fornito
        if not filename:
            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"

        local_path = os.path.join(images_dir, filename)

        # Scrivi il file
        with open(local_path, 'wb') as f:
            f.write(image_bytes)

        logger.info(f"Immagine salvata: {local_path}")
        return True, local_path

    except Exception as e:
        logger.error(f"Errore salvataggio immagine: {e}")
        return False, None


def get_ha_accessible_url(local_path: str, location_id: str = None) -> str:
    """
    Converte path locale in URL accessibile da HA.

    /app/data/www/generated/image.png → http://HA:8123/local/generated/image.png

    Nota: usa l'URL della location specificata per supporto multi-location.
    """
    from database import get_location

    filename = os.path.basename(local_path)

    # Recupera URL HA per la location
    location = get_location(location_id)
    if location and location.hass_url:
        ha_url = location.hass_url.rstrip('/')
    else:
        cfg = _get_config()
        ha_url = cfg["ha_base_url"].rstrip('/')

    return f"{ha_url}/local/generated/{filename}"


# ===========================================================================
# TV DISPLAY
# ===========================================================================

async def show_image_on_tv(
    image_url: str,
    tv_entity: str,
    location_id: str = None
) -> Tuple[bool, str]:
    """
    Mostra un'immagine sulla TV via media_player.play_media.

    Args:
        image_url: URL dell'immagine (accessibile dalla rete locale)
        tv_entity: Entity ID del media_player (es: "media_player.tv_soggiorno")
        location_id: ID della location HA

    Returns:
        Tuple[success: bool, message: str]
    """
    from integrations import call_hass_service

    # Normalizza entity_id
    if not tv_entity.startswith("media_player."):
        tv_entity = f"media_player.{tv_entity.lower().replace(' ', '_')}"

    service_data = {
        "entity_id": tv_entity,
        "media_content_type": "image/png",
        "media_content_id": image_url
    }

    success, message = await call_hass_service(location_id, "media_player", "play_media", service_data)

    if success:
        logger.info(f"Image displayed on {tv_entity} at {location_id}")
    else:
        logger.warning(f"Failed to display image on TV: {message}")

    return success, message


# ===========================================================================
# COMPLETE FLOW
# ===========================================================================

async def generate_and_show(
    prompt: str,
    tv_entity: str = None,
    location_id: str = None,
    send_telegram: bool = False,
    telegram_chat_id: Optional[int] = None
) -> Tuple[bool, str, Optional[str]]:
    """
    Flusso completo: genera immagine → salva → mostra su TV → (opzionale) Telegram.

    Args:
        prompt: Descrizione dell'immagine
        tv_entity: Entity ID della TV (opzionale, se None non mostra su TV)
        location_id: Location HA
        send_telegram: Se True, invia anche a Telegram
        telegram_chat_id: Chat ID per Telegram (opzionale, usa default se None)

    Returns:
        Tuple[success: bool, message: str, image_url: Optional[str]]
    """
    # 1. Genera immagine
    success, local_path, gen_message = await generate_image(prompt)
    if not success:
        return False, f"Errore generazione: {gen_message}", None

    # 2. Ottieni URL accessibile da HA
    ha_url = get_ha_accessible_url(local_path, location_id)

    # 3. Mostra su TV (se specificata)
    if tv_entity:
        tv_success, tv_message = await show_image_on_tv(ha_url, tv_entity, location_id)
        if not tv_success:
            logger.warning(f"Errore TV: {tv_message}")
            # Non fallire completamente, l'immagine è stata generata

    # 4. Opzionale: invia a Telegram
    if send_telegram:
        try:
            from integrations import send_telegram_photo
            await send_telegram_photo(local_path, caption=f"🎨 {prompt}", chat_id=telegram_chat_id)
        except Exception as e:
            logger.warning(f"Failed to send to Telegram: {e}")

    # Componi messaggio di risposta
    if tv_entity:
        response_msg = "Immagine generata e mostrata sulla TV"
    else:
        response_msg = "Immagine generata"

    if send_telegram:
        response_msg += " e inviata su Telegram"

    return True, response_msg, ha_url


# ===========================================================================
# CLEANUP
# ===========================================================================

async def cleanup_old_images(max_age_hours: int = 24):
    """Rimuove immagini più vecchie di max_age_hours."""
    import time

    try:
        cfg = _get_config()
        images_dir = Path(cfg["images_dir"])

        if not images_dir.exists():
            return

        now = time.time()
        max_age_seconds = max_age_hours * 3600
        removed_count = 0

        for filepath in images_dir.glob("*"):
            if filepath.is_file():
                try:
                    if now - filepath.stat().st_mtime > max_age_seconds:
                        filepath.unlink()
                        removed_count += 1
                        logger.debug(f"Rimossa immagine vecchia: {filepath}")
                except Exception as e:
                    logger.warning(f"Could not delete {filepath}: {e}")

        if removed_count > 0:
            logger.info(f"Cleanup: removed {removed_count} old images")

    except Exception as e:
        logger.error(f"Errore cleanup immagini: {e}")


# ===========================================================================
# UTILITY FUNCTIONS
# ===========================================================================

def extract_tv_from_room(room: str, location_id: str = None) -> Optional[str]:
    """
    Estrae entity_id della TV da una stanza usando l'entity map.

    Args:
        room: Nome della stanza (es: "soggiorno", "camera")
        location_id: ID della location

    Returns:
        Entity ID della TV o None se non trovata
    """
    from database import get_entities_by_type

    try:
        media_players = get_entities_by_type(location_id, "media_player")

        room_lower = room.lower()

        for mp in media_players:
            mp_room = mp.get("room", "").lower()
            mp_name = mp.get("name", "").lower()

            # Match per room
            if room_lower in mp_room or mp_room in room_lower:
                # Preferisci TV rispetto a speaker
                if "tv" in mp_name or "samsung" in mp_name or "lg" in mp_name or "sony" in mp_name:
                    entity_name = mp["name"].lower().replace(" ", "_")
                    return f"media_player.{entity_name}"

        # Fallback: cerca qualsiasi media_player nella stanza
        for mp in media_players:
            mp_room = mp.get("room", "").lower()
            if room_lower in mp_room or mp_room in room_lower:
                entity_name = mp["name"].lower().replace(" ", "_")
                return f"media_player.{entity_name}"

    except Exception as e:
        logger.warning(f"Could not find TV for room {room}: {e}")

    return None


def parse_image_request(text: str) -> dict:
    """
    Parsa una richiesta di generazione immagine.

    Estrae:
    - prompt: descrizione dell'immagine
    - room: stanza dove mostrare (se specificata)
    - send_telegram: se inviare anche su Telegram

    Examples:
    - "genera un'immagine di un tramonto" → prompt="un tramonto"
    - "mostrami un gatto sulla TV del salotto" → prompt="un gatto", room="salotto"
    - "crea un disegno di un cane e mandalo su Telegram" → prompt="un cane", send_telegram=True
    """
    import re

    text_lower = text.lower()

    result = {
        "prompt": "",
        "room": None,
        "send_telegram": False,
    }

    # Rileva se deve inviare su Telegram
    telegram_patterns = [
        r"(?:manda|invia|spedisci).*?(?:su|a)\s*telegram",
        r"telegram",
        r"mandamelo",
        r"inviamelo"
    ]
    for pattern in telegram_patterns:
        if re.search(pattern, text_lower):
            result["send_telegram"] = True
            break

    # Rileva stanza/TV
    room_patterns = [
        r"(?:sulla|nella|alla)\s*(?:tv|televisione)\s*(?:del|della|di|in)?\s*(\w+)",
        r"(?:tv|televisione)\s*(?:del|della|di)?\s*(\w+)",
        r"(?:nel|nella|in)\s*(\w+)",
    ]
    for pattern in room_patterns:
        match = re.search(pattern, text_lower)
        if match:
            room = match.group(1)
            # Ignora parole non-stanza
            if room not in ["telegram", "un", "una", "immagine", "foto", "disegno"]:
                result["room"] = room
                break

    # Estrai prompt (la parte descrittiva)
    # Rimuovi i pattern comuni e tieni la descrizione
    prompt = text

    remove_patterns = [
        r"^(?:jarvis\s*,?\s*)?",
        r"genera\s*(?:un['\s])?(?:immagine|foto|disegno)\s*(?:di|del|della|che\s+rappresenta)?\s*",
        r"crea\s*(?:un['\s])?(?:immagine|foto|disegno)\s*(?:di|del|della|che\s+rappresenta)?\s*",
        r"mostrami\s*(?:un['\s]|una['\s])?\s*",
        r"fai\s+vedere\s*(?:un['\s]|una['\s])?\s*",
        r"(?:e\s+)?(?:mostra(?:la|lo)?|mettila|mettilo)\s+(?:sulla|nella)\s+(?:tv|televisione).*",
        r"(?:e\s+)?(?:manda(?:la|lo)?|invia(?:la|lo)?|spedisci(?:la|lo)?)\s+(?:su|a)\s+telegram.*",
        r"(?:sulla|nella)\s+(?:tv|televisione)\s+(?:del|della|di)?\s*\w*",
        r"su\s+telegram.*",
    ]

    for pattern in remove_patterns:
        prompt = re.sub(pattern, "", prompt, flags=re.IGNORECASE)

    # Pulisci
    prompt = prompt.strip()
    prompt = re.sub(r'\s+', ' ', prompt)

    # Se il prompt è vuoto dopo la pulizia, usa il testo originale
    if not prompt or len(prompt) < 3:
        # Estrai tutto dopo "di" o "che rappresenta"
        match = re.search(r"(?:di|del|della|che\s+rappresenta)\s+(.+)", text, re.IGNORECASE)
        if match:
            prompt = match.group(1)
            # Rimuovi parti finali
            prompt = re.sub(r"(?:e\s+)?(?:mostra|manda|invia).*", "", prompt, flags=re.IGNORECASE)
            prompt = prompt.strip()

    result["prompt"] = prompt.strip()

    return result
