"""
JARVIS Configuration File

NOTA: Questo file contiene solo parametri che richiedono un riavvio per essere applicati.
I parametri modificabili a runtime sono nel database (global_preferences).

Parametri in global_preferences (modificabili da dashboard):
- dnd_mode: Modalità non disturbare
- silent_hour_start/end: Orari silenzio notturno
- confidence_threshold_high/low: Soglie AI
- ha_quick_feedback: Feedback audio rapido
- sound_positive/neutral/negative: Suoni Alexa
- security_suppression_time: Tempo soppressione allarmi
- approval_timeout: Timeout approvazioni

Parametri per-location (in tabella locations):
- keywords: Parole chiave per identificare la location
- hass_url, hass_token: Connessione Home Assistant
- has_security: Se la location ha sistema di sicurezza

Parametri entity_maps (per-location):
- Camere (internal/perimetral) basate sulla zona nell'entity map
"""

import os
from pathlib import Path

# ===========================================================================
# DIRECTORY PATHS
# ===========================================================================
BASE_DIR = Path(__file__).resolve().parent
SYSTEM_RULES_PATH = BASE_DIR / "config/router_system_prompt.txt"

# ===========================================================================
# NETWORK & API ENDPOINTS (richiedono riavvio)
# ===========================================================================
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
WHISPER_URL = os.getenv("WHISPER_URL", "http://localhost:9000")

# Endpoint derivati
OLLAMA_CHAT_URL = f"{OLLAMA_URL}/api/chat"
OLLAMA_GENERATE_URL = f"{OLLAMA_URL}/api/generate"
WHISPER_TRANSCRIBE_URL = f"{WHISPER_URL}/v1/audio/transcriptions"

# Home Assistant - Configurazione base di fallback
# NOTA: URL e Token per le location sono nel database (tabella locations)
HASS_URL_DEFAULT = os.getenv("HASS_URL", "http://homeassistant:8123")
DEFAULT_HASS_PORT = 8123

# Tailscale (location remote via VPN)
TAILSCALE_PATTERNS = ["100.", ".ts.net"]
TAILSCALE_TIMEOUT_REMOTE = float(os.getenv("TAILSCALE_TIMEOUT_REMOTE", "15.0"))
TAILSCALE_TIMEOUT_LOCAL = float(os.getenv("TAILSCALE_TIMEOUT_LOCAL", "10.0"))

# Voice device sources (canale voce) — tutti i tipi di device vocali supportati
VOICE_SOURCES = {"AtomS3R", "NabuVoice", "VirtualMic"}

# Device API Token (autenticazione voice device firmware)
# Se vuoto, l'autenticazione device è disabilitata
DEVICE_API_TOKEN = os.getenv("DEVICE_API_TOKEN", "")

# Wakeword Server URLs (one per location/casa, JSON map)
# Format: {"location_id": "http://192.168.1.X:8200", ...}
# Used to push wake_word_sensitivity and trigger_listen to LXC wakeword-servers
WAKEWORD_SERVER_URLS_RAW = os.getenv("WAKEWORD_SERVER_URLS", "")
WAKEWORD_SERVER_URLS: dict = {}
if WAKEWORD_SERVER_URLS_RAW:
    try:
        import json
        WAKEWORD_SERVER_URLS = json.loads(WAKEWORD_SERVER_URLS_RAW)
    except Exception:
        pass

# OpenClaw (AI Brain - Gemini 3 Pro)
OPENCLAW_URL = os.getenv("OPENCLAW_URL", "https://openclaw.mintwork.it:18789")
OPENCLAW_TOKEN = os.getenv("OPENCLAW_TOKEN", "")
OPENCLAW_TIMEOUT = int(os.getenv("OPENCLAW_TIMEOUT", "30"))  # legacy (non-streaming fallback)
OPENCLAW_TIMEOUT_TOTAL = int(os.getenv("OPENCLAW_TIMEOUT_TOTAL", "300"))  # Max totale streaming SSE (5 min)
OPENCLAW_TIMEOUT_READ = int(os.getenv("OPENCLAW_TIMEOUT_READ", "90"))     # Max silenzio tra chunk SSE

# OpenClaw Gateway WebSocket (operator mode for exec approvals)
# Connect as operator to receive exec.approval.requested events and resolve them.
OPENCLAW_WS_URL = os.getenv("OPENCLAW_WS_URL", "wss://openclaw.mintwork.it:18789")
OPENCLAW_EXEC_APPROVAL_TIMEOUT = int(os.getenv("OPENCLAW_EXEC_APPROVAL_TIMEOUT", "120"))

# JARVIS Approval Bot (L3 critical actions - separate from OpenClaw Telegram)
JARVIS_APPROVAL_BOT_TOKEN = os.getenv("JARVIS_APPROVAL_BOT_TOKEN", "")
JARVIS_APPROVAL_CHAT_ID = os.getenv("JARVIS_APPROVAL_CHAT_ID", "")

# Webhook URL for Telegram bot (public HTTPS URL)
# If set, orchestrator registers webhook on startup instead of using getUpdates polling.
# Example: https://jarvis-pub.mintwork.it/telegram_webhook
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")

# ===========================================================================
# MODEL NAMES (richiedono che i modelli siano caricati in Ollama)
# ===========================================================================
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "qwen2.5:3b")

# ===========================================================================
# WEB TOOLS (Brave Search API per tool calling Qwen)
# ===========================================================================
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "BSA8KS4JcRxAtVndf_sF8bn_ZRddL6a")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# Context window per tool calling (più ampio del routing)
QWEN_TOOLS_NUM_CTX = int(os.getenv("QWEN_TOOLS_NUM_CTX", "16384"))

# ===========================================================================
# MEMORY CONTEXT LIMITS (potenzialmente modificabili a runtime ma meno critici)
# ===========================================================================
# Limiti per il ROUTER (Qwen) - deve essere veloce, meno contesto
ROUTER_MEMORY_HIGH_PRIORITY = 10      # Messaggi speaker corrente
ROUTER_MEMORY_MEDIUM_PRIORITY = 5     # Messaggi altri familiari
ROUTER_MEMORY_GLOBAL = 3              # Eventi sistema

# Sliding window temporale (secondi)
MEMORY_WINDOW_SECONDS = 3600          # 1 ora di default

# ===========================================================================
# STRATIFIED MEMORY (hot/warm/cold/longterm)
# ===========================================================================
# User memory - finestre temporali per COMPACTOR (cleanup/purge jobs)
COMPACTOR_HOT_MINUTES = 30               # Messaggi raw recenti
COMPACTOR_WARM_HOURS = 24                # Summaries orari
COMPACTOR_COLD_DAYS = 7                  # Summaries giornalieri
COMPACTOR_HOURLY_MAX_AGE_HOURS = 48      # Pulizia summaries orari
COMPACTOR_DAILY_MAX_AGE_DAYS = 30        # Pulizia summaries giornalieri

# User memory - finestre temporali per REASONING (context builder retrieval)
# Queste sono by design più ampie delle finestre compactor
REASONING_WARM_HOURS = 48                # Finestra warm per reasoning
REASONING_COLD_DAYS = 14                 # Finestra cold per reasoning
ROUTING_WARM_HOURS = 24                  # Finestra warm per routing
ROUTING_COLD_DAYS = 7                    # Finestra cold per routing

# Budget token per context builder (routing vs reasoning)
# Usati da context_builder.py per allocare contesto SQL + Vector
CONTEXT_BUDGETS = {
    "routing": {
        "user_memory_sql": int(os.getenv("CTX_ROUTING_USER_SQL", "300")),
        "user_memory_vector": int(os.getenv("CTX_ROUTING_USER_VECTOR", "200")),
        "location_memory": int(os.getenv("CTX_ROUTING_LOCATION", "300")),
        "total_target": int(os.getenv("CTX_ROUTING_TOTAL", "900")),
    },
    "reasoning": {
        "user_memory_sql": int(os.getenv("CTX_REASONING_USER_SQL", "800")),
        "user_memory_vector": int(os.getenv("CTX_REASONING_USER_VECTOR", "600")),
        "location_memory": int(os.getenv("CTX_REASONING_LOCATION", "800")),
        "total_target": int(os.getenv("CTX_REASONING_TOTAL", "5000")),
    },
}


# ===========================================================================
# SECURITY: ACTION CATEGORIZATION (modificare con cautela)
# ===========================================================================
# Azioni che richiedono SEMPRE approvazione Telegram
ALWAYS_CONFIRM_ACTIONS = {
    "lock": ["lock", "unlock"],
    "cover": ["open_cover", "close_cover"],
    "alarm_control_panel": ["*"],
    "delete": ["*"],
    "remove": ["*"],
    "cancel": ["*"],
    "payment": ["*"],
    "transfer": ["*"],
    "purchase": ["*"],
    "email": ["send"],
    "calendar": ["delete", "cancel"],
}

# Azioni auto-approvate (basso rischio)
AUTO_APPROVE_ACTIONS = {
    "light": ["turn_on", "turn_off", "toggle"],
    "sensor": ["read"],
    "search": ["web", "email_read"],
    "switch": ["turn_on", "turn_off"],
}

# Domini HA critici (richiedono sempre approvazione se da canale non trusted)
CRITICAL_DOMAINS = ["cover", "lock", "alarm_control_panel"]

# ===========================================================================
# DEFAULTS per backward compatibility
# Questi sono usati come fallback se il DB non ha il valore
# ===========================================================================
SILENT_START = 23
SILENT_END = 7
CONFIDENCE_THRESHOLD_HIGH = 0.85
CONFIDENCE_THRESHOLD_LOW = 0.50
SECURITY_SUPPRESSION_TIME = 600
APPROVAL_TIMEOUT = 3600

# Session
SESSION_DURATION_SECONDS = int(os.getenv("SESSION_DURATION_DAYS", "30")) * 86400

# Default location (fallback per global_preference "default_location_id")
DEFAULT_LOCATION_ID = os.getenv("DEFAULT_LOCATION_ID", "wagmi")

# Default fallback speaker (usato se entity_map non ha media_player per la stanza)
DEFAULT_FALLBACK_SPEAKER = os.getenv("DEFAULT_FALLBACK_SPEAKER", "media_player.echo_salotto")

# Speaker per annunci security
SECURITY_ANNOUNCEMENT_SPEAKER = os.getenv("SECURITY_ANNOUNCEMENT_SPEAKER", "media_player.bose_salotto")

# Audio feedback defaults
HA_QUICK_FEEDBACK_DEFAULT = True
SOUND_POSITIVE_DEFAULT = "amzn_ui_sfx_gameshow_positive_response_01"
SOUND_NEUTRAL_DEFAULT = "amzn_ui_sfx_gameshow_neutral_response_03"
SOUND_NEGATIVE_DEFAULT = "amzn_ui_sfx_gameshow_negative_response_01"

# ===========================================================================
# AI BACKEND CONFIGURATION
# ===========================================================================
# Backend mode: "local" (Ollama/Whisper) or "api" (Groq/OpenRouter)
# Set via environment variable for easy switching between local and cloud deploy
AI_BACKEND = os.getenv("AI_BACKEND", "local")

# --- API Keys ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# --- API Endpoints ---
GROQ_API_URL = "https://api.groq.com/openai/v1"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1"
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "https://jarvis.yourdomain.com")
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "JARVIS Home Assistant")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta"

# --- API Model Names ---
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"
OPENROUTER_ROUTER_MODEL = os.getenv("OPENROUTER_ROUTER_MODEL", "qwen/qwen-2.5-7b-instruct")
GEMINI_REASONING_MODEL = "gemini-2.0-flash"

# --- API Timeouts (seconds) ---
API_TIMEOUT_STT = int(os.getenv("API_TIMEOUT_STT", "30"))
API_TIMEOUT_ROUTING = int(os.getenv("API_TIMEOUT_ROUTING", "30"))

# --- Feature Flags ---
# Gemini is available via OpenClaw as the brain
GEMINI_ENABLED = bool(GEMINI_API_KEY)

# ===========================================================================
# GEMINI IMAGE GENERATION
# ===========================================================================
# Uses the same GEMINI_API_KEY as above
# Model for image generation (Gemini 3 Pro with image output)
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.0-flash-exp-image-generation")

# Directory where generated images are saved
# Must be accessible via HA's /local/ URL (mapped to /config/www/)
GENERATED_IMAGES_DIR = os.getenv("GENERATED_IMAGES_DIR", "/app/data/www/generated")

# Auto-cleanup images older than this (hours)
IMAGE_CLEANUP_HOURS = int(os.getenv("IMAGE_CLEANUP_HOURS", "24"))

# ===========================================================================
# MEDIA CAST (TV display via Samsung TV Smart)
# ===========================================================================
CAST_DIR = os.getenv("CAST_DIR", "/app/data/www/cast")
CAST_MAX_FILE_SIZE = int(os.getenv("CAST_MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100 MB
CAST_DEFAULT_IMAGE_DURATION = int(os.getenv("CAST_DEFAULT_IMAGE_DURATION", "30"))   # seconds
CAST_FILE_TTL = int(os.getenv("CAST_FILE_TTL", "7200"))                            # 2 hours

# ===========================================================================
# HINT CONFIDENCE (pre-routing keyword matching)
# ===========================================================================
HINT_CONFIDENCE = {
    "IMAGE_GENERATION": 0.95,
}

# ===========================================================================
# LLM PARAMETERS (defaults, overrideable via global_preferences)
# ===========================================================================
# Questi valori sono i default usati se il DB non ha override.
# Modificabili a runtime dalla dashboard admin via global_preferences.
# Chiave DB: "llm_params" → JSON con stessa struttura
LLM_PARAMS = {
    "routing": {
        "temperature": 0.1,
        "max_tokens": 500,
        "timeout": 10,
    },
    "quick_response": {
        "temperature": 0.7,
        "max_tokens": 200,
        "timeout": 10,
    },
    "summary": {
        "temperature": 0.3,
        "max_tokens": 150,
        "timeout": 15,
    },
    "gemini": {
        "temperature": 0.7,
        "max_tokens": 2000,
    },
}

# ===========================================================================
# TIMEOUTS (seconds)
# ===========================================================================
TIMEOUTS = {
    "telegram": int(os.getenv("TIMEOUT_TELEGRAM", "5")),
    "whisper": int(os.getenv("TIMEOUT_WHISPER", "30")),
    "ha_read": int(os.getenv("TIMEOUT_HA_READ", "5")),
    "health_check": int(os.getenv("TIMEOUT_HEALTH_CHECK", "5")),
    "embedding": int(os.getenv("TIMEOUT_EMBEDDING", "30")),
    "openclaw": int(os.getenv("TIMEOUT_OPENCLAW", "30")),
    "sqlite": int(os.getenv("TIMEOUT_SQLITE", "30")),
    "location_memory": int(os.getenv("TIMEOUT_LOCATION_MEMORY", "5")),
    "telegram_photo": int(os.getenv("TIMEOUT_TELEGRAM_PHOTO", "30")),
}

# ===========================================================================
# INTERVALS (seconds)
# ===========================================================================
INTERVALS = {
    "cleanup": int(os.getenv("INTERVAL_CLEANUP", "1800")),
    "health_check": int(os.getenv("INTERVAL_HEALTH_CHECK", "30")),
    "proactive_check": int(os.getenv("INTERVAL_PROACTIVE", "300")),
    "proactive_cooldown": int(os.getenv("INTERVAL_PROACTIVE_COOLDOWN", "1800")),
    "telegram_stream_update": float(os.getenv("INTERVAL_TELEGRAM_STREAM", "2.0")),
    "tts_clear_delay": float(os.getenv("INTERVAL_TTS_CLEAR_DELAY", "5.0")),
}

# Whisper locale
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "it")

# Whisper initial_prompt — context per migliorare trascrizione (max ~224 token / ~900 chars)
# IMPORTANTE: scrivere in PROSA FLUIDA, senza elenchi numerati/puntati né separatori.
# Whisper replica lo stile del prompt nell'output: prosa → output pulito.
WHISPER_PROMPT = os.getenv("WHISPER_PROMPT", (
    "JARVIS è l'assistente AI di casa. Le persone che parlano sono Marco, Ada, "
    "Giorgio, Sofia, Loredana, Mario e Melina. "
    "I comandi domotici più frequenti sono accendi, spegni, apri, chiudi, alza, "
    "abbassa, muta, stop e silenzio. "
    "Le stanze della casa sono Ingresso, Soggiorno, Cucina, Lavanderia, "
    "Disimpegno, Camera, Cabina armadio, Cameretta, Bagno grande, Bagno piccolo, "
    "Balcone interno, Balcone esterno, Garage e Box. "
    "Le zone sono Notte, Giorno e Esterno. "
    "I dispositivi includono TV, Cam, Lampada, Lampada Giorgio, Luce, Luci, "
    "Porta, Soundbar e Echo. "
    "Le luci si chiamano Centro Block, Strip Led, Divano, Faretto, Tavola, "
    "Braava, Roomba, Letto e Specchio. "
    "Si può anche chiedere di cercare su email, Drive, internet, Amazon, "
    "shopping, cron, trading e Twitter."
))

# STT normalization via LLM (Qwen) — disable per test con solo Whisper prompt
STT_NORMALIZE_ENABLED = os.getenv("STT_NORMALIZE_ENABLED", "false").lower() == "true"

# Keywords cache TTL
KEYWORDS_CACHE_TTL = int(os.getenv("KEYWORDS_CACHE_TTL", "300"))

# ===========================================================================
# VECTOR / CONTEXT THRESHOLDS
# ===========================================================================
VECTOR_SCORE_MIN_MESSAGES = float(os.getenv("VECTOR_SCORE_MIN_MESSAGES", "0.3"))
VECTOR_SCORE_MIN_FACTS = float(os.getenv("VECTOR_SCORE_MIN_FACTS", "0.4"))
VECTOR_RETENTION_DAYS = int(os.getenv("VECTOR_RETENTION_DAYS", "7"))
MAX_VECTOR_RESULTS_DEFAULT = int(os.getenv("MAX_VECTOR_RESULTS_DEFAULT", "30"))
VECTOR_METADATA_TRUNCATE = int(os.getenv("VECTOR_METADATA_TRUNCATE", "500"))
VECTOR_RECENCY_DECAY = float(os.getenv("VECTOR_RECENCY_DECAY", "0.05"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
CHROMA_PATH = os.getenv("CHROMA_PATH", "./data/chroma")

# Context builder search limits
CONTEXT_SEARCH_LIMITS = {
    "routing": {
        "n_messages": int(os.getenv("CTX_ROUTING_N_MESSAGES", "20")),
        "n_facts": int(os.getenv("CTX_ROUTING_N_FACTS", "10")),
        "n_location_results": int(os.getenv("CTX_ROUTING_N_LOCATION", "15")),
    },
    "reasoning": {
        "n_messages": int(os.getenv("CTX_REASONING_N_MESSAGES", "50")),
        "n_facts": int(os.getenv("CTX_REASONING_N_FACTS", "30")),
        "n_location_results": int(os.getenv("CTX_REASONING_N_LOCATION", "30")),
    },
}

# ===========================================================================
# LOCATION MEMORY LIMITS
# ===========================================================================
LOCATION_MEMORY_DEFAULTS = {
    "hot_minutes": int(os.getenv("LOC_MEM_HOT_MINUTES", "30")),
    "warm_hours": int(os.getenv("LOC_MEM_WARM_HOURS", "24")),
    "cold_days": int(os.getenv("LOC_MEM_COLD_DAYS", "7")),
    "max_tokens_per_location": int(os.getenv("LOC_MEM_MAX_TOKENS", "500")),
    "max_longterm_facts": int(os.getenv("LOC_MEM_MAX_FACTS", "5")),
    "max_hot_events": int(os.getenv("LOC_MEM_MAX_HOT_EVENTS", "10")),
    "n_results": int(os.getenv("LOC_MEM_N_RESULTS", "30")),
    "n_results_per_location": int(os.getenv("LOC_MEM_N_PER_LOCATION", "20")),
    "min_final_score": float(os.getenv("LOC_MEM_MIN_SCORE", "0.3")),
}

# ===========================================================================
# VOICE RECOGNITION
# ===========================================================================
VOICE_MODELS_DIR = os.getenv("VOICE_MODELS_DIR", "/app/data/voice_models")
VOICE_SIMILARITY_THRESHOLD = float(os.getenv("VOICE_SIMILARITY_THRESHOLD", "0.70"))
VOICE_MIN_ENROLLMENT_SAMPLES = int(os.getenv("VOICE_MIN_ENROLLMENT_SAMPLES", "24"))

# ===========================================================================
# DATABASE LIMITS
# ===========================================================================
OTP_EXPIRY_SECONDS = int(os.getenv("OTP_EXPIRY_SECONDS", "900"))
CHAT_MEMORY_MAX_AGE = int(os.getenv("CHAT_MEMORY_MAX_AGE", "86400"))
AUTO_LEARN_FREQUENCY_THRESHOLD = int(os.getenv("AUTO_LEARN_FREQUENCY", "5"))

# Service health
HEALTH_FAILURE_THRESHOLD = int(os.getenv("HEALTH_FAILURE_THRESHOLD", "3"))

# Main.py limits
MAX_WEIGHTED_CONTEXT_MESSAGES = int(os.getenv("MAX_WEIGHTED_CONTEXT_MESSAGES", "150"))
MAX_AUDIT_LOGS = int(os.getenv("MAX_AUDIT_LOGS", "100"))
MAX_AUDIT_LOGS_IN_REPORT = int(os.getenv("MAX_AUDIT_LOGS_IN_REPORT", "20"))
TTS_CHARS_PER_SECOND = float(os.getenv("TTS_CHARS_PER_SECOND", "10.0"))
TTS_SOUND_OFFSET = float(os.getenv("TTS_SOUND_OFFSET", "1.5"))
TTS_MIN_DURATION = float(os.getenv("TTS_MIN_DURATION", "3.0"))

# Multi-turn follow-up
FOLLOWUP_TRIGGER_BUFFER_S = float(os.getenv("FOLLOWUP_TRIGGER_BUFFER_S", "0.5"))

# Memory jobs
MEMORY_MIN_MESSAGES_FOR_SUMMARY = int(os.getenv("MEMORY_MIN_MESSAGES", "2"))
MEMORY_CONTENT_TRUNCATE = int(os.getenv("MEMORY_CONTENT_TRUNCATE", "200"))
MEMORY_HOURLY_TRIGGER_MINUTE = int(os.getenv("MEMORY_HOURLY_MINUTE", "5"))
MEMORY_DAILY_TRIGGER_HOUR = int(os.getenv("MEMORY_DAILY_HOUR", "3"))
MEMORY_VECTOR_CLEANUP_DAYS = int(os.getenv("MEMORY_VECTOR_CLEANUP_DAYS", "7"))

# ===========================================================================
# PROACTIVE MONITORING THRESHOLDS (candidati per global_preferences)
# ===========================================================================
PROACTIVE_DOOR_OPEN_MINUTES = int(os.getenv("PROACTIVE_DOOR_OPEN_MINUTES", "30"))
PROACTIVE_NO_MOTION_HOURS = int(os.getenv("PROACTIVE_NO_MOTION_HOURS", "12"))
PROACTIVE_NIGHT_START = int(os.getenv("PROACTIVE_NIGHT_START", "2"))
PROACTIVE_NIGHT_END = int(os.getenv("PROACTIVE_NIGHT_END", "5"))
SECURITY_ANIMAL_LABELS = os.getenv("SECURITY_ANIMAL_LABELS", "cat,dog").split(",")

# ===========================================================================
# TTS ENGINE
# ===========================================================================
# Engine: "xtts" (XTTSv2 Coqui, locale GPU) o "kokoro" (Kokoro-82M, cloud/CPU)
# Default: "xtts" per deploy locale (AI_BACKEND=local), "kokoro" per cloud (AI_BACKEND=api)
TTS_ENGINE = os.getenv("TTS_ENGINE", "xtts" if AI_BACKEND == "local" else "kokoro")

# --- XTTSv2 (deploy locale, GPU ~2.1 GB VRAM fp16) ---
XTTS_URL = os.getenv("XTTS_URL", "http://localhost:8890")
XTTS_SPEAKER = os.getenv("XTTS_SPEAKER", "default")   # nome file WAV in speakers/ (senza .wav)
XTTS_LANGUAGE = os.getenv("XTTS_LANGUAGE", "it")

# --- Kokoro (deploy cloud, CPU) ---
KOKORO_TTS_URL = os.getenv("KOKORO_TTS_URL", "http://localhost:8890")
KOKORO_TTS_VOICE = os.getenv("KOKORO_TTS_VOICE", "if_sara")

# --- Preprocessing (comune a entrambi gli engine) ---
TTS_PREPROCESS_ENABLED = os.getenv("TTS_PREPROCESS_ENABLED", "true").lower() == "true"
TTS_PREPROCESS_LLM = os.getenv("TTS_PREPROCESS_LLM", "false").lower() == "true"

# ===========================================================================
# WS AUDIO + VAD (server-side audio reception via WebSocket + Opus)
# ===========================================================================
WS_AUDIO_SESSION_TIMEOUT = int(os.getenv("WS_AUDIO_SESSION_TIMEOUT", "60"))
VAD_THRESHOLD = float(os.getenv("WEBRTC_VAD_THRESHOLD", os.getenv("VAD_THRESHOLD", "0.5")))
VAD_MIN_SILENCE_MS = int(os.getenv("WEBRTC_VAD_MIN_SILENCE_MS", os.getenv("VAD_MIN_SILENCE_MS", "700")))
VAD_MIN_SPEECH_MS = int(os.getenv("WEBRTC_VAD_MIN_SPEECH_MS", os.getenv("VAD_MIN_SPEECH_MS", "250")))
