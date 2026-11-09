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
    // live_session_end in ingresso: fine di una sessione hub decisa dal device (vedi EndReason)
    // Fase 3 (design-voice-live-hub.md sez. 19): tap che apre subito la sessione hub
    const val LIVE_START = "live_start"                    // {req_id, trigger:"tap"}
    const val LIVE_CANCEL = "live_cancel"                  // {req_id}: timeout o rinuncia
    const val LIVE_SESSION_ACTIVE = "live_session_active"  // {call_id, req_id?}: accept dell'hub

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
    // Fase 3
    const val LIVE_DECLINED = "live_declined"  // {req_id, reason}: niente live, turno classico
    const val LIVE_ON_WAKE = "live_on_wake"    // {live_on_wake: bool}: flag cambiato in admin
    const val ANNOUNCE = "announce"            // {call_id, text, urgent}: da inoltrare all'hub
}

/** Fase 3: richiesta di sessione hub dal tap (live_start / live_declined). */
object LiveWake {
    const val TRIGGER_TAP = "tap"
    const val ORIGIN_TAP = "tap"
    const val ORIGIN_WAKE = "wake"

    /** Origini con l'accept tardivo dell'hub e il ripiego classico in locale. */
    fun isRequestOrigin(origin: String?): Boolean = origin == ORIGIN_TAP || origin == ORIGIN_WAKE

    /**
     * Rifiuti dopo cui il peer torna a riposo SENZA turno classico (main.py LIVE_DECLINE_REASONS:
     * "duplicate" e "voice_gate"); con tutti gli altri (disabled, hub_off, hub_down, busy,
     * enrollment, sconosciuti) parte subito il turno classico.
     */
    val SILENT_DECLINES = setOf("duplicate", "voice_gate")

    /** Attesa massima della risposta dell'orchestrator al live_start (poi live_cancel e classico). */
    const val ANSWER_TIMEOUT_MS = 2_500L

    /** Accept tardivo dell'hub per le origini wake/tap (contesto + Gemini aperto, tetto 5 s lato hub). */
    const val ACCEPT_TIMEOUT_MS = 7_000
}

/** Motivi di `live_session_end` device → orchestrator per le sessioni sul Voice Live Hub. */
object EndReason {
    const val BUTTON = "button"
    const val HUB_HANGUP = "hub_hangup"
    const val HUB_REJECTED = "hub_rejected"
    const val HUB_UNREACHABLE = "hub_unreachable"
    const val ERROR = "error"

    /**
     * true se con questo live_session_end l'orchestrator ripiega sulla sessione live di oggi
     * (hub che rifiuta o non risponde, oppure `error`). Il device NON entra da
     * solo nella vecchia modalità live: aspetta il live_session_start semplice dell'orchestrator.
     */
    fun awaitsFallback(callId: String, reason: String): Boolean =
        reason == HUB_REJECTED || reason == HUB_UNREACHABLE || reason == ERROR
}

object Codec {
    const val PCM = "pcm"    // uplink usato dal client mobile: nessun encoding
    const val OPUS = "opus"  // usato dagli AtomS3R
}
