package com.jarvis.voice.shared.hub

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.DataInputStream
import java.io.IOException

class HubIpcTest {

    private fun hex(s: String) = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    private fun stream(b: ByteArray) = DataInputStream(ByteArrayInputStream(b))

    // Byte attesi generati da voice_live_hub/ipc.py (encode_audio / encode_json).
    @Test fun encodeAudio_matchesHubBytes() {
        val got = HubIpc.encodeAudio("ab12cd34", byteArrayOf(1, 2, 0xff.toByte(), 0x7f))
        assertArrayEquals(hex("020000000d0861623132636433340102ff7f"), got)
    }

    @Test fun encodeJson_matchesHubBytes() {
        val got = HubIpc.encodeJson("""{"type":"clear","call_id":"ab12cd34"}""")
        assertArrayEquals(
            hex("01000000257b2274797065223a22636c656172222c2263616c6c5f6964223a226162313263643334227d"),
            got,
        )
    }

    @Test fun decode_hubFrames() {
        val audio = HubIpc.readFrame(stream(hex("020000000d0861623132636433340102ff7f"))) as HubIpc.Frame.Audio
        assertEquals("ab12cd34", audio.callId)
        assertArrayEquals(byteArrayOf(1, 2, 0xff.toByte(), 0x7f), audio.pcm)

        // JSON con escape unicode dal json.dumps Python
        val json = HubIpc.readFrame(
            stream(hex("01000000247b2274797065223a22616363657074222c2263616c6c5f6964223a225c7530306539227d"))
        ) as HubIpc.Frame.Json
        assertEquals("é", org.json.JSONObject(json.text).getString("call_id"))
    }

    @Test fun roundTrip_sequenceThenEof() {
        val pcm = ByteArray(640) { it.toByte() }
        val bytes = HubIpc.encodeJson("""{"type":"offer","call_id":"x1"}""") +
            HubIpc.encodeAudio("x1", pcm) +
            HubIpc.encodeAudio("x1", ByteArray(0))
        val input = stream(bytes)
        val a = HubIpc.readFrame(input) as HubIpc.Frame.Json
        assertEquals("""{"type":"offer","call_id":"x1"}""", a.text)
        val b = HubIpc.readFrame(input) as HubIpc.Frame.Audio
        assertEquals("x1", b.callId)
        assertArrayEquals(pcm, b.pcm)
        val c = HubIpc.readFrame(input) as HubIpc.Frame.Audio
        assertEquals(0, c.pcm.size)
        assertNull(HubIpc.readFrame(input))
    }

    @Test fun encodeAudio_slice() {
        val pcm = byteArrayOf(9, 9, 1, 2, 3, 4, 9)
        val got = HubIpc.encodeAudio("c", pcm, 2, 4)
        assertArrayEquals(byteArrayOf(2, 0, 0, 0, 6, 1, 'c'.code.toByte(), 1, 2, 3, 4), got)
    }

    @Test fun oversizeFrame_rejected() {
        val head = byteArrayOf(1, 0, 0x10, 0, 1)   // 1 MiB + 1
        assertTrue(runCatching { HubIpc.readFrame(stream(head)) }.exceptionOrNull() is IOException)
        val huge = byteArrayOf(2, 0xff.toByte(), 0xff.toByte(), 0xff.toByte(), 0xff.toByte())
        assertTrue(runCatching { HubIpc.readFrame(stream(huge)) }.exceptionOrNull() is IOException)
    }

    @Test fun unknownKind_andTruncated_rejected() {
        assertTrue(runCatching { HubIpc.readFrame(stream(byteArrayOf(3, 0, 0, 0, 0))) }.isFailure)
        // header dice 10 byte ma ne arrivano 2
        assertTrue(runCatching { HubIpc.readFrame(stream(byteArrayOf(1, 0, 0, 0, 10, 1, 2))) }.isFailure)
        // id_len oltre il payload
        assertTrue(runCatching { HubIpc.readFrame(stream(byteArrayOf(2, 0, 0, 0, 2, 5, 'a'.code.toByte()))) }.isFailure)
    }
}
