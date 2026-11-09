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

# AGC software pre-wakeword (il mic AtomS3R è molto basso):
# picco target, gain massimo e floor sotto il quale il chunk è solo rumore.
WAKEWORD_AGC_TARGET = float(os.getenv("WAKEWORD_AGC_TARGET", "24000"))
WAKEWORD_AGC_MAX = float(os.getenv("WAKEWORD_AGC_MAX", "20"))
WAKEWORD_AGC_FLOOR = int(os.getenv("WAKEWORD_AGC_FLOOR", "80"))

# --- Multi-room ---
MULTIROOM_COOLDOWN_S = int(os.getenv("MULTIROOM_COOLDOWN_S", "5"))

# --- Location ---
# This wakeword-server serves a single home/LAN. The firmware does NOT send
# location_id on management calls (e.g. /room_temperature), so the orchestrator
# would otherwise search all locations and return the wrong home's data.
# When set, it is injected into proxied management requests that need it.
LOCATION_ID = os.getenv("LOCATION_ID", "")

# --- Voice Live Hub (sessione live full-duplex, Fase 2) ---
# Con HUB_PORT<=0 oppure HUB_HOST vuoto la funzione e' spenta: un live_session_start "hub"
# dell'orchestrator viene rifiutato con live_session_end reason "hub_unreachable" e tutto
# resta come oggi. L'indirizzo del hub per gli Atom viene solo da qui, mai dal messaggio.
HUB_HOST = os.getenv("HUB_HOST", "").strip()
HUB_PORT = int(os.getenv("HUB_PORT", "0") or "0")
# Cancellazione d'eco per l'Atom in sessione hub: "webrtc" (AEC3), "speex" oppure "off".
AEC_BACKEND = os.getenv("AEC_BACKEND", "webrtc").strip().lower()
# VAD di Gemini chiesto all'hub per le sessioni degli Atom: lo speaker e' a pochi mm dal mic e
# all'inizio di ogni frase un breve residuo d'eco passa l'AEC; con la sensibilita' di default
# Gemini lo prendeva per l'utente che interrompe. Vuoto = default dell'hub.
HUB_VAD_START_SENSITIVITY = os.getenv("HUB_VAD_START_SENSITIVITY", "START_SENSITIVITY_LOW").strip()
HUB_VAD_PREFIX_PADDING_MS = int(os.getenv("HUB_VAD_PREFIX_PADDING_MS", "300") or "0")
# Guadagno digitale del mic dell'Atom (ADC scale ES8311, 0-42 dB a passi di 6) durante la sessione
# live con riferimento dal device: a +30 dB (default del firmware) l'eco dello speaker satura
# l'ADC e la parte saturata non si cancella. Vuoto = guadagno del firmware.
HUB_ATOM_MIC_GAIN_DB = os.getenv("HUB_ATOM_MIC_GAIN_DB", "").strip()
# Cosa chiedere ai firmware con la cap "aec_ref": "level" (mic Opus + livello dello speaker,
# il server chiude il canale verso Gemini mentre l'Atom suona) oppure "pcm" (mic e loopback in
# PCM per l'AEC, ~512 kbit/s: sul WiFi dell'Atom faceva cadere la sessione). "level" ignora
# HUB_ATOM_MIC_GAIN_DB: l'eco non arriva comunque a Gemini.
HUB_AEC_REF_MODE = os.getenv("HUB_AEC_REF_MODE", "level").strip().lower()
# Guadagno fisso sul mic dei device con l'AEC a bordo (NabuVoice): in sessione live l'XMOS lavora
# senza AGC, che rialzava il residuo d'eco nei silenzi, e la voce a un metro arriva a ~-60 dBFS.
# Un guadagno fisso alza voce e residuo insieme, senza pompare nei silenzi. 0 = nessuno.
HUB_ONBOARD_GAIN_DB = float(os.getenv("HUB_ONBOARD_GAIN_DB", "24") or "0")

def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- Pre-roll della wake word (Fase 3, design 19 e 19.12) ---
# Ring PCM per device, riempito in IDLE subito dopo il decode Opus (anche durante il cooldown
# multiroom): al rilevamento se ne prende la coda, che finisce sul rilevamento e contiene la
# wake word. Serve all'archivio delle clip, al voice ID in shadow, al cancello a voce e al
# live_start.
WAKE_PREROLL_S = float(os.getenv("WAKE_PREROLL_S", "2.0") or "2.0")
# Shadow: a ogni rilevamento il pre-roll va all'orchestrator (wake_preroll) che ci calcola il
# voice ID e logga solo i punteggi. Non cambia niente per il device. Acceso di default.
WAKE_PREROLL_SHADOW = _env_bool("WAKE_PREROLL_SHADOW", "1")
# Archivio delle clip di ogni rilevamento (WAV 16 kHz, nome con istante UTC, device e
# punteggio), per riaddestrare il modello con i falsi positivi. Limitato per numero e byte:
# oltre si cancellano le piu' vecchie. Acceso di default.
WAKE_CLIP_ARCHIVE = _env_bool("WAKE_CLIP_ARCHIVE", "1")
WAKE_CLIP_DIR = os.getenv("WAKE_CLIP_DIR", "/app/data/wakeclips").strip()
WAKE_CLIP_MAX_FILES = int(os.getenv("WAKE_CLIP_MAX_FILES", "3000") or "0")
WAKE_CLIP_MAX_MB = float(os.getenv("WAKE_CLIP_MAX_MB", "200") or "0")
# Cancello a voce: prima del wake_detected si aspetta il verdetto dell'orchestrator (wake_gate)
# sul voice ID del pre-roll; "drop" = wake ignorata in silenzio. "auto" (default) segue il flag
# wake_voice_gate del welcome dell'orchestrator (spento di default la'), "1" sempre, "0" mai.
# Senza verdetto entro il timeout la wake passa (il cancello non deve rendere sordo il device).
WAKE_VOICE_GATE = os.getenv("WAKE_VOICE_GATE", "auto").strip().lower() or "auto"
WAKE_VOICE_GATE_TIMEOUT_S = float(os.getenv("WAKE_VOICE_GATE_TIMEOUT_S", "1.0") or "1.0")

# --- Dalla wake word direttamente a Gemini Live (design 19) ---
# Interruttore di emergenza (acceso: tutti i device sono abilitati); serve anche live_on_wake
# nel welcome dell'orchestrator e, per le wake rilevate qui, la cap "live_wake" del firmware.
# Spento = comportamento di oggi.
LIVE_ON_WAKE = _env_bool("LIVE_ON_WAKE", "1")
# Attesa dell'ack del firmware a wake_detected{live} prima di ripiegare sul classico
LIVE_ACK_TIMEOUT_S = float(os.getenv("LIVE_ACK_TIMEOUT_S", "1.0") or "1.0")
# Attesa della risposta dell'orchestrator a live_start (poi live_cancel e classico)
LIVE_START_TIMEOUT_S = float(os.getenv("LIVE_START_TIMEOUT_S", "2.5") or "2.5")
# Accept tardivo del hub per le sessioni con origine wake o tap (contesto + provider.open)
LIVE_ACCEPT_TIMEOUT_S = float(os.getenv("LIVE_ACCEPT_TIMEOUT_S", "7.0") or "7.0")
# Buffer del comando detto tutto d'un fiato (dalla fine della wake word, o dal tap, fino
# all'accept del hub) e VAD a energia che decide offer.command
LIVE_COMMAND_MAX_S = float(os.getenv("LIVE_COMMAND_MAX_S", "8") or "8")
LIVE_COMMAND_MIN_SPEECH_MS = int(os.getenv("LIVE_COMMAND_MIN_SPEECH_MS", "300") or "300")
LIVE_COMMAND_MARGIN_DB = float(os.getenv("LIVE_COMMAND_MARGIN_DB", "10") or "10")
LIVE_COMMAND_MIN_DBFS = float(os.getenv("LIVE_COMMAND_MIN_DBFS", "-65") or "-65")
# Il chirp dell'Atom suona subito dopo l'ack "ok": quei ms non contano come parlato nel VAD
# (l'audio resta nel buffer che va al hub)
LIVE_COMMAND_CHIRP_SKIP_MS = int(os.getenv("LIVE_COMMAND_CHIRP_SKIP_MS", "250") or "0")

# --- Opus ---
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_SAMPLES = 320  # 20ms @ 16 kHz
