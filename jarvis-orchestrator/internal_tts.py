"""
Internal TTS Engine — Edge TTS + Opus streaming per speaker interno AtomS3R.

Genera audio TTS con Edge TTS (voce Microsoft it-IT-ElsaNeural),
converte in PCM 16kHz mono, codifica in frame Opus e invia al device
via WebSocket.

Il device decodifica e riproduce i frame Opus direttamente sullo speaker
integrato (ES8311 DAC + NS4150B amp).
"""

import asyncio
import io
import logging
import struct
import subprocess
from typing import Optional, Tuple

logger = logging.getLogger("JARVIS_INTERNAL_TTS")

# Opus encoder (lazy init)
_opus_encoder = None

# Costanti audio (devono matchare il firmware AtomS3R)
SAMPLE_RATE = 16000
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


async def generate_tts_audio(text: str, voice: str = "it-IT-ElsaNeural") -> Optional[bytes]:
    """
    Genera audio PCM 16kHz mono int16 da testo usando Edge TTS + ffmpeg.

    Args:
        text: Testo da sintetizzare
        voice: Voce Edge TTS (default: ElsaNeural italiana)

    Returns:
        bytes PCM raw (int16 little-endian, 16kHz mono) o None se errore
    """
    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)

        # Raccogli tutti i chunk audio MP3
        mp3_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])

        if not mp3_chunks:
            logger.error(f"Edge TTS: nessun audio generato per '{text[:50]}...'")
            return None

        mp3_data = b"".join(mp3_chunks)

        # Converti MP3 → PCM 16kHz mono int16 via ffmpeg (pipe, no file temporanei)
        pcm_data = await _mp3_to_pcm(mp3_data)
        if pcm_data:
            duration = len(pcm_data) / (SAMPLE_RATE * 2)  # 2 bytes per sample
            logger.info(f"TTS generato: {len(text)} chars → {duration:.1f}s audio ({len(pcm_data)} bytes PCM)")
        return pcm_data

    except Exception as e:
        logger.error(f"Edge TTS error: {e}", exc_info=True)
        return None


async def _mp3_to_pcm(mp3_data: bytes) -> Optional[bytes]:
    """Converte MP3 → PCM 16kHz mono int16 via ffmpeg (subprocess async)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0",
            "-f", "s16le",         # raw PCM int16
            "-acodec", "pcm_s16le",
            "-ar", str(SAMPLE_RATE),  # 16kHz
            "-ac", "1",             # mono
            "pipe:1",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=mp3_data)

        if proc.returncode != 0:
            logger.error(f"ffmpeg error (rc={proc.returncode}): {stderr.decode()[:200]}")
            return None

        return stdout

    except FileNotFoundError:
        logger.error("ffmpeg non trovato! Installare con: sudo apt install ffmpeg")
        return None
    except Exception as e:
        logger.error(f"MP3→PCM conversion error: {e}")
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


async def speak_to_device(text: str, device_id: str, voice: str = "it-IT-ElsaNeural") -> Tuple[bool, float]:
    """
    Genera TTS e invia frame Opus al device via WebSocket.

    Flusso: text → Edge TTS → MP3 → ffmpeg → PCM 16kHz → Opus frames → WS binary → Device

    Args:
        text: Testo da sintetizzare
        device_id: MAC address del device target
        voice: Voce Edge TTS

    Returns:
        (success: bool, duration_seconds: float)
    """
    from ws_audio_handler import send_tts_frame

    if not text or not text.strip():
        logger.warning("speak_to_device: testo vuoto, skip")
        return False, 0.0

    # Genera audio PCM
    pcm_data = await generate_tts_audio(text, voice=voice)
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

    # Invia frame al device via WebSocket
    sent_count = 0
    for frame in opus_frames:
        success = await send_tts_frame(device_id, frame)
        if not success:
            logger.error(f"speak_to_device: invio frame fallito dopo {sent_count}/{len(opus_frames)} frame")
            return False, 0.0
        sent_count += 1

    logger.info(f"speak_to_device({device_id}): {sent_count} frame inviati ({duration:.1f}s)")
    return True, duration
