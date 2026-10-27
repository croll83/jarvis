package com.jarvis.voice.mobile.live

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log
import androidx.core.content.ContextCompat
import com.jarvis.voice.shared.audio.LiveAudio
import com.jarvis.voice.shared.audio.LiveAudioRouting
import com.jarvis.voice.shared.audio.MicRecorder
import com.jarvis.voice.shared.audio.PlaybackPump
import com.jarvis.voice.shared.audio.TtsPlayer

/**
 * Sessione live sul telefono: pattern VoIP con AEC on-device.
 *   MODE_IN_COMMUNICATION → TtsPlayer comms 24 kHz → MicRecorder VOICE_COMMUNICATION + AEC
 * Il mic resta aperto anche mentre il modello parla (full-duplex, niente gate né gain x8).
 */
class LocalLiveAudio(
    private val context: Context,
    private val onMicFrame: (ByteArray) -> Unit,
) : LiveAudioPort {

    private val tag = "LocalLiveAudio"
    private val lock = Any()
    private var routing: LiveAudioRouting? = null
    private var player: TtsPlayer? = null
    private var pump: PlaybackPump? = null
    private var mic: MicRecorder? = null

    @SuppressLint("MissingPermission")
    override fun start(onPlaybackLevel: (Float) -> Unit): Boolean = synchronized(lock) {
        if (mic != null) return true
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) return false

        // Ordine: prima il modo comms (AEC di piattaforma), poi player e record.
        val r = LiveAudioRouting(context).also { it.enter() }
        val p = TtsPlayer(LiveAudio.MODEL_SAMPLE_RATE, live = true).also { it.start() }
        val q = PlaybackPump(LiveAudio.MODEL_SAMPLE_RATE, sink = { p.write(it) }, onLevel = onPlaybackLevel)
            .also { it.start() }
        val m = MicRecorder(live = true)
        if (!m.start(onMicFrame)) {
            Log.w(tag, "mic live non partito")
            q.stop(); p.stop(); r.exit()
            return false
        }
        if (!m.echoCancelerActive) Log.w(tag, "AcousticEchoCanceler non disponibile: barge-in a rischio eco")
        routing = r; player = p; pump = q; mic = m
        true
    }

    override fun play(pcm: ShortArray) { pump?.offer(pcm) }

    override fun clear() {
        pump?.clear()
        player?.clear()
    }

    override fun stop() {
        synchronized(lock) {
            mic?.stop(); mic = null
            pump?.stop(); pump = null
            player?.stop(); player = null
            routing?.exit(); routing = null
        }
    }
}
