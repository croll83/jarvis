import aiohttp
import asyncio
import json
import logging
import re
from typing import Dict, List, Tuple, Optional, Any

import config
from database import log_event, get_default_location_id

logger = logging.getLogger("JARVIS_INTEGRATIONS")


# ===========================================================================
# HOME ASSISTANT - Multi-Location Support
# ===========================================================================
# La gestione delle connessioni HA è ora in multi_ha.py
# Qui manteniamo solo i wrapper per compatibilità

from multi_ha import multi_ha


async def call_hass_service(location_id: str, domain: str, service: str, service_data: dict) -> Tuple[bool, str]:
    """
    Wrapper principale per chiamate HA con supporto multi-location.

    Args:
        location_id: ID della location (es: "wagmi", "albani")
        domain: Dominio HA (es: "light", "switch")
        service: Servizio da chiamare (es: "turn_on", "turn_off")
        service_data: Dati del servizio (es: {"entity_id": "light.salotto"})
    """
    return await multi_ha.call_service(location_id, domain, service, service_data)


async def call_hass_service_bulk(
    location_id: str, domain: str, service: str,
    entity_ids: List[str], service_data: dict = None
) -> Tuple[bool, str]:
    """
    Wrapper per chiamate bulk HA — esegue un servizio su multiple entity in una singola chiamata.

    Args:
        location_id: ID della location
        domain: Dominio HA (es: "light")
        service: Servizio (es: "turn_off")
        entity_ids: Lista di entity_id su cui eseguire
        service_data: Parametri aggiuntivi opzionali
    """
    return await multi_ha.call_service_bulk(location_id, domain, service, entity_ids, service_data)


async def get_hass_states_bulk(
    location_id: str, entity_ids: List[str] = None
) -> Dict[str, dict]:
    """
    Wrapper per fetch bulk degli stati HA — un'unica chiamata GET /api/states.

    Args:
        location_id: ID della location
        entity_ids: Lista opzionale di entity_id da filtrare
    """
    return await multi_ha.get_states_bulk(location_id, entity_ids)


async def call_hass_service_by_name(
    location_id: str,
    domain: str,
    service: str,
    friendly_name: str,
    room: str = None,
    extra_data: dict = None
) -> Tuple[bool, str]:
    """
    Chiama un servizio HA risolvendo automaticamente friendly_name → entity_id.

    Questo è il metodo preferito per l'orchestratore: riceve il friendly name
    da Qwen e lo traduce nell'entity_id reale prima di chiamare HA.

    Args:
        location_id: ID della location
        domain: Dominio HA (light, switch, media_player, etc.)
        service: Servizio (turn_on, turn_off, etc.)
        friendly_name: Nome friendly del dispositivo (es: "Luce Soggiorno", "Echo Camera")
        room: Stanza opzionale per disambiguare
        extra_data: Dati aggiuntivi per il servizio (brightness, color, etc.)

    Returns:
        Tuple[success, message]

    Example:
        # Qwen output: {"device": "Echo Camera", "room": "Camera", "action": "speak"}
        await call_hass_service_by_name(
            "wagmi", "notify", "alexa_media",
            friendly_name="Echo Camera",
            room="Camera",
            extra_data={"message": "Ciao Marco"}
        )
        # Internamente risolve a: media_player.alexa_media_radiosveglia
    """
    from database import resolve_entity_id

    # Risolvi entity_id dal friendly name
    entity_id = resolve_entity_id(
        location_id=location_id,
        friendly_name=friendly_name,
        entity_type=domain,
        room=room
    )

    if not entity_id:
        # Fallback: prova a costruire entity_id dal nome
        # es: "Luce Soggiorno" → light.luce_soggiorno
        entity_id = f"{domain}.{friendly_name.lower().replace(' ', '_')}"
        logger.warning(f"Entity '{friendly_name}' not found in DB, using fallback: {entity_id}")

    # Costruisci service_data
    service_data = {"entity_id": entity_id}
    if extra_data:
        service_data.update(extra_data)

    logger.debug(f"Resolved '{friendly_name}' → {entity_id}")
    return await call_hass_service(location_id, domain, service, service_data)


def resolve_device_to_entity_id(
    location_id: str,
    device_name: str,
    entity_type: str = None,
    room: str = None
) -> Optional[str]:
    """
    Risolve un nome device (friendly) in entity_id.
    Funzione sincrona per uso in contesti non-async.

    Args:
        location_id: ID location
        device_name: Nome friendly (es: "TV Soggiorno", "Echo Camera")
        entity_type: Tipo entità opzionale (media_player, light, etc.)
        room: Stanza opzionale

    Returns:
        entity_id o None
    """
    from database import resolve_entity_id
    return resolve_entity_id(location_id, device_name, entity_type, room)


# ===========================================================================
# TEXT-TO-SPEECH (via Home Assistant / Alexa)
# ===========================================================================

# Speakers muted by speaker_stop (triple-tap). Maps entity_id → original_volume.
# speak() checks this and unmutes before sending TTS.
_stop_muted_speakers: Dict[str, float] = {}


async def mute_speaker_for_stop(location_id: str, entity_id: str):
    """Mute a speaker via triple-tap stop. Records original volume for restore."""
    original_volume = 0.5
    try:
        states = await get_hass_states_bulk(location_id, [entity_id])
        if states and entity_id in states:
            original_volume = states[entity_id].get("attributes", {}).get("volume_level", 0.5)
    except Exception:
        pass
    _stop_muted_speakers[entity_id] = original_volume
    # Mute via volume_mute (hardware) + volume=0 (belt & suspenders)
    await call_hass_service(location_id, "media_player", "volume_mute",
        {"entity_id": entity_id, "is_volume_muted": True})
    await call_hass_service(location_id, "media_player", "volume_set",
        {"entity_id": entity_id, "volume_level": 0})
    logger.info(f"🛑 mute_speaker_for_stop: {entity_id} muted (was vol={original_volume})")


async def unmute_speaker_from_stop(location_id: str, entity_id: str):
    """Unmute a speaker that was muted by triple-tap stop."""
    original_volume = _stop_muted_speakers.pop(entity_id, None)
    if original_volume is not None:
        await call_hass_service(location_id, "media_player", "volume_mute",
            {"entity_id": entity_id, "is_volume_muted": False})
        await call_hass_service(location_id, "media_player", "volume_set",
            {"entity_id": entity_id, "volume_level": original_volume})
        logger.info(f"🔊 unmute_speaker_from_stop: {entity_id} restored to vol={original_volume}")


async def speak(text: str, media_player_id: str, location_id: str = None) -> Tuple[bool, str]:
    """
    Utilizza l'integrazione Alexa Media Player per il TTS.

    Args:
        text: Testo da pronunciare
        media_player_id: Entity ID del media player Alexa/Echo.
        location_id: ID della location HA (None = default da DB)
    """
    location_id = location_id or get_default_location_id()
    # Se non sembra un entity_id valido, prova a risolverlo
    if media_player_id and not media_player_id.startswith("media_player."):
        resolved_id = resolve_device_to_entity_id(
            location_id=location_id,
            device_name=media_player_id,
            entity_type="media_player"
        )
        if resolved_id:
            logger.debug(f"Resolved speaker '{media_player_id}' → {resolved_id}")
            media_player_id = resolved_id
        else:
            # Fallback: costruisci entity_id dal nome
            media_player_id = f"media_player.{media_player_id.lower().replace(' ', '_')}"
            logger.warning(f"Speaker not found in DB, using fallback: {media_player_id}")

    # Auto-unmute if speaker was muted by triple-tap stop
    if media_player_id in _stop_muted_speakers:
        await unmute_speaker_from_stop(location_id, media_player_id)

    service_data = {
        "target": media_player_id,
        "data": {"type": "tts"},
        "message": text
    }
    return await call_hass_service(location_id, "notify", "alexa_media", service_data)


async def play_sound(sound_id: str, media_player_id: str, location_id: str = None) -> Tuple[bool, str]:
    """
    Riproduce un suono dalla Alexa Skills Kit Sound Library.

    Args:
        sound_id: ID del suono dalla libreria Alexa
        media_player_id: Entity ID del media player Alexa/Echo (o friendly name)
        location_id: ID della location HA (None = default da DB)
    """
    location_id = location_id or get_default_location_id()
    if not sound_id:
        return True, "No sound configured"

    # Se non sembra un entity_id valido, prova a risolverlo
    if media_player_id and not media_player_id.startswith("media_player."):
        resolved_id = resolve_device_to_entity_id(
            location_id=location_id,
            device_name=media_player_id,
            entity_type="media_player"
        )
        if resolved_id:
            media_player_id = resolved_id
        else:
            media_player_id = f"media_player.{media_player_id.lower().replace(' ', '_')}"

    # Costruisce URL soundbank - la maggior parte dei suoni UI sono in ui/gameshow/
    # ma supportiamo anche path completi se specificati
    if "/" in sound_id:
        # Path completo fornito (es: bells/bells_02)
        sound_path = sound_id
    else:
        # Solo ID fornito, assume ui/gameshow/
        sound_path = f"ui/gameshow/{sound_id}"

    ssml = f'<speak><audio src="soundbank://soundlibrary/{sound_path}"/></speak>'

    service_data = {
        "target": media_player_id,
        "data": {"type": "tts"},
        "message": ssml
    }
    return await call_hass_service(location_id, "notify", "alexa_media", service_data)


def get_sound_id(sound_type: str) -> str:
    """
    Recupera l'ID del suono dal database per il tipo specificato.

    Args:
        sound_type: "positive", "neutral", o "negative"

    Returns:
        ID del suono dalla Sound Library
    """
    from database import get_global_preference

    defaults = {
        "positive": config.SOUND_POSITIVE_DEFAULT,
        "neutral": config.SOUND_NEUTRAL_DEFAULT,
        "negative": config.SOUND_NEGATIVE_DEFAULT
    }

    return get_global_preference(f"sound_{sound_type}", defaults.get(sound_type, ""))


async def play_feedback_sound(sound_type: str, media_player_id: str, location_id: str = None) -> Tuple[bool, str]:
    """Riproduce un suono di feedback (positivo/neutrale/negativo)."""
    location_id = location_id or get_default_location_id()
    sound_id = get_sound_id(sound_type)
    return await play_sound(sound_id, media_player_id, location_id)


async def speak_with_sound(text: str, media_player_id: str, sound_type: str = None, location_id: str = None) -> Tuple[bool, str]:
    """Riproduce un suono introduttivo e poi parla."""
    location_id = location_id or get_default_location_id()
    if sound_type:
        sound_id = get_sound_id(sound_type)
        if sound_id:
            # Combina suono + testo in un unico messaggio SSML
            if "/" in sound_id:
                sound_path = sound_id
            else:
                sound_path = f"ui/gameshow/{sound_id}"

            ssml = f'<speak><audio src="soundbank://soundlibrary/{sound_path}"/>{text}</speak>'
            service_data = {
                "target": media_player_id,
                "data": {"type": "tts"},
                "message": ssml
            }
            return await call_hass_service(location_id, "notify", "alexa_media", service_data)

    # Nessun suono, solo TTS
    return await speak(text, media_player_id, location_id)


async def quick_feedback(success: bool, media_player_id: str, error_msg: str = None, location_id: str = None) -> Tuple[bool, str]:
    """
    Feedback audio rapido per comandi HA (stile Alexa).
    - Successo: suono positivo breve
    - Errore: suono negativo + messaggio vocale con l'errore
    """
    location_id = location_id or get_default_location_id()
    if success:
        return await play_feedback_sound("positive", media_player_id, location_id)
    else:
        # Per errori: suono negativo + messaggio descrittivo
        error_text = f"Non riesco: {error_msg}" if error_msg else "C'è stato un problema"
        return await speak_with_sound(error_text, media_player_id, "negative", location_id)


# ===========================================================================
# TELEGRAM (JARVIS Approval Bot)
# ===========================================================================
# The main Telegram webhook is handled by OpenClaw.
# These functions use the JARVIS Approval Bot for:
# - L3 critical action confirmations
# - System notifications and alerts
# - Photo/image delivery

def _approval_bot_api_url() -> str:
    """Costruisce la base URL per il JARVIS Approval Bot."""
    return f"https://api.telegram.org/bot{config.JARVIS_APPROVAL_BOT_TOKEN}"


async def send_telegram(text: str, parse_mode: str = "Markdown") -> Optional[dict]:
    """
    Invia un messaggio su Telegram tramite il JARVIS Approval Bot.
    Restituisce il JSON della risposta (per catturare message_id).
    """
    if not config.JARVIS_APPROVAL_BOT_TOKEN or not config.JARVIS_APPROVAL_CHAT_ID:
        logger.error("JARVIS Approval Bot not configured (missing token or chat_id)")
        return None

    url = f"{_approval_bot_api_url()}/sendMessage"
    payload = {
        "chat_id": config.JARVIS_APPROVAL_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=config.TIMEOUTS["telegram"]) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await resp.text()
                logger.error(f"Send Telegram Error: {resp.status} — {body}")
                # Retry senza parse_mode se Markdown ha causato errore 400
                if resp.status == 400 and parse_mode:
                    logger.info("Retrying Telegram send without parse_mode")
                    payload.pop("parse_mode", None)
                    async with session.post(url, json=payload, timeout=config.TIMEOUTS["telegram"]) as resp2:
                        if resp2.status == 200:
                            return await resp2.json()
                        logger.error(f"Send Telegram retry failed: {resp2.status}")
                return None
    except Exception as e:
        logger.error(f"Send Telegram Exception: {e}")
        return None


async def edit_telegram(message_id: int, text: str, parse_mode: str = "Markdown") -> bool:
    """Modifica un messaggio esistente su Telegram tramite il JARVIS Approval Bot."""
    if not config.JARVIS_APPROVAL_BOT_TOKEN or not config.JARVIS_APPROVAL_CHAT_ID:
        logger.error("JARVIS Approval Bot not configured (missing token or chat_id)")
        return False

    url = f"{_approval_bot_api_url()}/editMessageText"
    payload = {
        "chat_id": config.JARVIS_APPROVAL_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=config.TIMEOUTS["telegram"]) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"Edit Telegram Exception: {e}")
        return False


async def send_telegram_approval(text: str, action_id: str) -> bool:
    """
    Invia un messaggio con pulsanti Inline per approvazione L3 critical actions.
    Usa il JARVIS Approval Bot (separato dal Telegram webhook di OpenClaw).
    """
    if not config.JARVIS_APPROVAL_BOT_TOKEN or not config.JARVIS_APPROVAL_CHAT_ID:
        logger.error("JARVIS Approval Bot not configured (missing token or chat_id)")
        return False

    url = f"{_approval_bot_api_url()}/sendMessage"
    payload = {
        "chat_id": config.JARVIS_APPROVAL_CHAT_ID,
        "text": f"\U0001f6e1\ufe0f *JARVIS SECURITY CHECK*\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "\u2705 Esegui", "callback_data": f"confirm_{action_id}"},
                {"text": "\u274c Blocca", "callback_data": f"reject_{action_id}"}
            ]]
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=config.TIMEOUTS["telegram"]) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"Telegram Approval Exception: {e}")
        return False


async def send_exec_approval(approval_id: str, command: str, cwd: str = "", agent: str = "") -> bool:
    """
    Invia un messaggio con pulsanti Inline per approvazione exec OpenClaw.
    3 bottoni: Allow Once, Allow Always, Deny.
    """
    if not config.JARVIS_APPROVAL_BOT_TOKEN or not config.JARVIS_APPROVAL_CHAT_ID:
        logger.error("JARVIS Approval Bot not configured (missing token or chat_id)")
        return False

    # Tronca comando se troppo lungo per callback_data (max 64 bytes)
    slug = approval_id[:8] if len(approval_id) > 8 else approval_id
    display_cmd = command if len(command) <= 200 else command[:200] + "..."

    text = (
        f"\U0001f510 *EXEC APPROVAL*\n\n"
        f"`{display_cmd}`\n"
    )
    if cwd:
        text += f"\n\U0001f4c2 `{cwd}`"
    if agent:
        text += f"\n\U0001f916 Agent: {agent}"

    url = f"{_approval_bot_api_url()}/sendMessage"
    payload = {
        "chat_id": config.JARVIS_APPROVAL_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "\u2705 Once", "callback_data": f"execonce_{slug}"},
                {"text": "\U0001f513 Always", "callback_data": f"execalways_{slug}"},
                {"text": "\u274c Deny", "callback_data": f"execdeny_{slug}"}
            ]]
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=config.TIMEOUTS["telegram"]) as resp:
                if resp.status == 200:
                    logger.info(f"Exec approval sent for {slug}: {command[:50]}")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Exec approval send error: {resp.status} - {error}")
                    return False
    except Exception as e:
        logger.error(f"Exec Approval Exception: {e}")
        return False


# ===========================================================================
# AUDIO PREPROCESSING (RNNoise)
# ===========================================================================

async def denoise_audio(audio_bytes: bytes) -> bytes:
    """
    Applica noise reduction all'audio usando pyrnnoise.

    pyrnnoise lavora a 48kHz internamente. Il nostro audio è PCM 16kHz int16 mono.
    Strategia: resample 16k→48k con filtro anti-aliasing, denoise, resample 48k→16k.
    Se pyrnnoise non è disponibile, restituisce l'audio originale (graceful fallback).
    """
    try:
        import numpy as np
        from pyrnnoise import RNNoise
        from scipy.signal import resample_poly

        denoiser = RNNoise(sample_rate=48000)

        # PCM int16 mono 16kHz → numpy float64 normalizzato
        audio_16k = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float64) / 32768.0

        # Resample 16kHz → 48kHz con filtro anti-aliasing (up=3, down=1)
        audio_48k = resample_poly(audio_16k, up=3, down=1).astype(np.float32)

        # pyrnnoise vuole shape [1, num_samples] per mono (channels, samples)
        # e valori int16 range (pyrnnoise lavora internamente in int16)
        audio_48k_int16 = (audio_48k * 32767.0).clip(-32768, 32767).astype(np.int16)
        audio_48k_2d = audio_48k_int16.reshape(1, -1)

        # Denoise in chunks — raccoglie l'output
        denoised_chunks = []
        for _speech_prob, denoised in denoiser.denoise_chunk(audio_48k_2d):
            denoised_chunks.append(denoised)

        if not denoised_chunks:
            logger.warning("RNNoise returned no chunks, returning original audio")
            return audio_bytes

        # Concatena e torna a mono 1D float64 normalizzato
        denoised_48k = np.concatenate(denoised_chunks, axis=-1).flatten()
        if denoised_48k.dtype == np.int16:
            denoised_48k = denoised_48k.astype(np.float64) / 32768.0
        elif denoised_48k.dtype != np.float64:
            denoised_48k = denoised_48k.astype(np.float64)

        # Resample 48kHz → 16kHz con filtro anti-aliasing (up=1, down=3)
        denoised_16k = resample_poly(denoised_48k, up=1, down=3)

        # Assicurati che la lunghezza sia compatibile con l'originale
        orig_len = len(audio_bytes) // 2  # int16 = 2 bytes per sample
        if len(denoised_16k) > orig_len:
            denoised_16k = denoised_16k[:orig_len]
        elif len(denoised_16k) < orig_len:
            denoised_16k = np.pad(denoised_16k, (0, orig_len - len(denoised_16k)))

        # Converti a int16 con clipping esplicito
        result = (denoised_16k * 32767.0).clip(-32768, 32767).astype(np.int16)
        logger.info(f"RNNoise denoised {len(audio_bytes)} bytes of audio (proper resampling)")
        return result.tobytes()

    except ImportError:
        logger.warning("RNNoise not available (pyrnnoise not installed), returning original audio")
        return audio_bytes
    except Exception as e:
        logger.error(f"Denoise error: {e}")
        return audio_bytes


# ===========================================================================
# SPEECH-TO-TEXT (faster-whisper locale o Groq API)
# ===========================================================================

def _clean_stt_text(text: str) -> str:
    """
    Pulizia leggera dell'output Whisper: rimuove punteggiatura spuria
    che Whisper può inserire copiando lo stile del initial_prompt.
    Es. "accendi, luce, box" → "accendi luce box"
    NON tocca apostrofi (l'ingresso) né trattini composti (Strip Led).
    """
    if not text:
        return text
    # Rimuove virgole e punti e virgola isolati tra parole
    # (mantiene quelli in numeri tipo "3.14" o "1,5")
    cleaned = re.sub(r'(?<=[a-zA-ZàèéìòùÀÈÉÌÒÙ])\s*[,;]\s*(?=[a-zA-ZàèéìòùÀÈÉÌÒÙ])', ' ', text)
    # Rimuove trattini em/en usati come separatori di lista (— / –)
    cleaned = re.sub(r'\s*[—–]\s*', ' ', cleaned)
    # Rimuove prefissi tipo "1/" "2/" che Whisper può copiare dal prompt
    cleaned = re.sub(r'\b\d+[/\\]\s*', '', cleaned)
    # Collassa spazi multipli
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    if cleaned != text:
        logger.debug(f"STT cleanup: '{text}' → '{cleaned}'")
    return cleaned


async def transcribe_audio(audio_bytes: bytes) -> Optional[str]:
    """
    Trascrive audio usando faster-whisper locale o Groq API.
    Il backend è determinato da config.AI_BACKEND.
    Applica pulizia punteggiatura post-trascrizione.
    """
    if config.AI_BACKEND == "api":
        text = await _transcribe_groq(audio_bytes)
    else:
        text = await _transcribe_local(audio_bytes)
    return _clean_stt_text(text) if text else text


async def _transcribe_local(audio_bytes: bytes) -> Optional[str]:
    """Trascrizione via faster-whisper locale (speaches)."""
    try:
        # Wrap raw PCM in WAV header se non ha già un RIFF header
        if not audio_bytes[:4] == b'RIFF':
            audio_bytes = _wrap_pcm_as_wav(audio_bytes)

        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field('file', audio_bytes,
                          filename='audio.wav',
                          content_type='audio/wav')
            data.add_field('model', config.WHISPER_MODEL)
            data.add_field('language', config.WHISPER_LANGUAGE)
            if config.WHISPER_PROMPT:
                data.add_field('initial_prompt', config.WHISPER_PROMPT)

            async with session.post(config.WHISPER_TRANSCRIBE_URL,
                                   data=data, timeout=config.TIMEOUTS["whisper"]) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result.get("text", "").strip()
                else:
                    logger.error(f"Whisper error: {resp.status}")
                    return None

    except Exception as e:
        logger.error(f"Transcribe exception: {e}")
        return None


async def send_telegram_photo(
    photo_path: str,
    caption: str = "",
    chat_id: int = None,
    parse_mode: str = "Markdown"
) -> Optional[dict]:
    """
    Invia una foto a Telegram tramite il JARVIS Approval Bot.

    Args:
        photo_path: Path locale del file immagine
        caption: Didascalia opzionale
        chat_id: Chat ID destinatario (default: config.JARVIS_APPROVAL_CHAT_ID)
        parse_mode: Formato parsing ("Markdown" o "HTML")

    Returns:
        JSON della risposta Telegram o None in caso di errore
    """
    import os

    if not config.JARVIS_APPROVAL_BOT_TOKEN:
        logger.error("JARVIS Approval Bot token not configured")
        return None

    url = f"{_approval_bot_api_url()}/sendPhoto"
    target_chat_id = chat_id or config.JARVIS_APPROVAL_CHAT_ID

    if not target_chat_id:
        logger.error("No Telegram chat_id configured")
        return None

    if not os.path.exists(photo_path):
        logger.error(f"Photo file not found: {photo_path}")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field('chat_id', str(target_chat_id))

            # Leggi e invia il file
            with open(photo_path, 'rb') as photo_file:
                data.add_field('photo', photo_file, filename=os.path.basename(photo_path))

                if caption:
                    data.add_field('caption', caption)
                    data.add_field('parse_mode', parse_mode)

                async with session.post(url, data=data, timeout=config.TIMEOUTS["telegram_photo"]) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        logger.info(f"Photo sent to Telegram chat {target_chat_id}")
                        return result
                    else:
                        error = await resp.text()
                        logger.error(f"Send Telegram Photo Error: {resp.status} - {error}")
                        return None

    except Exception as e:
        logger.error(f"Send Telegram Photo Exception: {e}")
        return None


def _wrap_pcm_as_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    """Aggiunge un WAV header a raw PCM int16 bytes."""
    import struct
    data_size = len(pcm_bytes)
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits,
        b'data', data_size
    )
    return header + pcm_bytes


async def _transcribe_groq(audio_bytes: bytes) -> Optional[str]:
    """
    Trascrizione via Groq API (whisper-large-v3-turbo).
    Più veloce e accurato di whisper-base locale.
    """
    if not config.GROQ_API_KEY:
        logger.error("GROQ_API_KEY not configured, falling back to local")
        return await _transcribe_local(audio_bytes)

    # Wrap raw PCM in WAV header se non ha già un RIFF header
    if not audio_bytes[:4] == b'RIFF':
        audio_bytes = _wrap_pcm_as_wav(audio_bytes)

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}

            data = aiohttp.FormData()
            data.add_field('file', audio_bytes,
                          filename='audio.wav',
                          content_type='audio/wav')
            data.add_field('model', config.GROQ_WHISPER_MODEL)
            data.add_field('language', 'it')
            data.add_field('response_format', 'json')
            if config.WHISPER_PROMPT:
                data.add_field('prompt', config.WHISPER_PROMPT)

            url = f"{config.GROQ_API_URL}/audio/transcriptions"

            async with session.post(url, headers=headers, data=data,
                                   timeout=config.API_TIMEOUT_STT) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    text = result.get("text", "").strip()
                    logger.debug(f"Groq STT result: {text[:50]}...")
                    return text
                else:
                    error = await resp.text()
                    logger.error(f"Groq STT error: {resp.status} - {error}")
                    # Fallback to local if Groq fails
                    logger.warning("Falling back to local Whisper")
                    return await _transcribe_local(audio_bytes)

    except asyncio.TimeoutError:
        logger.error(f"Groq STT timeout after {config.API_TIMEOUT_STT}s")
        return await _transcribe_local(audio_bytes)
    except Exception as e:
        logger.error(f"Groq transcribe exception: {e}")
        return await _transcribe_local(audio_bytes)
