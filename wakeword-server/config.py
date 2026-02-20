"""Configuration for jarvis-wakeword-server."""

import os
import json
import logging

logger = logging.getLogger("wakeword-server")

# --- Server ---
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8200"))

# --- Auth ---
DEVICE_API_TOKEN = os.getenv("DEVICE_API_TOKEN", "")

# --- Orchestrator VPS relay ---
ORCHESTRATOR_WS_URL = os.getenv("ORCHESTRATOR_WS_URL", "")  # ws://jarvis-orchestrator:5000/ws/audio

# --- Wake word ---
WAKEWORD_MODEL = os.getenv("WAKEWORD_MODEL", "hey_jarvis")
WAKEWORD_THRESHOLD = float(os.getenv("WAKEWORD_THRESHOLD", "0.5"))

# --- Multi-room ---
MULTIROOM_COOLDOWN_S = int(os.getenv("MULTIROOM_COOLDOWN_S", "5"))

# --- Opus ---
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_SAMPLES = 320  # 20ms @ 16 kHz
