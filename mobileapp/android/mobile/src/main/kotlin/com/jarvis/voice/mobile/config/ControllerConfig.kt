package com.jarvis.voice.mobile.config

/**
 * Ciò che il [com.jarvis.voice.mobile.ws.JarvisController] legge della configurazione.
 * In app è [JarvisConfig] (SharedPreferences); nei test JVM una config in memoria.
 */
interface ControllerConfig {
    /** Base WS senza path, es. ws://100.x.y.z:5000 (l'app appende /ws/audio). */
    val url: String
    val token: String
    /** TTL finestra calda in ms. */
    val ttlMillis: Long
    /** device_id effettivamente usato per la connessione. */
    val deviceId: String
    val isConfigured: Boolean
}
