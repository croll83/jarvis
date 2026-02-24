"""Configuration for jarvis-wakeword-server."""

import os
import json
import logging
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("wakeword-server")

# --- Server ---
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8200"))

# --- Auth ---
DEVICE_API_TOKEN = os.getenv("DEVICE_API_TOKEN", "")

# --- Orchestrator VPS relay ---
ORCHESTRATOR_WS_URL = os.getenv("ORCHESTRATOR_WS_URL", "")  # ws://jarvis-orchestrator:5000/ws/audio


def _derive_orchestrator_url() -> str:
    """Derive HTTP base URL from WebSocket URL if ORCHESTRATOR_URL not set."""
    ws_url = ORCHESTRATOR_WS_URL
    if not ws_url:
        return ""
    # ws:// -> http://, wss:// -> https://
    http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")
    # Strip path (e.g. /ws/audio) — keep only scheme + host:port
    parsed = urlparse(http_url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


# --- Orchestrator HTTP base URL (for proxied management calls) ---
# If not set, derived from ORCHESTRATOR_WS_URL by replacing ws:// with http://
# and stripping the path component.
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "") or _derive_orchestrator_url()

# --- Wake word ---
WAKEWORD_MODEL = os.getenv("WAKEWORD_MODEL", "hey_jarvis")
WAKEWORD_THRESHOLD = float(os.getenv("WAKEWORD_THRESHOLD", "0.5"))

# --- Multi-room ---
MULTIROOM_COOLDOWN_S = int(os.getenv("MULTIROOM_COOLDOWN_S", "5"))

# --- Opus ---
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_SAMPLES = 320  # 20ms @ 16 kHz
