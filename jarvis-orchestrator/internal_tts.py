"""
Internal TTS Engine — Kokoro TTS + Opus streaming per speaker interno AtomS3R.

Genera audio TTS con Kokoro-82M in modalita streaming (voce if_sara, italiano).
La generazione audio e la riproduzione si sovrappongono: i primi frame Opus
vengono inviati al device mentre il resto dell'audio e ancora in generazione.
Questo riduce drasticamente il time-to-first-audio, specialmente su CPU.

Flusso streaming:
  text -> Kokoro HTTP (chunked) -> PCM 24kHz chunks -> resample 16kHz -> Opus -> WS -> Device
                                   ^^^^^ overlap con riproduzione ^^^^^
"""

import asyncio
import logging
import time
from typing import Optional, Tuple

import aiohttp
import numpy as np
from scipy.signal import resample_poly

import config as _cfg

logger = logging.getLogger("JARVIS_INTERNAL_TTS")

# ---------------------------------------------------------------------------
# TTS Text Preprocessing — LLM normalizza testo per pronuncia italiana
# ---------------------------------------------------------------------------
_TTS_PREPROCESS_PROMPT = (
    "Sei un preprocessore di testo per un motore TTS italiano (Kokoro). "
    "Il tuo compito è preparare il testo per una lettura naturale ad alta voce.\n\n"
    "Regole:\n"
    "1. Parole inglesi comuni: translittera in fonetica italiana "
    "(es. \"trading\" → \"tréiding\", \"monitor\" → \"mònitor\", \"budget\" → \"bàdget\", "
    "\"server\" → \"sèrver\", \"router\" → \"ràuter\", \"file\" → \"fàil\")\n"
    "2. Numeri: scrivi in lettere (es. \"3\" → \"tre\", \"2024\" → \"duemilaventiquattro\")\n"
    "3. Abbreviazioni: espandi (es. \"ecc.\" → \"eccetera\", \"km\" → \"chilometri\")\n"
    "4. Accenti tonici ambigui: aggiungi accento grafico dove la pronuncia potrebbe "
    "essere sbagliata (es. \"monitora\" → \"monìtora\", \"subito\" → \"sùbito\")\n"
    "5. NON cambiare il significato, la struttura o il contenuto della frase\n"
    "6. NON aggiungere spiegazioni, rispondi SOLO con il testo trasformato"
)

# Opus encoder (lazy init)
_opus_encoder = None

# Costanti audio (devono matchare il firmware AtomS3R)
SAMPLE_RATE = 16000
KOKORO_SAMPLE_RATE = 24000
OPUS_FRAME_SAMPLES = 320  # 20ms @ 16kHz
OPUS_BITRATE = 30000
OPUS_COMPLEXITY = 5

# Streaming: accumula almeno N bytes di PCM 24kHz prima di resamplare.
# 4800 bytes = 2400 samples = 0.1s @ 24kHz. Abbastanza per resample pulito,
# piccolo abbastanza per bassa latenza.
_STREAM_MIN_BYTES = 4800


def _get_opus_encoder():
    """Lazy init dell'encoder Opus."""
    global _opus_encoder
    if _opus_encoder is None:
        import opuslib
        _opus_encoder = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
        _opus_encoder.bitrate = OPUS_BITRATE
        _opus_encoder.complexity = OPUS_COMPLEXITY
        logger.info(f"Opus encoder: {SAMPLE_RATE}Hz, {OPUS_BITRATE}bps, complexity={OPUS_COMPLEXITY}")
    return _opus_encoder


def _resample_24k_to_16k(pcm_24k: bytes) -> bytes:
    """Resample PCM int16 da 24kHz a 16kHz."""
    samples = np.frombuffer(pcm_24k, dtype=np.int16).astype(np.float32)
    resampled = resample_poly(samples, up=2, down=3)  # 24000 * 2/3 = 16000
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


async def _preprocess_tts_text(text: str) -> str:
    """Pre-processa testo per TTS via LLM (Qwen). Ritorna testo originale se fallisce."""
    if not _cfg.TTS_PREPROCESS_ENABLED or len(text) < 10:
        return text

    t0 = time.monotonic()
    try:
        if _cfg.AI_BACKEND == "api" and _cfg.OPENROUTER_API_KEY:
            # Cloud: OpenRouter
            headers = {
                "Authorization": f"Bearer {_cfg.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": _cfg.OPENROUTER_REFERER,
                "X-Title": _cfg.OPENROUTER_TITLE,
            }
            payload = {
                "model": _cfg.OPENROUTER_ROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": _TTS_PREPROCESS_PROMPT},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.2,
                "max_tokens": 500,
            }
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{_cfg.OPENROUTER_API_URL}/chat/completions",
                    headers=headers, json=payload,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        content = result["choices"][0]["message"]["content"].strip()
                        if content:
                            elapsed = time.monotonic() - t0
                            logger.info(f"TTS preprocess ({elapsed:.2f}s): '{text[:40]}...' -> '{content[:40]}...'")
                            return content
        else:
            # Local: Ollama
            payload = {
                "model": _cfg.ROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": _TTS_PREPROCESS_PROMPT},
                    {"role": "user", "content": text},
                ],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 500},
            }
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    _cfg.OLLAMA_CHAT_URL, json=payload, timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = data.get("message", {}).get("content", "").strip()
                        if content:
                            elapsed = time.monotonic() - t0
                            logger.info(f"TTS preprocess ({elapsed:.2f}s): '{text[:40]}...' -> '{content[:40]}...'")
                            return content
    except Exception as e:
        logger.warning(f"TTS preprocess failed, using original: {e}")

    return text


async def generate_tts_audio(text: str) -> Optional[bytes]:
    """
    Genera audio PCM 16kHz mono int16 da testo (non-streaming, full buffer).

    Returns:
        bytes PCM raw (int16 little-endian, 16kHz mono) o None se errore
    """
    try:
        url = f"{_cfg.KOKORO_TTS_URL}/v1/audio/speech"
        payload = {
            "model": "kokoro",
            "voice": _cfg.KOKORO_TTS_VOICE,
            "input": text,
            "response_format": "pcm",
        }

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Kokoro TTS error (HTTP {resp.status}): {body[:200]}")
                    return None
                pcm_24k = await resp.read()

        if not pcm_24k:
            logger.error(f"Kokoro TTS: nessun audio per '{text[:50]}...'")
            return None

        pcm_data = _resample_24k_to_16k(pcm_24k)
        duration = len(pcm_data) / (SAMPLE_RATE * 2)
        logger.info(f"TTS: {len(text)} chars -> {duration:.1f}s ({len(pcm_data)} bytes PCM)")
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
    """Codifica PCM int16 in frame Opus (320 samples/frame, 20ms)."""
    encoder = _get_opus_encoder()
    frame_size_bytes = OPUS_FRAME_SAMPLES * 2  # 640 bytes
    frames = []

    for offset in range(0, len(pcm_data), frame_size_bytes):
        chunk = pcm_data[offset:offset + frame_size_bytes]
        if len(chunk) < frame_size_bytes:
            chunk += b'\x00' * (frame_size_bytes - len(chunk))
        frames.append(encoder.encode(chunk, OPUS_FRAME_SAMPLES))

    return frames


async def speak_to_device(text: str, device_id: str) -> Tuple[bool, float]:
    """
    Genera TTS in streaming e invia frame Opus al device in tempo reale.

    La generazione Kokoro e la riproduzione si sovrappongono: i primi frame
    Opus vengono inviati al device mentre il resto dell'audio e ancora in
    generazione sul server TTS. Riduce il time-to-first-audio da decine
    di secondi (CPU) a pochi secondi.

    Returns:
        (success: bool, duration_seconds: float)
    """
    from ws_audio_handler import send_tts_frame

    if not text or not text.strip():
        logger.warning("speak_to_device: testo vuoto, skip")
        return False, 0.0

    # Pre-processing LLM: normalizza numeri, translittera inglese, accenti
    text = await _preprocess_tts_text(text)

    url = f"{_cfg.KOKORO_TTS_URL}/v1/audio/speech"
    payload = {
        "model": "kokoro",
        "voice": _cfg.KOKORO_TTS_VOICE,
        "input": text,
        "response_format": "pcm",
        "stream": True,
    }

    encoder = _get_opus_encoder()
    frame_bytes = OPUS_FRAME_SAMPLES * 2  # 640 bytes per Opus frame

    BURST_FRAMES = 5     # 100ms di pre-buffer iniziale
    FRAME_INTERVAL = 0.018  # ~18ms (sotto 20ms per evitare underrun)

    pcm_24k_buf = b""   # Buffer 24kHz per allineamento resample
    opus_buf = b""       # Buffer 16kHz per allineamento frame Opus
    total_16k_bytes = 0
    frame_idx = 0
    t_start = time.monotonic()
    first_frame_logged = False

    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Kokoro stream error (HTTP {resp.status}): {body[:200]}")
                    return False, 0.0

                async for http_chunk in resp.content.iter_any():
                    pcm_24k_buf += http_chunk

                    # Processa quando abbiamo abbastanza dati per resample pulito
                    while len(pcm_24k_buf) >= _STREAM_MIN_BYTES:
                        # Allinea a 6 byte (3 samples@24kHz -> 2 samples@16kHz)
                        take = (len(pcm_24k_buf) // 6) * 6
                        if take < 6:
                            break
                        pcm_24k = pcm_24k_buf[:take]
                        pcm_24k_buf = pcm_24k_buf[take:]

                        pcm_16k = _resample_24k_to_16k(pcm_24k)
                        total_16k_bytes += len(pcm_16k)
                        opus_buf += pcm_16k

                        # Encode + invia frame Opus con pacing
                        while len(opus_buf) >= frame_bytes:
                            frame_data = opus_buf[:frame_bytes]
                            opus_buf = opus_buf[frame_bytes:]
                            opus_frame = encoder.encode(frame_data, OPUS_FRAME_SAMPLES)
                            if not await send_tts_frame(device_id, opus_frame):
                                logger.error(f"speak_to_device: send failed at frame {frame_idx}")
                                return False, 0.0
                            frame_idx += 1
                            if not first_frame_logged:
                                ttfa = time.monotonic() - t_start
                                logger.info(f"speak_to_device({device_id}): first frame at {ttfa:.2f}s")
                                first_frame_logged = True
                            if frame_idx > BURST_FRAMES:
                                await asyncio.sleep(FRAME_INTERVAL)

        # Flush buffer residuo 24kHz
        if pcm_24k_buf:
            rem = len(pcm_24k_buf) % 6
            if rem:
                pcm_24k_buf += b'\x00' * (6 - rem)
            if pcm_24k_buf:
                pcm_16k = _resample_24k_to_16k(pcm_24k_buf)
                total_16k_bytes += len(pcm_16k)
                opus_buf += pcm_16k

        # Flush buffer residuo Opus (ultimo frame con padding silenzio)
        if opus_buf:
            if len(opus_buf) < frame_bytes:
                opus_buf += b'\x00' * (frame_bytes - len(opus_buf))
            while len(opus_buf) >= frame_bytes:
                frame_data = opus_buf[:frame_bytes]
                opus_buf = opus_buf[frame_bytes:]
                opus_frame = encoder.encode(frame_data, OPUS_FRAME_SAMPLES)
                if not await send_tts_frame(device_id, opus_frame):
                    return False, 0.0
                frame_idx += 1
                if frame_idx > BURST_FRAMES:
                    await asyncio.sleep(FRAME_INTERVAL)

        duration = total_16k_bytes / (SAMPLE_RATE * 2)
        elapsed = time.monotonic() - t_start
        logger.info(f"speak_to_device({device_id}): {frame_idx} frames streamed, "
                     f"{duration:.1f}s audio in {elapsed:.1f}s wall")
        return True, duration

    except asyncio.TimeoutError:
        logger.error("speak_to_device: Kokoro timeout")
        return False, 0.0
    except aiohttp.ClientError as e:
        logger.error(f"speak_to_device: connection error: {e}")
        return False, 0.0
    except Exception as e:
        logger.error(f"speak_to_device: error: {e}", exc_info=True)
        return False, 0.0
