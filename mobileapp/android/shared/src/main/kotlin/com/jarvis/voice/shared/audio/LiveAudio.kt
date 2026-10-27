package com.jarvis.voice.shared.audio

import android.media.AudioAttributes
import android.media.MediaRecorder

/**
 * Parametri audio per modalità. "normale" = push-to-talk half-duplex di oggi (invariato);
 * "live" = sessione live full-duplex sul Voice Live Hub, attiva SOLO tra inizio e fine sessione.
 */
data class AudioProfile(
    val micSource: Int,
    val micGain: Float,
    val effects: Boolean,      // AcousticEchoCanceler (+ NS/AGC) sulla sessione di record
    val playbackUsage: Int,
)

object LiveAudio {
    /** Il modello parla a 24 kHz; il mic verso l'hub resta a 16 kHz (AudioFormat.SAMPLE_RATE). */
    const val MODEL_SAMPLE_RATE = 24_000

    /**
     * Fallback: playback live su USAGE_MEDIA invece dello stream comms. Lo stream comms è
     * quello che l'AEC usa come riferimento, ma sul watch USAGE_ASSISTANT veniva silenziato:
     * se succede anche con USAGE_VOICE_COMMUNICATION, mettere true.
     */
    const val PLAYBACK_MEDIA_USAGE = false

    /** Guadagno software del path normale (mic telefono/watch molto basso). */
    const val NORMAL_MIC_GAIN = 8f

    /**
     * Il mic live non ha il gain x8 (lo alzano sorgente comms e AGC): la curva del livello
     * della waveform (tarata sul mic x8) va moltiplicata per questo, solo per la UI.
     * Da tarare sul dispositivo insieme al livello del mic live.
     */
    const val LIVE_LEVEL_BOOST = 3f

    fun profile(live: Boolean, mediaUsageFallback: Boolean = PLAYBACK_MEDIA_USAGE): AudioProfile =
        if (!live) {
            AudioProfile(
                micSource = MediaRecorder.AudioSource.VOICE_RECOGNITION,
                micGain = NORMAL_MIC_GAIN,
                effects = false,
                playbackUsage = AudioAttributes.USAGE_MEDIA,
            )
        } else {
            // Niente x8: distorce e sabota l'AEC. Il livello lo regolano la sorgente comms e l'AGC.
            AudioProfile(
                micSource = MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                micGain = 1f,
                effects = true,
                playbackUsage = if (mediaUsageFallback) AudioAttributes.USAGE_MEDIA
                                else AudioAttributes.USAGE_VOICE_COMMUNICATION,
            )
        }
}
