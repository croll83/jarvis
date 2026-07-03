package com.jarvis.voice.mobile.core

import android.content.Context
import com.jarvis.voice.mobile.config.JarvisConfig
import com.jarvis.voice.mobile.ws.JarvisController

/**
 * Holder app-scoped di config + controller (unica istanza condivisa tra UI, foreground
 * service e relay watch). Inizializzare con applicationContext prima dell'uso.
 */
object JarvisRuntime {
    private lateinit var appCtx: Context

    fun init(context: Context) {
        if (!::appCtx.isInitialized) appCtx = context.applicationContext
    }

    val config: JarvisConfig by lazy { JarvisConfig(appCtx) }
    val controller: JarvisController by lazy { JarvisController(config) }
}
