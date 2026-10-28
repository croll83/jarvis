package com.jarvis.voice.shared.wear

/**
 * Path del Wearable Data Layer usati tra Galaxy Watch e telefono.
 *
 * Modello: lo watch è "stateless" (niente WS, niente token, niente Tailscale).
 *  - audio: un unico ChannelClient BIDIREZIONALE (watch scrive PCM mic, legge PCM TTS)
 *  - controllo: MessageClient (messaggi corti, event-driven)
 *
 * Il telefono possiede la connessione WS verso l'orchestrator e fa da relay.
 */
object DataLayer {
    /** ChannelClient bidirezionale: watch→phone = mic PCM, phone→watch = TTS PCM (16k mono int16). */
    const val PATH_AUDIO = "/jarvis/audio"

    /**
     * Stesso canale bidirezionale, ma il verso phone→watch è a frame ([WatchStream]): TTS,
     * parlato live (con rate e stream) e clear del barge-in IN ORDINE con l'audio. Lo watch
     * da 0.3.0 apre questo; il telefono accetta entrambi (PATH_AUDIO = watch vecchio, PCM
     * grezzo + PATH_CLEAR). Il verso watch→phone (mic) resta PCM grezzo in entrambi.
     */
    const val PATH_AUDIO_FRAMED = "/jarvis/audio2"

    /** MessageClient watch→phone: l'utente ha toccato la testa (tap). */
    const val PATH_TAP = "/jarvis/tap"

    /** MessageClient watch→phone: stop / barge-in (tap durante la risposta). */
    const val PATH_STOP = "/jarvis/stop"

    /**
     * Payload di [PATH_STOP] quando lo watch chiude una sessione live perché non riesce a
     * tenerla (mic o canale audio non partiti): il telefono la chiude con reason "error".
     * Payload vuoto = stop di oggi (tap / gesto dell'utente).
     */
    const val STOP_ERROR = "error"

    /** MessageClient phone→watch: aggiornamento HeadState (payload = HeadState.name in UTF-8). */
    const val PATH_STATE = "/jarvis/state"

    /**
     * MessageClient phone→watch: entra/esci dalla modalità live full-duplex (sessione sul
     * Voice Live Hub). Payload [LIVE_START] / [LIVE_STOP]. In live il watch tiene il mic
     * aperto anche mentre suona, con AEC locale; il tunnel audio resta questo ChannelClient.
     */
    const val PATH_LIVE = "/jarvis/live"
    const val LIVE_START = "start"
    const val LIVE_STOP = "stop"

    /**
     * MessageClient phone→watch: barge-in in sessione live, svuota SUBITO il TtsPlayer.
     * Solo verso gli watch sul canale legacy [PATH_AUDIO]: sul canale a frame il clear è in banda.
     */
    const val PATH_CLEAR = "/jarvis/clear"
}
