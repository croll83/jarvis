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

    /** MessageClient watch→phone: l'utente ha toccato la testa (tap). */
    const val PATH_TAP = "/jarvis/tap"

    /** MessageClient watch→phone: stop / barge-in (tap durante la risposta). */
    const val PATH_STOP = "/jarvis/stop"

    /** MessageClient phone→watch: aggiornamento HeadState (payload = HeadState.name in UTF-8). */
    const val PATH_STATE = "/jarvis/state"
}
