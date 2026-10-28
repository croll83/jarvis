package com.jarvis.voice.shared.wear

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream

class WatchStreamTest {

    private fun reader(vararg frames: ByteArray): WatchStream.Reader {
        val all = ByteArrayOutputStream()
        frames.forEach { all.write(it) }
        return WatchStream.Reader(ByteArrayInputStream(all.toByteArray()))
    }

    @Test fun roundTrip_ttsLiveClear_inOrder() {
        val tts = shortArrayOf(1, -1, 32767, -32768)
        val live = ShortArray(480) { (it * 37 - 9000).toShort() }
        val r = reader(
            WatchStream.tts(tts),
            WatchStream.live(live, 24_000, comms = false),
            WatchStream.clear(),
            WatchStream.live(shortArrayOf(5), 16_000, comms = true),
        )
        val a = r.next() as WatchStream.Frame.Tts
        assertArrayEquals(tts, a.pcm)
        val b = r.next() as WatchStream.Frame.Live
        assertArrayEquals(live, b.pcm)
        assertEquals(24_000, b.sampleRate)
        assertFalse(b.comms)
        assertEquals(WatchStream.Frame.Clear, r.next())
        val d = r.next() as WatchStream.Frame.Live
        assertEquals(16_000, d.sampleRate)
        assertTrue(d.comms)
        assertNull(r.next())   // fine stream
    }

    @Test fun header_isLittleEndianLength() {
        val f = WatchStream.live(ShortArray(320), 16_000, comms = false)
        assertEquals(4 + 640, f.size)
        assertEquals(WatchStream.TYPE_LIVE, f[0].toInt())
        assertEquals(16, f[1].toInt())
        assertEquals(640 and 0xFF, f[2].toInt() and 0xFF)
        assertEquals(640 shr 8, f[3].toInt())
    }

    @Test fun unknownType_payloadSkipped() {
        val unknown = byteArrayOf(9, 0, 3, 0, 1, 2, 3)
        val r = reader(unknown, WatchStream.clear())
        assertEquals(9, (r.next() as WatchStream.Frame.Unknown).type)
        assertEquals(WatchStream.Frame.Clear, r.next())
    }

    /** Il ChannelClient consegna a pezzi arbitrari: un byte alla volta deve andare uguale. */
    @Test fun fragmentedStream_reassembled() {
        val bytes = WatchStream.tts(ShortArray(960) { it.toShort() }) + WatchStream.clear()
        val oneByte = object : InputStream() {
            var i = 0
            override fun read(): Int = if (i < bytes.size) bytes[i++].toInt() and 0xFF else -1
            override fun read(b: ByteArray, off: Int, len: Int): Int {
                if (len == 0) return 0
                val v = read(); if (v < 0) return -1
                b[off] = v.toByte(); return 1
            }
        }
        val r = WatchStream.Reader(oneByte)
        assertEquals(960, (r.next() as WatchStream.Frame.Tts).pcm.size)
        assertEquals(WatchStream.Frame.Clear, r.next())
        assertNull(r.next())
    }

    @Test(expected = IllegalArgumentException::class)
    fun badRate_rejected() {
        WatchStream.live(ShortArray(1), 22_050, comms = false)
    }
}
