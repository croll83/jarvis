package com.jarvis.voice.shared.wear

import java.io.DataInputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream

/**
 * Framing del verso telefono → watch del canale [DataLayer.PATH_AUDIO_FRAMED].
 *
 * Prima lo stream era PCM grezzo e il barge-in viaggiava a parte su MessageClient
 * (PATH_CLEAR): arrivava in ritardo e svuotava il player al momento sbagliato, spesso la
 * testa della risposta successiva. Qui il clear è un frame NELLO STESSO stream, in ordine
 * con l'audio, e ogni frame dice se è parlato della sessione live (con rate e stream).
 *
 * Header di 4 byte: tipo u8, arg u8, lunghezza payload u16 LE. Poi il payload.
 *  - [TYPE_TTS]   PCM 16 kHz s16le del path di oggi (TTS dell'orchestrator). arg = 0.
 *  - [TYPE_LIVE]  PCM s16le del modello in sessione live. arg = rate/1000 (bit 0-5)
 *                 | [ARG_COMMS] se va suonato sullo stream comms invece che media.
 *  - [TYPE_CLEAR] barge-in: butta tutto il parlato live ricevuto finora. Niente payload.
 * Tipi sconosciuti: il payload viene saltato (compatibilità in avanti).
 *
 * Il verso watch → telefono (mic) resta PCM grezzo a frame da 640 byte.
 */
object WatchStream {
    const val TYPE_TTS = 1
    const val TYPE_LIVE = 2
    const val TYPE_CLEAR = 3

    const val ARG_COMMS = 0x80
    private const val RATE_MASK = 0x3F
    const val HEADER_BYTES = 4
    const val MAX_PAYLOAD = 0xFFFF

    sealed class Frame {
        class Tts(val pcm: ShortArray) : Frame()
        class Live(val pcm: ShortArray, val sampleRate: Int, val comms: Boolean) : Frame()
        object Clear : Frame()
        class Unknown(val type: Int) : Frame()
    }

    fun tts(pcm: ShortArray): ByteArray = encode(TYPE_TTS, 0, pcm)

    fun live(pcm: ShortArray, sampleRate: Int, comms: Boolean): ByteArray {
        require(sampleRate % 1000 == 0 && sampleRate / 1000 in 8..48) { "rate $sampleRate" }
        return encode(TYPE_LIVE, (sampleRate / 1000) or (if (comms) ARG_COMMS else 0), pcm)
    }

    fun clear(): ByteArray = byteArrayOf(TYPE_CLEAR.toByte(), 0, 0, 0)

    private fun encode(type: Int, arg: Int, pcm: ShortArray): ByteArray {
        val len = pcm.size * 2
        require(len <= MAX_PAYLOAD) { "frame troppo lungo: ${pcm.size} campioni" }
        val b = ByteArray(HEADER_BYTES + len)
        b[0] = type.toByte()
        b[1] = arg.toByte()
        b[2] = (len and 0xFF).toByte()
        b[3] = (len shr 8).toByte()
        var o = HEADER_BYTES
        for (s in pcm) {
            val v = s.toInt()
            b[o++] = (v and 0xFF).toByte()
            b[o++] = (v shr 8).toByte()
        }
        return b
    }

    /** Lettore bloccante dei frame. Non thread-safe: un solo thread di lettura. */
    class Reader(input: InputStream) {
        private val din = if (input is DataInputStream) input else DataInputStream(input)
        private val header = ByteArray(HEADER_BYTES)
        private var buf = ByteArray(2048)

        /** Prossimo frame; null a fine stream (canale chiuso). */
        @Throws(IOException::class)
        fun next(): Frame? {
            try {
                din.readFully(header)
            } catch (_: EOFException) {
                return null
            }
            val type = header[0].toInt() and 0xFF
            val arg = header[1].toInt() and 0xFF
            val len = (header[2].toInt() and 0xFF) or ((header[3].toInt() and 0xFF) shl 8)
            if (buf.size < len) buf = ByteArray(len)
            din.readFully(buf, 0, len)
            return when (type) {
                TYPE_TTS -> Frame.Tts(pcm(len))
                TYPE_LIVE -> Frame.Live(pcm(len), (arg and RATE_MASK) * 1000, arg and ARG_COMMS != 0)
                TYPE_CLEAR -> Frame.Clear
                else -> Frame.Unknown(type)
            }
        }

        private fun pcm(len: Int): ShortArray {
            val n = len / 2
            return ShortArray(n) { i ->
                ((buf[2 * i].toInt() and 0xFF) or (buf[2 * i + 1].toInt() shl 8)).toShort()
            }
        }
    }
}
