"""
Internal TTS Engine — Kokoro TTS + Opus streaming per speaker interno AtomS3R.

Genera audio TTS con Kokoro-82M (voce if_sara, italiano),
riceve PCM 24kHz, resampla a 16kHz, codifica in frame Opus e invia al device
via WebSocket.

Il device decodifica e riproduce i frame Opus direttamente sullo speaker
integrato (ES8311 DAC + NS4150B amp).
"""

import asyncio
import logging
from typing import Optional, Tuple

import aiohttp
import numpy as np
from scipy.signal import resample_poly

from config import KOKORO_TTS_URL, KOKORO_TTS_VOICE

logger = logging.getLogger("JARVIS_INTERNAL_TTS")

# Opus encoder (lazy init)
_opus_encoder = None

# Costanti audio (devono matchare il firmware AtomS3R)
SAMPLE_RATE = 16000
KOKORO_SAMPLE_RATE = 24000
OPUS_FRAME_SAMPLES = 320  # 20ms @ 16kHz
OPUS_BITRATE = 30000
OPUS_COMPLEXITY = 5  # server-side, possiamo permetterci più qualità


def _get_opus_encoder():
    """Lazy init dell'encoder Opus."""
    global _opus_encoder
    if _opus_encoder is None:
        import opuslib
        _opus_encoder = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
        _opus_encoder.bitrate = OPUS_BITRATE
        _opus_encoder.complexity = OPUS_COMPLEXITY
        logger.info(f"Opus encoder inizializzato: {SAMPLE_RATE}Hz, {OPUS_BITRATE}bps, complexity={OPUS_COMPLEXITY}")
    return _opus_encoder


def _resample_24k_to_16k(pcm_24k: bytes) -> bytes:
    """Resample PCM int16 da 24kHz a 16kHz."""
    samples = np.frombuffer(pcm_24k, dtype=np.int16).astype(np.float32)
    resampled = resample_poly(samples, up=2, down=3)  # 24000 * 2/3 = 16000
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


async def generate_tts_audio(text: str) -> Optional[bytes]:
    """
    Genera audio PCM 16kHz mono int16 da testo usando Kokoro TTS.

    Args:
        text: Testo da sintetizzare

    Returns:
        bytes PCM raw (int16 little-endian, 16kHz mono) o None se errore
    """
    try:
        url = f"{KOKORO_TTS_URL}/v1/audio/speech"
        payload = {
            "model": "kokoro",
            "voice": KOKORO_TTS_VOICE,
            "input": text,
            "response_format": "pcm",
        }

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Kokoro TTS error (HTTP {resp.status}): {body[:200]}")
                    return None
                pcm_24k = await resp.read()

        if not pcm_24k:
            logger.error(f"Kokoro TTS: nessun audio generato per '{text[:50]}...'")
            return None

        # Resample 24kHz → 16kHz
        pcm_data = _resample_24k_to_16k(pcm_24k)
        duration = len(pcm_data) / (SAMPLE_RATE * 2)  # 2 bytes per sample
        logger.info(f"TTS generato: {len(text)} chars -> {duration:.1f}s audio ({len(pcm_data)} bytes PCM)")
        return pcm_data

    except asyncio.TimeoutError:
        logger.error("Kokoro TTS timeout")
        return None
    except aiohttp.ClientError as e:
        logger.error(f"Kokoro TTS connection error: {e}")
        return None
    except Exception as e:
        logger.error(f"Kokoro TTS error: {e}", exc_info=True)
        return None


def pcm_to_opus_frames(pcm_data: bytes) -> list[bytes]:
    """
    Codifica PCM int16 in una lista di frame Opus (320 samples ciascuno).

    Args:
        pcm_data: bytes PCM raw (int16 little-endian, 16kHz mono)

    Returns:
        Lista di bytes Opus encoded (uno per frame da 20ms)
    """
    encoder = _get_opus_encoder()
    frame_size_bytes = OPUS_FRAME_SAMPLES * 2  # 640 bytes per frame (int16)
    frames = []

    for offset in range(0, len(pcm_data), frame_size_bytes):
        chunk = pcm_data[offset:offset + frame_size_bytes]

        # Padding dell'ultimo frame con silenzio se incompleto
        if len(chunk) < frame_size_bytes:
            chunk = chunk + b'\x00' * (frame_size_bytes - len(chunk))

        opus_frame = encoder.encode(chunk, OPUS_FRAME_SAMPLES)
        frames.append(opus_frame)

    return frames


async def speak_to_device(text: str, device_id: str) -> Tuple[bool, float]:
    """
    Genera TTS e invia frame Opus al device via WebSocket.

    Flusso: text -> Kokoro TTS -> PCM 24kHz -> resample 16kHz -> Opus frames -> WS binary -> Device

    Args:
        text: Testo da sintetizzare
        device_id: MAC address del device target

    Returns:
        (success: bool, duration_seconds: float)
    """
    from ws_audio_handler import send_tts_frame

    if not text or not text.strip():
        logger.warning("speak_to_device: testo vuoto, skip")
        return False, 0.0

    # Genera audio PCM
    pcm_data = await generate_tts_audio(text)
    if pcm_data is None:
        logger.error(f"speak_to_device: TTS generation failed per '{text[:50]}...'")
        return False, 0.0

    # Codifica in frame Opus
    opus_frames = pcm_to_opus_frames(pcm_data)
    if not opus_frames:
        logger.error("speak_to_device: nessun frame Opus generato")
        return False, 0.0

    duration = len(pcm_data) / (SAMPLE_RATE * 2)
    logger.info(f"speak_to_device({device_id}): invio {len(opus_frames)} frame Opus ({duration:.1f}s)")

    # Invia frame al device via WebSocket con pacing ~real-time.
    # Ogni frame = 20ms di audio. Senza pacing, centinaia di frame vengono
    # sparati a raffica saturando il buffer TCP dell'ESP32 -> connection reset.
    # Invio i primi BURST_FRAMES senza delay (pre-buffer), poi paco a ~18ms/frame.
    BURST_FRAMES = 5  # 100ms di pre-buffer iniziale
    FRAME_INTERVAL = 0.018  # ~18ms (leggermente sotto 20ms per evitare underrun)

    sent_count = 0
    for i, frame in enumerate(opus_frames):
        success = await send_tts_frame(device_id, frame)
        if not success:
            logger.error(f"speak_to_device: invio frame fallito dopo {sent_count}/{len(opus_frames)} frame")
            return False, 0.0
        sent_count += 1
        # Pacing: burst iniziale poi ~real-time
        if i >= BURST_FRAMES:
            await asyncio.sleep(FRAME_INTERVAL)

    logger.info(f"speak_to_device({device_id}): {sent_count} frame inviati ({duration:.1f}s)")
    return True, duration
