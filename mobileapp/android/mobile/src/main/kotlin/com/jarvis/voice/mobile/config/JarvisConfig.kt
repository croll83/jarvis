package com.jarvis.voice.mobile.config

import android.content.Context
import java.util.Locale

/**
 * Configurazione persistente del client (URL orchestrator, token, device_id, TTL).
 * Salvata in SharedPreferences — l'URL/token del tailnet non vanno hardcodati nel repo.
 */
class JarvisConfig(context: Context) {

    private val prefs = context.getSharedPreferences("jarvis", Context.MODE_PRIVATE)

    /** Base WS senza path, es. ws://100.x.y.z:5000 (l'app appende /ws/audio). */
    var url: String
        get() = prefs.getString(KEY_URL, "") ?: ""
        set(v) = prefs.edit().putString(KEY_URL, v.trim().removeSuffix("/")).apply()

    var token: String
        get() = prefs.getString(KEY_TOKEN, "") ?: ""
        set(v) = prefs.edit().putString(KEY_TOKEN, v.trim()).apply()

    /** MAC-like uppercase; se assente viene derivato da un ID stabile installazione. */
    var deviceId: String
        get() = prefs.getString(KEY_DEVICE_ID, "")?.takeIf { it.isNotBlank() } ?: defaultDeviceId()
        set(v) = prefs.edit().putString(KEY_DEVICE_ID, v.trim().uppercase(Locale.ROOT)).apply()

    /** TTL finestra calda in ms (default 10 min). */
    var ttlMillis: Long
        get() = prefs.getLong(KEY_TTL, 600_000L)
        set(v) = prefs.edit().putLong(KEY_TTL, v).apply()

    private fun defaultDeviceId(): String {
        // Pseudo-MAC stabile derivato da un UUID installazione (persistito).
        val existing = prefs.getString(KEY_GEN_ID, null)
        if (existing != null) return existing
        val hex = java.util.UUID.randomUUID().toString().replace("-", "").take(12).uppercase(Locale.ROOT)
        prefs.edit().putString(KEY_GEN_ID, hex).apply()
        return hex
    }

    val isConfigured: Boolean get() = url.isNotBlank()

    private companion object {
        const val KEY_URL = "url"
        const val KEY_TOKEN = "token"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_TTL = "ttl_ms"
        const val KEY_GEN_ID = "gen_device_id"
    }
}
