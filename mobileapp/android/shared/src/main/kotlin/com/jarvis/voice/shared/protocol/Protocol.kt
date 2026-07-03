package com.jarvis.voice.shared.protocol

/**
 * Costanti del protocollo JARVIS `/ws/audio`.
 * Riferimento: jarvis-orchestrator/ws_audio_handler.py e tools/jarvis_ptt_spike/.
 *
 * Questo file è la fonte di verità condivisa tra il client telefono e (indirettamente)
 * lo watch. La state machine effettiva vive in mobile/ws/JarvisController.kt.
 */
object MsgType {
    // ── device → server ──────────────────────────────────────────────
    const val HELLO = "hello"
    const val AUDIO_START = "audio_start"
    const val AUDIO_FLUSH = "audio_flush"   // secondo tap: chiudi turno e processa SUBITO
    const val AUDIO_END = "audio_end"       // abort: scarta l'audio
    const val STATE = "state"
    const val SPEAKER_STOP = "speaker_stop" // barge-in
    const val VOLUME_CHANGE = "volume_change"
    const val PONG = "pong"

    // ── server → device ──────────────────────────────────────────────
    const val WELCOME = "welcome"
    const val READY = "ready"
    const val SPEECH_END = "speech_end"
    const val TRIGGER_LISTEN = "trigger_listen"
    const val TTS_START = "tts_start"
    const val TTS_DONE = "tts_done"
    const val CONFIG_UPDATE = "config_update"
    const val WAKE_DETECTED = "wake_detected"
    const val LIVE_SESSION_START = "live_session_start"
    const val LIVE_SESSION_END = "live_session_end"
    const val PING = "ping"
    const val ERROR = "error"
    const val TRANSCRIPT = "transcript"  // testo di ciò che ha detto l'utente (storico)
    const val RESPONSE = "response"      // testo della risposta di JARVIS (storico)
}

object Codec {
    const val PCM = "pcm"    // uplink usato dal client mobile: nessun encoding
    const val OPUS = "opus"  // usato dagli AtomS3R
}
