"""
Internal TTS Engine — Qwen3-TTS/XTTSv2/Kokoro + Opus streaming per speaker interno voice devices.

Supporta tre engine TTS selezionabili via TTS_ENGINE in config:
  - "qwen3tts" (Qwen3-TTS su GX10, GPU ~4.4 GiB): deploy locale, voice cloning, voci IT/EN
  - "xtts"     (XTTSv2 Coqui, DEPRECATO):          ex deploy locale Atomman
  - "kokoro"   (Kokoro-82M, CPU/GPU ~0.5 GB):       deploy cloud / VPS

La generazione audio e la riproduzione si sovrappongono: i primi frame Opus
vengono inviati al device mentre il resto dell'audio e ancora in generazione.
Questo riduce drasticamente il time-to-first-audio.

Flusso streaming (tutti gli engine producono PCM 24kHz):
  text -> TTS HTTP (chunked) -> PCM 24kHz chunks -> resample 16kHz -> Opus -> WS -> Device
                                ^^^^^ overlap con riproduzione ^^^^^
"""

import asyncio
import logging
import re
import time
from typing import Optional, Tuple

import aiohttp
import numpy as np
from num2words import num2words as _num2words
from scipy.signal import resample_poly

import config as _cfg

logger = logging.getLogger("JARVIS_INTERNAL_TTS")

# Opus encoder (lazy init)
_opus_encoder = None

# Costanti audio (devono matchare il firmware dei voice devices)
SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000   # Sia XTTS che Kokoro producono 24kHz
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


def _strip_wav_header(data: bytes) -> bytes:
    """Estrae PCM raw da un file WAV. Se non e WAV, restituisce i dati invariati."""
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return data
    # Cerca il subchunk 'data'
    pos = 12
    while pos < len(data) - 8:
        chunk_id = data[pos:pos + 4]
        chunk_size = int.from_bytes(data[pos + 4:pos + 8], 'little')
        if chunk_id == b'data':
            return data[pos + 8:]
        pos += 8 + chunk_size
    return data


# ---------------------------------------------------------------------------
# TTS Text Preprocessing — regole deterministiche, zero latenza
# ---------------------------------------------------------------------------

# Parole inglesi comuni → fonetica italiana per Kokoro G2P
_EN_TO_IT_PHONETIC: dict[str, str] = {
    "trading": "tréiding",
    "budget": "bàdget",
    "server": "sèrver",
    "router": "ràuter",
    "file": "fàil",
    "monitor": "mònitor",
    "computer": "compiùter",
    "software": "sòftuer",
    "hardware": "àrduer",
    "network": "nètuork",
    "cloud": "clàud",
    "smart": "smàrt",
    "smartphone": "smartfòn",
    "display": "displèi",
    "update": "apdèit",
    "download": "dàunlod",
    "upload": "àplod",
    "online": "onlàin",
    "offline": "oflàin",
    "website": "uèbsait",
    "email": "imèil",
    "password": "pàssuord",
    "token": "tòken",
    "wallet": "uòllet",
    "staking": "stèiking",
    "yield": "ìild",
    "balance": "bàlans",
    "blockchain": "blòkcein",
    "exchange": "excèingg",
    "market": "màrket",
    "bullish": "bùllish",
    "bearish": "bèrish",
    "rally": "ràlli",
    "pump": "pàmp",
    "dump": "dàmp",
    "spread": "sprèd",
    "futures": "fiùcers",
    "leverage": "lèverig",
    "default": "difòlt",
    "privacy": "prìvasi",
    "feedback": "fìdbek",
    "startup": "stàrtap",
    "machine learning": "machìn lèrning",
    "deep learning": "dìp lèrning",
    "home assistant": "hòm assìstent",
}

# Abbreviazioni/sigle → espansione italiana
_ABBREVIATIONS: dict[str, str] = {
    "ecc.": "eccetera",
    "etc.": "eccetera",
    "km": "chilometri",
    "kg": "chilogrammi",
    "mb": "megabàit",
    "gb": "gigabàit",
    "tb": "terabàit",
    "cpu": "si pi ù",
    "gpu": "gi pi ù",
    "ram": "ràm",
    "api": "ei pi ài",
    "url": "u erre èlle",
    "usb": "u esse bì",
    "wifi": "uài fài",
    "btc": "bitcòin",
    "eth": "etèreum",
    "usdc": "u esse di sì",
    "usdt": "u esse di tì",
    "nft": "enne effe tì",
    "ai": "ei ài",
}

# Pattern regex compilati
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_ITALIC = re.compile(r"(?<!\*)\*(.+?)\*(?!\*)")
_RE_CODE = re.compile(r"`(.+?)`")
_RE_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_BULLET = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_RE_NUMBERED = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)
_RE_LINK = re.compile(r"\[(.+?)\]\(.+?\)")
_RE_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\b")
# Emoji: qualsiasi codepoint nelle aree emoji Unicode
_RE_EMOJI = re.compile(
    "[\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"    # symbols & pictographs
    "\U0001F680-\U0001F6FF"    # transport & map
    "\U0001F1E0-\U0001F1FF"    # flags
    "\U00002702-\U000027B0"    # dingbats
    "\U0000FE00-\U0000FE0F"    # variation selectors
    "\U0000200D"               # ZWJ
    "\U00002640-\U00002642"    # gender symbols
    "\U000023CF-\U000023F3"    # misc technical
    "\U0001FA00-\U0001FA6F"    # chess/extended-A
    "\U0001FA70-\U0001FAFF"    # extended-B
    "\U00002600-\U000026FF"    # misc symbols
    "\U0001F900-\U0001F9FF"    # supplemental
    "]+", flags=re.UNICODE
)
# Punti esclamativi/interrogativi ripetuti
_RE_REPEATED_PUNCT = re.compile(r"([!?]){2,}")


def _strip_markdown(text: str) -> str:
    """Rimuove formattazione markdown preservando il contenuto."""
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC.sub(r"\1", text)
    text = _RE_CODE.sub(r"\1", text)
    text = _RE_HEADER.sub("", text)
    text = _RE_BULLET.sub("", text)
    text = _RE_NUMBERED.sub("", text)
    text = _RE_LINK.sub(r"\1", text)
    return text


def _clean_for_speech(text: str) -> str:
    """Pulisce testo da elementi che XTTS/Kokoro leggono male: emoji, punti, ecc.

    XTTS legge '.' come 'punto' letteralmente — va rimosso ovunque tranne
    che nei numeri decimali (es. 86.79, 1.500).
    """
    # Rimuovi emoji (XTTS le legge come "punto" o sbarella)
    text = _RE_EMOJI.sub("", text)
    # Ellissi → virgola (pausa naturale)
    text = text.replace("...", ",")
    text = text.replace("…", ",")
    # Punti esclamativi/interrogativi ripetuti → singolo (!!!! → !)
    text = _RE_REPEATED_PUNCT.sub(r"\1", text)
    # Rimuovi TUTTI i punti tranne quelli nei numeri (es. 86.79, 1.500.000).
    # Punto tra cifre = decimale/migliaia → preserva.
    # Punto a fine frase o dopo parola → rimuovi (XTTS lo legge come "punto").
    text = re.sub(r"(?<!\d)\.(?!\d)", "", text)
    # Pulisci spazi multipli risultanti
    text = re.sub(r"  +", " ", text).strip()
    return text


def _transliterate_english(text: str) -> str:
    """Sostituisce parole inglesi comuni con fonetica italiana."""
    # Multi-word prima (es. "machine learning")
    for eng, ita in _EN_TO_IT_PHONETIC.items():
        if " " in eng:
            text = re.sub(re.escape(eng), ita, text, flags=re.IGNORECASE)
    # Single word: match solo parole intere
    for eng, ita in _EN_TO_IT_PHONETIC.items():
        if " " not in eng:
            text = re.sub(rf"\b{re.escape(eng)}\b", ita, text, flags=re.IGNORECASE)
    return text


def _expand_abbreviations(text: str) -> str:
    """Espande abbreviazioni e sigle comuni."""
    for abbr, expansion in _ABBREVIATIONS.items():
        text = re.sub(rf"\b{re.escape(abbr)}\b", expansion, text, flags=re.IGNORECASE)
    return text


def _numbers_to_words(text: str) -> str:
    """Converte numeri in parole italiane."""
    def _replace_number(match: re.Match) -> str:
        raw = match.group(0)
        try:
            # Gestisci decimali con punto o virgola
            if "." in raw and "," not in raw:
                # Potrebbe essere decimale (86.79) o migliaia (1.000)
                parts = raw.split(".")
                if len(parts) == 2 and len(parts[1]) <= 2:
                    # Decimale: "86.79" -> "ottantasei punto settantanove"
                    int_part = _num2words(int(parts[0]), lang="it")
                    dec_part = _num2words(int(parts[1]), lang="it")
                    return f"{int_part} punto {dec_part}"
            if "," in raw:
                # Decimale italiano: "86,79"
                parts = raw.split(",")
                if len(parts) == 2:
                    int_part = _num2words(int(parts[0]), lang="it")
                    dec_part = _num2words(int(parts[1]), lang="it")
                    return f"{int_part} virgola {dec_part}"
            return _num2words(int(raw), lang="it")
        except (ValueError, OverflowError):
            return raw

    return _RE_NUMBER.sub(_replace_number, text)


def _preprocess_tts_text(text: str) -> str:
    """Pre-processa testo per TTS con regole deterministiche (zero latenza)."""
    if not _cfg.TTS_PREPROCESS_ENABLED:
        return text

    original = text
    # _strip_markdown: disabilitato — OpenClaw già strippa markdown prima di inviare
    # text = _strip_markdown(text)
    text = _clean_for_speech(text)
    # _expand_abbreviations: disabilitato — troppo prone a errori (false positive)
    # text = _expand_abbreviations(text)
    text = _transliterate_english(text)
    # _numbers_to_words: disabilitato — XTTS legge i numeri molto meglio di EdgeTTS
    # text = _numbers_to_words(text)
    # Pulisci spazi multipli
    text = re.sub(r"  +", " ", text).strip()

    if text != original:
        logger.debug(f"TTS preprocess: '{original[:60]}' -> '{text[:60]}'")
    return text


# ---------------------------------------------------------------------------
# TTS Text Preprocessing — LLM (opzionale, disabilitato di default)
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


async def _preprocess_tts_text_llm(text: str) -> str:
    """Pre-processa testo per TTS via LLM. Disabilitato di default (TTS_PREPROCESS_LLM=true)."""
    if not _cfg.TTS_PREPROCESS_LLM or len(text) < 10:
        return text

    t0 = time.monotonic()
    try:
        if _cfg.AI_BACKEND == "api" and _cfg.OPENROUTER_API_KEY:
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
                            logger.info(f"TTS LLM preprocess ({elapsed:.2f}s): '{text[:40]}...' -> '{content[:40]}...'")
                            return content
        else:
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
                            logger.info(f"TTS LLM preprocess ({elapsed:.2f}s): '{text[:40]}...' -> '{content[:40]}...'")
                            return content
    except Exception as e:
        logger.warning(f"TTS LLM preprocess failed, using original: {e}")

    return text


def _build_tts_request(engine: str, text: str, stream: bool = False) -> tuple:
    """Costruisce URL e payload per il TTS engine selezionato."""
    if engine == "qwen3tts":
        url = f"{_cfg.QWEN3_TTS_URL}/v1/audio/speech"
        payload = {
            "model": "qwen3-tts",
            "voice": _cfg.QWEN3_TTS_VOICE,
            "input": text,
            "response_format": "pcm",
        }
        if stream:
            payload["stream"] = True
    elif engine == "xtts":
        url = f"{_cfg.XTTS_URL}/tts_to_audio/"
        payload = {
            "text": text,
            "speaker_wav": _cfg.XTTS_SPEAKER,
            "language": _cfg.XTTS_LANGUAGE,
        }
    else:  # kokoro
        url = f"{_cfg.KOKORO_TTS_URL}/v1/audio/speech"
        payload = {
            "model": "kokoro",
            "voice": _cfg.KOKORO_TTS_VOICE,
            "input": text,
            "response_format": "pcm",
        }
        if stream:
            payload["stream"] = True
    return url, payload


async def generate_tts_audio(text: str) -> Optional[bytes]:
    """
    Genera audio PCM 16kHz mono int16 da testo (non-streaming, full buffer).

    Returns:
        bytes PCM raw (int16 little-endian, 16kHz mono) o None se errore
    """
    engine = _cfg.TTS_ENGINE
    try:
        url, payload = _build_tts_request(engine, text)

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"TTS ({engine}) error (HTTP {resp.status}): {body[:200]}")
                    return None
                raw_data = await resp.read()

        if not raw_data:
            logger.error(f"TTS ({engine}): nessun audio per '{text[:50]}...'")
            return None

        # XTTS restituisce WAV, Qwen3-TTS e Kokoro restituiscono PCM raw
        pcm_24k = _strip_wav_header(raw_data) if engine == "xtts" else raw_data

        pcm_data = _resample_24k_to_16k(pcm_24k)
        duration = len(pcm_data) / (SAMPLE_RATE * 2)
        logger.info(f"TTS ({engine}): {len(text)} chars -> {duration:.1f}s ({len(pcm_data)} bytes PCM)")
        return pcm_data

    except asyncio.TimeoutError:
        logger.error(f"TTS ({engine}) timeout")
        return None
    except aiohttp.ClientError as e:
        logger.error(f"TTS ({engine}) connection error: {e}")
        return None
    except Exception as e:
        logger.error(f"TTS ({engine}) error: {e}", exc_info=True)
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

    La generazione TTS e la riproduzione si sovrappongono: i primi frame
    Opus vengono inviati al device mentre il resto dell'audio e ancora in
    generazione sul server TTS. Riduce il time-to-first-audio.

    Supporta Qwen3-TTS (GX10), XTTSv2 (deprecato) e Kokoro (cloud) tramite TTS_ENGINE config.

    Returns:
        (success: bool, duration_seconds: float)
    """
    from ws_audio_handler import send_tts_frame, notify_tts_start

    if not text or not text.strip():
        logger.warning("speak_to_device: testo vuoto, skip")
        return False, 0.0

    engine = _cfg.TTS_ENGINE

    # Pre-processing deterministico: markdown, numeri, translittering inglese
    text = _preprocess_tts_text(text)
    # Pre-processing LLM opzionale (disabilitato di default)
    text = await _preprocess_tts_text_llm(text)

    # Costruisci URL e payload in base all'engine
    url, payload = _build_tts_request(engine, text, stream=True)

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
    wav_header_checked = False

    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"TTS stream ({engine}) error (HTTP {resp.status}): {body[:200]}")
                    return False, 0.0

                async for http_chunk in resp.content.iter_any():
                    # XTTS: il primo chunk potrebbe avere un header WAV, strippalo
                    if engine == "xtts" and not wav_header_checked:
                        http_chunk = _strip_wav_header(http_chunk)
                        wav_header_checked = True

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
                            # Send tts_start before the very first frame
                            if frame_idx == 0:
                                if not await notify_tts_start(device_id):
                                    logger.error(f"speak_to_device: tts_start failed for {device_id}")
                                    return False, 0.0
                            if not await send_tts_frame(device_id, opus_frame):
                                logger.error(f"speak_to_device: send failed at frame {frame_idx}")
                                return False, 0.0
                            frame_idx += 1
                            if not first_frame_logged:
                                ttfa = time.monotonic() - t_start
                                logger.info(f"speak_to_device({device_id}, {engine}): first frame at {ttfa:.2f}s")
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
        logger.info(f"speak_to_device({device_id}, {engine}): {frame_idx} frames streamed, "
                     f"{duration:.1f}s audio in {elapsed:.1f}s wall")
        return True, duration

    except asyncio.TimeoutError:
        logger.error(f"speak_to_device: TTS ({engine}) timeout")
        return False, 0.0
    except aiohttp.ClientError as e:
        logger.error(f"speak_to_device: TTS ({engine}) connection error: {e}")
        return False, 0.0
    except Exception as e:
        logger.error(f"speak_to_device: TTS ({engine}) error: {e}", exc_info=True)
        return False, 0.0
