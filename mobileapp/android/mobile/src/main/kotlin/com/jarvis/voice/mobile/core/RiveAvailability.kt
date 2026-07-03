package com.jarvis.voice.mobile.core

import android.content.Context
import android.util.Log
import app.rive.runtime.kotlin.core.Rive

/**
 * Inizializzazione GUARDATA di Rive. L'auto-init via androidx.startup è disabilitato in
 * manifest (crashava all'avvio del processo). Qui carichiamo le native una volta sola,
 * catturando qualsiasi Throwable (es. UnsatisfiedLinkError da allineamento 16 KB page):
 * se fallisce, [ok] resta false e RiveFace usa il fallback disegnato, senza crash.
 */
object RiveAvailability {
    @Volatile
    var ok = false
        private set

    @Volatile
    private var tried = false

    fun tryInit(context: Context) {
        if (tried) return
        tried = true
        ok = runCatching { Rive.init(context.applicationContext) }
            .onFailure { Log.w("RiveAvailability", "Rive init failed → fallback avatar: ${it.message}") }
            .isSuccess
    }
}
