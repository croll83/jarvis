package com.jarvis.voice.shared.hub

import com.jarvis.voice.shared.protocol.AudioFormat
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.sqrt

/**
 * Buffer del comando detto subito dopo il tap (design-voice-live-hub.md 19.12, D1): il mic dal
 * tap all'accept dell'hub, PCM 16 kHz s16le mono, con un tetto ([maxBytes], 8 s come
 * LIVE_COMMAND_MAX_S del wakeword-server). All'accept va all'hub PRIMA del mic dal vivo.
 *
 * VAD a energia ([speechMs]) come `preroll.speech_ms` del wakeword-server: frame da 20 ms,
 * fondo = 10imo percentile dei frame, parlato = frame sopra fondo + [marginDb] e sopra
 * [minDbfs]. Differenze: niente pre-roll di riferimento (il telefono non ne ha), e i primi
 * [skipBytes] (vibrazione del tap, avvio del mic) e i frame a zero digitale non contano né
 * come parlato né per il fondo, altrimenti il silenzio d'avvio porterebbe la soglia a [minDbfs]
 * e il rumore della stanza conterebbe come parlato.
 *
 * Thread-safe: i frame arrivano dal thread del mic, la decisione dal thread dell'hub.
 */
class CommandBuffer(
    private val maxBytes: Int = DEFAULT_MAX_BYTES,
    private val skipBytes: Int = DEFAULT_SKIP_BYTES,
    private val minSpeechMs: Int = DEFAULT_MIN_SPEECH_MS,
    private val marginDb: Double = DEFAULT_MARGIN_DB,
    private val minDbfs: Double = DEFAULT_MIN_DBFS,
) {
    private val lock = Object()
    private var buf = ByteArray(minOf(maxBytes, 64 * 1024))
    private var size = 0

    /** Byte nel buffer. */
    val length: Int get() = synchronized(lock) { size }

    /** Aggiunge un frame del mic; oltre il tetto il resto si perde (l'inizio del comando conta di più). */
    fun add(frame: ByteArray) {
        synchronized(lock) {
            val room = maxBytes - size
            if (room <= 0 || frame.isEmpty()) return
            val n = minOf(room, frame.size)
            if (size + n > buf.size) buf = buf.copyOf(minOf(maxBytes, max(buf.size * 2, size + n)))
            System.arraycopy(frame, 0, buf, size, n)
            size += n
            lock.notifyAll()
        }
    }

    fun snapshot(): ByteArray = synchronized(lock) { buf.copyOf(size) }

    /** Millisecondi di parlato nel buffer (VAD a energia). */
    fun speechMs(): Int = speechMs(snapshot(), skipBytes, marginDb, minDbfs)

    /** true se nel buffer c'è un comando (almeno [minSpeechMs] di parlato). */
    fun hasCommand(): Boolean = speechMs() >= minSpeechMs

    /**
     * Aspetta (bloccando, thread dell'hub) che nel buffer compaia un comando, al massimo fino a
     * [deadlineMs] (System.currentTimeMillis). Ritorna appena lo trova: il comando decide
     * l'apertura dell'hub ("Eccomi" o sessione rapida), quindi si decide il più tardi possibile
     * ma senza ritardare l'offer quando il parlato c'è già.
     */
    fun awaitCommand(deadlineMs: Long): Boolean {
        while (true) {
            if (hasCommand()) return true
            val left = deadlineMs - System.currentTimeMillis()
            if (left <= 0) return false
            synchronized(lock) {
                try { lock.wait(minOf(left, POLL_MS)) } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt(); return hasCommand()
                }
            }
        }
    }

    companion object {
        const val FRAME_SAMPLES = AudioFormat.SAMPLE_RATE / 50   // 20 ms
        const val BYTES_PER_MS = AudioFormat.SAMPLE_RATE * 2 / 1000
        const val DEFAULT_MAX_BYTES = 8 * AudioFormat.SAMPLE_RATE * 2        // 8 s
        const val DEFAULT_SKIP_BYTES = 200 * BYTES_PER_MS                    // 200 ms
        const val DEFAULT_MIN_SPEECH_MS = 300
        const val DEFAULT_MARGIN_DB = 10.0
        const val DEFAULT_MIN_DBFS = -65.0
        private const val POLL_MS = 40L

        /** dBFS per frame da 20 ms; NaN = frame a zero digitale (mic non ancora partito). */
        fun frameDb(pcm: ByteArray): DoubleArray {
            val n = pcm.size / (FRAME_SAMPLES * 2)
            val out = DoubleArray(n)
            for (f in 0 until n) {
                var sum = 0.0
                var nonZero = false
                val base = f * FRAME_SAMPLES * 2
                for (i in 0 until FRAME_SAMPLES) {
                    val o = base + i * 2
                    val v = ((pcm[o].toInt() and 0xFF) or (pcm[o + 1].toInt() shl 8)).toShort().toInt()
                    if (v != 0) nonZero = true
                    sum += v.toDouble() * v
                }
                val rms = sqrt(sum / FRAME_SAMPLES)
                out[f] = if (!nonZero) Double.NaN else 20 * log10(max(rms, 1e-3) / 32768.0)
            }
            return out
        }

        fun speechMs(
            pcm: ByteArray,
            skipBytes: Int = DEFAULT_SKIP_BYTES,
            marginDb: Double = DEFAULT_MARGIN_DB,
            minDbfs: Double = DEFAULT_MIN_DBFS,
        ): Int {
            val db = frameDb(pcm)
            val first = (skipBytes + FRAME_SAMPLES * 2 - 1) / (FRAME_SAMPLES * 2)   // frame saltati (per eccesso)
            val counted = (first until db.size).map { db[it] }.filter { !it.isNaN() }
            if (counted.isEmpty()) return 0
            val floor = percentile(counted, 10.0)
            val thr = max(floor + marginDb, minDbfs)
            return counted.count { it > thr } * 20
        }

        /** Percentile con interpolazione lineare, come numpy.percentile di default. */
        fun percentile(values: List<Double>, p: Double): Double {
            val s = values.sorted()
            if (s.size == 1) return s[0]
            val rank = p / 100.0 * (s.size - 1)
            val lo = rank.toInt()
            val hi = minOf(lo + 1, s.size - 1)
            return s[lo] + (s[hi] - s[lo]) * (rank - lo)
        }
    }
}
