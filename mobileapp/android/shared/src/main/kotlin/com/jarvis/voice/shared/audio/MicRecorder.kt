package com.jarvis.voice.shared.audio

import android.Manifest
import android.annotation.SuppressLint
import android.media.AudioFormat as AndroidAudioFormat
import android.media.AudioRecord
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AudioEffect
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import androidx.annotation.RequiresPermission
import com.jarvis.voice.shared.protocol.AudioFormat
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Cattura microfono 16 kHz mono int16 e consegna frame da 640 byte (20 ms).
 * L'uplink JARVIS usa PCM grezzo, quindi nessun encoding qui.
 *
 * [live] = sessione live full-duplex: sorgente VOICE_COMMUNICATION + AcousticEchoCanceler
 * (e NS/AGC se disponibili), niente gain x8. Il chiamante deve aver attivato
 * MODE_IN_COMMUNICATION ([LiveAudioRouting]) prima di start(). Default = path di oggi.
 */
class MicRecorder(live: Boolean = false) {

    private val profile = LiveAudio.profile(live)
    private val effects = mutableListOf<AudioEffect>()

    /** true se l'AEC di piattaforma è attaccato alla sessione di record (solo live). */
    @Volatile var echoCancelerActive = false
        private set

    private var record: AudioRecord? = null
    private val running = AtomicBoolean(false)
    private var worker: Thread? = null

    @SuppressLint("MissingPermission")
    @RequiresPermission(Manifest.permission.RECORD_AUDIO)
    fun start(onFrame: (ByteArray) -> Unit): Boolean {
        if (running.get()) return true

        val minBuf = AudioRecord.getMinBufferSize(
            AudioFormat.SAMPLE_RATE,
            AndroidAudioFormat.CHANNEL_IN_MONO,
            AndroidAudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuf <= 0) return false

        val bufSize = maxOf(minBuf, AudioFormat.FRAME_BYTES * 4)
        val rec = AudioRecord(
            profile.micSource,
            AudioFormat.SAMPLE_RATE,
            AndroidAudioFormat.CHANNEL_IN_MONO,
            AndroidAudioFormat.ENCODING_PCM_16BIT,
            bufSize,
        )
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return false
        }

        record = rec
        if (profile.effects) attachEffects(rec.audioSessionId)
        running.set(true)
        rec.startRecording()

        worker = thread(name = "jarvis-mic", isDaemon = true) {
            val frame = ByteArray(AudioFormat.FRAME_BYTES)
            while (running.get()) {
                var off = 0
                while (off < frame.size && running.get()) {
                    val n = rec.read(frame, off, frame.size - off)
                    if (n <= 0) break
                    off += n
                }
                if (off == frame.size) {
                    if (profile.micGain != 1f) applyGain(frame)
                    onFrame(frame.copyOf())
                }
            }
        }
        return true
    }

    /**
     * Guadagno digitale sul frame PCM int16 (con clipping). Il mic del telefono/watch cattura
     * molto piano (RMS ~0.01): senza boost il VAD server non rileva mai la fine del parlato
     * (sessione infinita) e lo STT, amplificando poi il rumore, sbaglia lingua/speaker.
     */
    private fun applyGain(frame: ByteArray) {
        var i = 0
        while (i + 1 < frame.size) {
            val s = ((frame[i].toInt() and 0xFF) or (frame[i + 1].toInt() shl 8)).toShort().toInt()
            var v = (s * profile.micGain).toInt()
            if (v > 32767) v = 32767 else if (v < -32768) v = -32768
            frame[i] = (v and 0xFF).toByte()
            frame[i + 1] = ((v shr 8) and 0xFF).toByte()
            i += 2
        }
    }

    /** AEC (+ NS/AGC) sulla sessione di record, solo in live. Se mancano si va avanti senza. */
    private fun attachEffects(sessionId: Int) {
        runCatching {
            if (AcousticEchoCanceler.isAvailable()) {
                AcousticEchoCanceler.create(sessionId)?.let { it.enabled = true; effects += it; echoCancelerActive = true }
            }
            if (NoiseSuppressor.isAvailable()) {
                NoiseSuppressor.create(sessionId)?.let { it.enabled = true; effects += it }
            }
            if (AutomaticGainControl.isAvailable()) {
                AutomaticGainControl.create(sessionId)?.let { it.enabled = true; effects += it }
            }
        }.onFailure { Log.w(TAG, "effetti audio: ${it.message}") }
        Log.i(TAG, "live mic: AEC=${echoCancelerActive} effetti=${effects.size}")
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        worker?.join(500)
        worker = null
        effects.forEach { runCatching { it.release() } }
        effects.clear()
        echoCancelerActive = false
        record?.run {
            runCatching { stop() }
            release()
        }
        record = null
    }

    private companion object {
        const val TAG = "MicRecorder"
    }
}
