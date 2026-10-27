package com.jarvis.voice.shared.audio

import android.media.AudioAttributes
import android.media.AudioFormat as AndroidAudioFormat
import android.media.AudioTrack
import com.jarvis.voice.shared.protocol.AudioFormat

/**
 * Riproduce PCM 16 kHz mono int16 in streaming (i frame TTS decodificati).
 * Modalità streaming: si scrivono chunk mano a mano che arrivano dall'orchestrator.
 *
 * [live] = sessione live full-duplex: stream comms (riferimento dell'AEC) invece di
 * USAGE_MEDIA, salvo il fallback [LiveAudio.PLAYBACK_MEDIA_USAGE]. [sampleRate] = 24 kHz
 * quando suona direttamente il parlato del modello. Default = path di oggi.
 */
class TtsPlayer(
    private val sampleRate: Int = AudioFormat.SAMPLE_RATE,
    live: Boolean = false,
) {

    private val usage = LiveAudio.profile(live).playbackUsage
    @Volatile private var track: AudioTrack? = null

    fun start() {
        if (track != null) return
        val minBuf = AudioTrack.getMinBufferSize(
            sampleRate,
            AndroidAudioFormat.CHANNEL_OUT_MONO,
            AndroidAudioFormat.ENCODING_PCM_16BIT,
        )
        val t = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    // USAGE_MEDIA: suona anche con suoneria in vibrazione/silenzioso
                    // (USAGE_ASSISTANT veniva silenziato sul watch).
                    .setUsage(usage)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AndroidAudioFormat.Builder()
                    .setSampleRate(sampleRate)
                    .setChannelMask(AndroidAudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AndroidAudioFormat.ENCODING_PCM_16BIT)
                    .build()
            )
            .setBufferSizeInBytes(maxOf(minBuf, sampleRate / 50 * 2 * 8))   // 8 frame da 20 ms
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        t.play()
        track = t
    }

    /** Scrive PCM int16. Bloccante finché il buffer non accetta i dati (MODE_STREAM). */
    fun write(pcm: ShortArray) {
        track?.write(pcm, 0, pcm.size, AudioTrack.WRITE_BLOCKING)
    }

    fun write(pcm: ByteArray) {
        track?.write(pcm, 0, pcm.size)
    }

    /** Barge-in: butta SUBITO l'audio in buffer non ancora suonato, il track resta pronto. */
    fun clear() {
        track?.run { runCatching { pause(); flush(); play() } }
    }

    fun stop() {
        track?.run {
            runCatching { pause(); flush(); stop() }
            release()
        }
        track = null
    }
}
