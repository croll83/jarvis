package com.jarvis.voice.shared.audio

import android.media.AudioAttributes
import android.media.AudioFormat as AndroidAudioFormat
import android.media.AudioTrack
import com.jarvis.voice.shared.protocol.AudioFormat

/**
 * Riproduce PCM 16 kHz mono int16 in streaming (i frame TTS decodificati).
 * Modalità streaming: si scrivono chunk mano a mano che arrivano dall'orchestrator.
 */
class TtsPlayer {

    private var track: AudioTrack? = null

    fun start() {
        if (track != null) return
        val minBuf = AudioTrack.getMinBufferSize(
            AudioFormat.SAMPLE_RATE,
            AndroidAudioFormat.CHANNEL_OUT_MONO,
            AndroidAudioFormat.ENCODING_PCM_16BIT,
        )
        val t = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AndroidAudioFormat.Builder()
                    .setSampleRate(AudioFormat.SAMPLE_RATE)
                    .setChannelMask(AndroidAudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AndroidAudioFormat.ENCODING_PCM_16BIT)
                    .build()
            )
            .setBufferSizeInBytes(maxOf(minBuf, AudioFormat.FRAME_BYTES * 8))
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

    fun stop() {
        track?.run {
            runCatching { pause(); flush(); stop() }
            release()
        }
        track = null
    }
}
