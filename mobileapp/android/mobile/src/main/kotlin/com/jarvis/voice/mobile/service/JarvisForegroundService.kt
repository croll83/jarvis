package com.jarvis.voice.mobile.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.LifecycleService
import com.jarvis.voice.mobile.core.JarvisRuntime
import com.jarvis.voice.mobile.live.LocalLiveAudio
import com.jarvis.voice.shared.audio.MicRecorder
import com.jarvis.voice.shared.protocol.EndReason

/**
 * Tiene "calda" la connessione WS nella finestra TTL e possiede il mic locale.
 * Parte on-demand (primo tap) e si autospegne quando il controller torna IDLE a TTL scaduto.
 *
 * NB: FGS di tipo "microphone" richiede RECORD_AUDIO concesso a runtime (Android 14+).
 */
class JarvisForegroundService : LifecycleService() {

    private val mic = MicRecorder()
    private var liveAudio: LocalLiveAudio? = null

    override fun onCreate() {
        super.onCreate()
        JarvisRuntime.init(this)
        val c = JarvisRuntime.controller

        // Wiring mic locale ↔ controller (solo modalità standalone telefono)
        c.onLocalMicStart = {
            if (hasRecordPermission()) {
                mic.start { frame -> c.feedMicFrame(frame) }
            }
        }
        c.onLocalMicStop = { mic.stop() }

        // Sessione live hub sul telefono: mic comms + AEC + player 24 kHz, solo in sessione.
        liveAudio = LocalLiveAudio(this) { frame -> c.feedMicFrame(frame) }
        c.liveAudio = liveAudio

        startAsForeground()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        return START_STICKY
    }

    override fun onDestroy() {
        mic.stop()
        // Senza service la sessione hub del telefono resterebbe sorda e muta fino al timeout
        // dell'hub: si chiude adesso e l'orchestrator lo sa (no-op fuori sessione).
        if (JarvisRuntime.controller.liveAudio === liveAudio) {
            JarvisRuntime.controller.abortLiveSession(EndReason.ERROR)
            JarvisRuntime.controller.liveAudio = null
        }
        liveAudio?.stop()
        JarvisRuntime.controller.onLocalMicStart = null
        JarvisRuntime.controller.onLocalMicStop = null
        super.onDestroy()
    }

    private fun hasRecordPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    private fun startAsForeground() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "JARVIS", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val n: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("JARVIS attivo")
            .setContentText("Connessione vocale pronta")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    companion object {
        private const val CHANNEL_ID = "jarvis_voice"
        private const val NOTIF_ID = 42

        fun start(context: Context) {
            val i = Intent(context, JarvisForegroundService::class.java)
            ContextCompat.startForegroundService(context, i)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, JarvisForegroundService::class.java))
        }
    }
}
