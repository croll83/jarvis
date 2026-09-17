package com.jarvis.voice.shared.audio

import android.Manifest
import android.annotation.SuppressLint
import android.media.AudioFormat as AndroidAudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import androidx.annotation.RequiresPermission
import com.jarvis.voice.shared.protocol.AudioFormat
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Cattura microfono 16 kHz mono int16 e consegna frame da 640 byte (20 ms).
 * L'uplink JARVIS usa PCM grezzo, quindi nessun encoding qui.
 */
private const val MIC_GAIN = 8f   // boost cattura (mic telefono/watch molto basso)

class MicRecorder {

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
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
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
                    applyGain(frame)
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
            var v = (s * MIC_GAIN).toInt()
            if (v > 32767) v = 32767 else if (v < -32768) v = -32768
            frame[i] = (v and 0xFF).toByte()
            frame[i + 1] = ((v shr 8) and 0xFF).toByte()
            i += 2
        }
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        worker?.join(500)
        worker = null
        record?.run {
            runCatching { stop() }
            release()
        }
        record = null
    }
}
