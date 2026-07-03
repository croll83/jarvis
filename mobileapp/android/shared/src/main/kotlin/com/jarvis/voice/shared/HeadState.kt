package com.jarvis.voice.shared

/**
 * Stato della "testa robot" mostrata all'utente. Mappa gli eventi di protocollo:
 *   IDLE       → nessuna sessione (tap per parlare)
 *   CONNECTING → apertura WS on-demand (solo la prima volta / dopo scadenza TTL)
 *   LISTENING  → audio_start/ready o trigger_listen (mic attivo)
 *   THINKING   → speech_end (STT + AI in corso)
 *   SPEAKING   → tts_start..tts_done (riproduzione risposta)
 */
enum class HeadState {
    IDLE,
    CONNECTING,
    LISTENING,
    THINKING,
    SPEAKING,
    ERROR,       // connessione persa / non configurato → occhi X rossi
}
