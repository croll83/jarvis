"""
CosyVoice3 TTS Server — OpenAI-compatible drop-in replacement for Qwen3-TTS.

Endpoints:
  POST /v1/audio/speech  - OpenAI-compatible TTS
  GET  /tts/voices       - List available voices
  GET  /health           - Health check
"""

import io
import os
import re
import sys
import time
import logging
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, Response
from num2words import num2words
from pydantic import BaseModel
from typing import Optional

sys.path.insert(0, os.path.expanduser("~/cosyvoice3"))
sys.path.insert(0, os.path.expanduser("~/cosyvoice3/third_party/Matcha-TTS"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cosyvoice3-tts")

app = FastAPI(title="CosyVoice3 TTS Server")

MODEL_DIR = os.environ.get("MODEL_DIR", os.path.expanduser("~/cosyvoice3/pretrained_models/Fun-CosyVoice3-0.5B"))
REF_WAV = os.path.join(MODEL_DIR, "default_it_f.wav")
REF_TEXT = "You are a helpful assistant.<|endofprompt|>Ciao, sono Sofia, la tua assistente vocale italiana. Sono qui per aiutarti con qualsiasi cosa tu abbia bisogno."
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "sofia")
DEFAULT_SPK_ID = "it_female"

cosyvoice = None

# ---------------------------------------------------------------------------
# Italian text normalization — converts numbers, units, symbols to spoken form
# ---------------------------------------------------------------------------

_UNIT_MAP = {
    "°c": "gradi celsius", "°f": "gradi fahrenheit",
    "°": "gradi",
    "w": "watt", "kw": "chilowatt", "mw": "megawatt",
    "wh": "wattora", "kwh": "chilowattora", "mwh": "megawattora",
    "v": "volt", "kv": "chilovolt",
    "a": "ampere", "ma": "milliampere",
    "hz": "hertz", "khz": "chilohertz", "mhz": "megahertz", "ghz": "gigahertz",
    "km": "chilometri", "m": "metri", "cm": "centimetri", "mm": "millimetri",
    "km/h": "chilometri orari",
    "kg": "chilogrammi", "g": "grammi", "mg": "milligrammi",
    "l": "litri", "ml": "millilitri",
    "m²": "metri quadri", "m³": "metri cubi",
    "gb": "gigabyte", "mb": "megabyte", "kb": "chilobyte", "tb": "terabyte",
    "%": "per cento",
    "€": "euro", "$": "dollari", "£": "sterline",
}

_UNIT_PATTERN = re.compile(
    r'(\d[\d.,]*)\s*(' + '|'.join(re.escape(u) for u in sorted(_UNIT_MAP, key=len, reverse=True)) + r')(?![a-zA-Z])',
    re.IGNORECASE,
)

_TIME_PATTERN = re.compile(r'\b(\d{1,2}):(\d{2})\b')

_NUMBER_PATTERN = re.compile(r'(?<![a-zA-Z\d])(\d[\d.,]*\d|\d)(?![a-zA-Z\d:.,]\d)')


def _parse_number(s: str) -> float | int:
    """Parse a number string, handling both 1.500 (thousands) and 27.3 (decimal)."""
    s = s.strip()
    if "," in s and "." in s:
        if s.rindex(",") > s.rindex("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    val = float(s)
    if val == int(val) and "." not in s.rstrip("0").rstrip("."):
        return int(val)
    return val


def _num_to_italian(match: re.Match) -> str:
    try:
        val = _parse_number(match.group(0))
        return num2words(val, lang="it")
    except (ValueError, OverflowError):
        return match.group(0)


def normalize_italian(text: str) -> str:
    """Convert numbers, units, times to spoken Italian."""
    def _time_repl(m):
        h, mi = int(m.group(1)), int(m.group(2))
        h_w = num2words(h, lang="it")
        if mi == 0:
            return h_w
        return f"{h_w} e {num2words(mi, lang='it')}"
    text = _TIME_PATTERN.sub(_time_repl, text)

    def _unit_repl(m):
        unit_key = m.group(2).lower()
        try:
            val = _parse_number(m.group(1))
            num_word = num2words(val, lang="it")
        except (ValueError, OverflowError):
            num_word = m.group(1)
        unit_word = _UNIT_MAP.get(unit_key, m.group(2))
        return f"{num_word} {unit_word}"
    text = _UNIT_PATTERN.sub(_unit_repl, text)

    text = _NUMBER_PATTERN.sub(_num_to_italian, text)

    return text


def get_model():
    global cosyvoice
    if cosyvoice is None:
        from cosyvoice.cli.cosyvoice import CosyVoice3
        logger.info(f"Loading CosyVoice3 from {MODEL_DIR}...")
        t0 = time.time()
        cosyvoice = CosyVoice3(MODEL_DIR)
        logger.info(f"Model loaded in {time.time()-t0:.1f}s, sr={cosyvoice.sample_rate}")

        if os.path.exists(REF_WAV):
            logger.info("Registering default Italian female speaker...")
            cosyvoice.add_zero_shot_spk(REF_TEXT, REF_WAV, DEFAULT_SPK_ID)
            logger.info(f"Speakers: {cosyvoice.list_available_spks()}")
        else:
            logger.warning(f"Reference WAV not found: {REF_WAV}")
    return cosyvoice


def generate_pcm(text: str, speed: float = 1.0) -> bytes:
    """Generate raw PCM int16 24kHz mono from text."""
    m = get_model()
    text = normalize_italian(text)
    logger.info(f"Normalized: '{text[:80]}...'")
    chunks = []
    t0 = time.time()

    for output in m.inference_zero_shot(
        tts_text=text,
        prompt_text=REF_TEXT,
        prompt_wav=REF_WAV,
        zero_shot_spk_id=DEFAULT_SPK_ID,
        stream=False,
        speed=speed,
    ):
        wav = output["tts_speech"].squeeze(0).cpu().numpy()
        chunks.append(wav)

    if not chunks:
        raise HTTPException(500, "No audio generated")

    wav_full = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    elapsed = time.time() - t0
    dur = len(wav_full) / m.sample_rate
    logger.info(f"Generated {dur:.1f}s in {elapsed:.1f}s (RTF: {elapsed/dur:.2f}x) text='{text[:60]}...'")

    pcm = (wav_full * 32767).clip(-32768, 32767).astype(np.int16)
    return pcm.tobytes()


def generate_pcm_stream(text: str, speed: float = 1.0):
    """Generate raw PCM int16 24kHz mono from text, yielding chunks."""
    m = get_model()
    text = normalize_italian(text)
    logger.info(f"Normalized (stream): '{text[:80]}...'")
    t0 = time.time()
    total_samples = 0

    for output in m.inference_zero_shot(
        tts_text=text,
        prompt_text=REF_TEXT,
        prompt_wav=REF_WAV,
        zero_shot_spk_id=DEFAULT_SPK_ID,
        stream=True,
        speed=speed,
    ):
        wav = output["tts_speech"].squeeze(0).cpu().numpy()
        total_samples += len(wav)
        pcm = (wav * 32767).clip(-32768, 32767).astype(np.int16)
        yield pcm.tobytes()

    elapsed = time.time() - t0
    dur = total_samples / m.sample_rate
    logger.info(f"Streamed {dur:.1f}s in {elapsed:.1f}s (RTF: {elapsed/dur:.2f}x)")


@app.on_event("startup")
async def startup():
    get_model()


class SpeechRequest(BaseModel):
    model: str = "cosyvoice3"
    input: str
    voice: str = "sofia"
    response_format: str = "pcm"
    speed: float = 1.0
    language: Optional[str] = None
    instruct: Optional[str] = None
    stream: Optional[bool] = None


@app.post("/v1/audio/speech")
async def openai_speech(req: SpeechRequest):
    if not req.input or not req.input.strip():
        raise HTTPException(400, "Empty input text")

    logger.info(f"Request: voice={req.voice}, fmt={req.response_format}, stream={req.stream}, len={len(req.input)}")

    if req.stream:
        return StreamingResponse(
            generate_pcm_stream(req.input, req.speed),
            media_type="application/octet-stream",
        )

    pcm_bytes = generate_pcm(req.input, req.speed)

    if req.response_format == "pcm":
        return Response(content=pcm_bytes, media_type="application/octet-stream")

    wav_np = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32767.0
    buf = io.BytesIO()
    fmt_map = {"wav": "WAV", "flac": "FLAC", "ogg": "OGG"}
    sf.write(buf, wav_np, get_model().sample_rate, format=fmt_map.get(req.response_format, "WAV"))
    buf.seek(0)

    media_map = {"wav": "audio/wav", "flac": "audio/flac", "ogg": "audio/ogg"}
    return StreamingResponse(buf, media_type=media_map.get(req.response_format, "audio/wav"))


@app.get("/health")
async def health():
    m = get_model()
    return {
        "status": "ok",
        "model": "CosyVoice3-0.5B",
        "sample_rate": m.sample_rate,
        "speakers": m.list_available_spks(),
    }


@app.get("/tts/voices")
async def list_voices():
    m = get_model()
    spks = m.list_available_spks()
    profiles = [{"name": DEFAULT_VOICE, "type": "zero_shot", "speaker_id": DEFAULT_SPK_ID, "language": "Italian"}]
    return {"profiles": profiles, "clone_voices": [], "raw_speakers": spks}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9880, log_level="info")
