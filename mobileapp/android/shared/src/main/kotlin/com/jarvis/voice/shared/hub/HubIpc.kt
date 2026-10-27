package com.jarvis.voice.shared.hub

import java.io.DataInputStream
import java.io.IOException

/**
 * Framing del Voice Live Hub (voice_live_hub/ipc.py), identico byte per byte:
 *   frame = kind: u8 | len: u32 big-endian | payload
 *   kind 1 = JSON utf-8, kind 2 = audio con payload `id_len: u8 | call_id | s16le mono`.
 * Una connessione TCP = una sessione live.
 */
object HubIpc {
    const val JSON = 1
    const val AUDIO = 2
    const val MAX_FRAME = 1 shl 20
    const val HEADER_BYTES = 5

    sealed class Frame {
        class Json(val text: String) : Frame()
        class Audio(val callId: String, val pcm: ByteArray) : Frame()
    }

    fun encodeJson(json: String): ByteArray {
        val payload = json.toByteArray(Charsets.UTF_8)
        val out = ByteArray(HEADER_BYTES + payload.size)
        writeHeader(out, JSON, payload.size)
        System.arraycopy(payload, 0, out, HEADER_BYTES, payload.size)
        return out
    }

    fun encodeAudio(callId: String, pcm: ByteArray, offset: Int = 0, length: Int = pcm.size - offset): ByteArray {
        val cid = callId.toByteArray(Charsets.UTF_8)
        require(cid.size <= 255) { "call_id troppo lungo" }
        val payloadLen = 1 + cid.size + length
        val out = ByteArray(HEADER_BYTES + payloadLen)
        writeHeader(out, AUDIO, payloadLen)
        out[HEADER_BYTES] = cid.size.toByte()
        System.arraycopy(cid, 0, out, HEADER_BYTES + 1, cid.size)
        System.arraycopy(pcm, offset, out, HEADER_BYTES + 1 + cid.size, length)
        return out
    }

    /** Legge un frame intero. null = fine stream pulita (EOF prima dell'header). */
    fun readFrame(input: DataInputStream): Frame? {
        val kind = input.read()
        if (kind < 0) return null
        val len = input.readInt()   // big-endian; > 2^31 diventa negativo → rifiutato
        if (len < 0 || len > MAX_FRAME) throw IOException("frame troppo grande: $len")
        val payload = ByteArray(len)
        input.readFully(payload)
        return when (kind) {
            JSON -> Frame.Json(String(payload, Charsets.UTF_8))
            AUDIO -> {
                if (payload.isEmpty()) throw IOException("frame audio vuoto")
                val n = payload[0].toInt() and 0xFF
                if (1 + n > payload.size) throw IOException("call_id oltre il payload")
                Frame.Audio(
                    String(payload, 1, n, Charsets.UTF_8),
                    payload.copyOfRange(1 + n, payload.size),
                )
            }
            else -> throw IOException("tipo frame sconosciuto $kind")
        }
    }

    private fun writeHeader(out: ByteArray, kind: Int, len: Int) {
        out[0] = kind.toByte()
        out[1] = (len ushr 24).toByte()
        out[2] = (len ushr 16).toByte()
        out[3] = (len ushr 8).toByte()
        out[4] = len.toByte()
    }
}
