package com.jarvis.voice.shared.audio

/** Conversioni PCM int16 little-endian ↔ ShortArray e livello RMS per le waveform. */
object Pcm {

    fun toShorts(bytes: ByteArray, offset: Int = 0, length: Int = bytes.size - offset): ShortArray {
        val out = ShortArray(length / 2)
        var i = 0
        while (i < out.size) {
            val lo = bytes[offset + i * 2].toInt() and 0xFF
            val hi = bytes[offset + i * 2 + 1].toInt()
            out[i] = ((hi shl 8) or lo).toShort()
            i++
        }
        return out
    }

    fun toBytes(pcm: ShortArray): ByteArray {
        val out = ByteArray(pcm.size * 2)
        for (i in pcm.indices) {
            val v = pcm[i].toInt()
            out[i * 2] = (v and 0xFF).toByte()
            out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    /** RMS → 0..1 con la stessa sensibilità della waveform del mic (x9). */
    fun level(pcm: ShortArray): Float {
        if (pcm.isEmpty()) return 0f
        var sum = 0.0
        for (s in pcm) sum += s.toDouble() * s
        return (Math.sqrt(sum / pcm.size) / 32768.0 * 9.0).coerceIn(0.0, 1.0).toFloat()
    }
}
