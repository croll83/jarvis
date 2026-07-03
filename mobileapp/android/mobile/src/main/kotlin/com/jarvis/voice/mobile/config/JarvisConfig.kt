package com.jarvis.voice.mobile.config

import android.annotation.SuppressLint
import android.content.Context
import android.provider.Settings
import java.util.Locale

/**
 * Configurazione persistente del client (URL orchestrator, token, device_id, TTL).
 * Salvata in SharedPreferences — l'URL/token del tailnet non vanno hardcodati nel repo.
 */
class JarvisConfig(private val context: Context) {

    private val prefs = context.getSharedPreferences("jarvis", Context.MODE_PRIVATE)

    /** Base WS senza path, es. ws://100.x.y.z:5000 (l'app appende /ws/audio). */
    var url: String
        get() = prefs.getString(KEY_URL, "") ?: ""
        set(v) = prefs.edit().putString(KEY_URL, v.trim().removeSuffix("/")).apply()

    var token: String
        get() = prefs.getString(KEY_TOKEN, "") ?: ""
        set(v) = prefs.edit().putString(KEY_TOKEN, v.trim()).apply()

    /** TTL finestra calda in ms (default 10 min). */
    var ttlMillis: Long
        get() = prefs.getLong(KEY_TTL, 600_000L)
        set(v) = prefs.edit().putLong(KEY_TTL, v).apply()

    // ── device_id: MODE_AUTO (MAC del dispositivo) | MODE_CUSTOM (inserito a mano) ─────

    /** Modalità di scelta del device_id: [MODE_AUTO] o [MODE_CUSTOM]. */
    var deviceIdMode: String
        get() = prefs.getString(KEY_ID_MODE, MODE_AUTO) ?: MODE_AUTO
        set(v) = prefs.edit().putString(KEY_ID_MODE, v).apply()

    /** device_id inserito manualmente (usato solo se [deviceIdMode] == [MODE_CUSTOM]). */
    var customDeviceId: String
        get() = prefs.getString(KEY_CUSTOM_ID, "") ?: ""
        set(v) = prefs.edit().putString(KEY_CUSTOM_ID, normalizeId(v)).apply()

    /**
     * device_id "MAC del dispositivo" (automatico). Android ≥ 6 non espone il MAC
     * hardware reale, quindi lo deriviamo da ANDROID_ID (stabile per dispositivo +
     * chiave di firma, sopravvive al clear dei dati) formattato come 12 hex uppercase.
     */
    @get:SuppressLint("HardwareIds")
    val autoDeviceId: String
        get() {
            val androidId = runCatching {
                Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            }.getOrNull()
            val hex = androidId
                ?.filter { it.isDigit() || it in 'a'..'f' || it in 'A'..'F' }
                ?.takeIf { it.length >= 12 }
                ?.take(12)
                ?.uppercase(Locale.ROOT)
            if (hex != null) return hex
            // Fallback: UUID persistito (se ANDROID_ID assente)
            val existing = prefs.getString(KEY_GEN_ID, null)
            if (existing != null) return existing
            val gen = java.util.UUID.randomUUID().toString()
                .replace("-", "").take(12).uppercase(Locale.ROOT)
            prefs.edit().putString(KEY_GEN_ID, gen).apply()
            return gen
        }

    /** device_id effettivamente usato per la connessione. */
    val deviceId: String
        get() = if (deviceIdMode == MODE_CUSTOM && customDeviceId.isNotBlank()) customDeviceId
        else autoDeviceId

    /**
     * device_id del Wear (relay). È un device separato lato orchestrator (va configurato
     * in dashboard con use_internal_speaker=true). Default = device_id telefono + "W".
     */
    var wearDeviceId: String
        get() = prefs.getString(KEY_WEAR_ID, "")?.takeIf { it.isNotBlank() } ?: (deviceId + "W")
        set(v) = prefs.edit().putString(KEY_WEAR_ID, normalizeId(v)).apply()

    val isConfigured: Boolean get() = url.isNotBlank()

    private fun normalizeId(v: String): String = v.trim().uppercase(Locale.ROOT)

    companion object {
        const val MODE_AUTO = "auto"
        const val MODE_CUSTOM = "custom"

        private const val KEY_URL = "url"
        private const val KEY_TOKEN = "token"
        private const val KEY_TTL = "ttl_ms"
        private const val KEY_ID_MODE = "device_id_mode"
        private const val KEY_CUSTOM_ID = "custom_device_id"
        private const val KEY_WEAR_ID = "wear_device_id"
        private const val KEY_GEN_ID = "gen_device_id"
    }
}
