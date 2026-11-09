package com.jarvis.voice.shared.hub

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.concurrent.thread
import kotlin.math.PI
import kotlin.math.sin
import kotlin.random.Random

/** Buffer del comando dopo il tap e VAD a energia (19.12, D1). */
class CommandBufferTest {

    private val rnd = Random(7)

    /** Frame da 20 ms: rumore di stanza (~-60 dBFS). */
    private fun noise(): ByteArray = frame { rnd.nextInt(-40, 41) }

    /** Frame da 20 ms: "voce" (sinusoide a ~-20 dBFS). */
    private var phase = 0
    private fun voice(): ByteArray = frame { (3_000 * sin(2 * PI * 220 * (phase++) / 16_000.0)).toInt() }

    private fun silence(): ByteArray = ByteArray(640)

    private fun frame(sample: () -> Int): ByteArray {
        val out = ByteArray(640)
        for (i in 0 until 320) {
            val v = sample()
            out[i * 2] = (v and 0xFF).toByte()
            out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    private fun CommandBuffer.feed(n: Int, f: () -> ByteArray) = repeat(n) { add(f()) }

    @Test fun onlyRoomNoise_isNoCommand() {
        val b = CommandBuffer()
        b.feed(75) { noise() }   // 1,5 s
        assertTrue(b.speechMs() < 300)
        assertFalse(b.hasCommand())
    }

    @Test fun speechAfterTap_isCommand() {
        val b = CommandBuffer()
        b.feed(25) { noise() }   // 500 ms di reazione dopo il tap
        b.feed(30) { voice() }   // 600 ms di parlato
        b.feed(10) { noise() }
        assertEquals(600, b.speechMs())
        assertTrue(b.hasCommand())
    }

    @Test fun shortBlip_isNoCommand() {
        val b = CommandBuffer()
        b.feed(30) { noise() }
        b.feed(10) { voice() }   // 200 ms: sotto i 300 ms
        b.feed(30) { noise() }
        assertFalse(b.hasCommand())
    }

    @Test fun firstMsSkipped_vibrationDoesNotCount() {
        val b = CommandBuffer()
        b.feed(10) { voice() }   // i primi 200 ms (vibrazione del tap): esclusi
        b.feed(60) { noise() }
        assertEquals(0, b.speechMs())
        assertFalse(b.hasCommand())
    }

    @Test fun digitalSilenceAtMicStart_doesNotLowerTheFloor() {
        // Mic che parte con zeri: senza escluderli il fondo andrebbe a -150 dB, la soglia a
        // -65 dBFS e il rumore della stanza conterebbe come parlato.
        val b = CommandBuffer(skipBytes = 0)
        b.feed(20) { silence() }
        b.feed(60) { noise() }
        assertFalse(b.hasCommand())
    }

    @Test fun cappedAtMaxBytes_keepsTheBeginning() {
        val b = CommandBuffer(maxBytes = 640 * 3)
        val first = voice()
        b.add(first)
        b.feed(5) { noise() }
        assertEquals(640 * 3, b.length)
        assertArrayEquals(first, b.snapshot().copyOfRange(0, 640))
    }

    @Test fun defaultCapIsEightSeconds() {
        val b = CommandBuffer()
        b.feed(500) { noise() }   // 10 s
        assertEquals(8 * 16_000 * 2, b.length)
    }

    @Test fun awaitCommand_returnsAsSoonAsSpeechArrives() {
        val b = CommandBuffer()
        b.feed(15) { noise() }
        val feeder = thread {
            Thread.sleep(150)
            b.feed(20) { voice() }
        }
        val t0 = System.currentTimeMillis()
        assertTrue(b.awaitCommand(t0 + 3_000))
        assertTrue(System.currentTimeMillis() - t0 < 1_500)
        feeder.join()
    }

    @Test fun awaitCommand_falseAtDeadline() {
        val b = CommandBuffer()
        b.feed(40) { noise() }
        val t0 = System.currentTimeMillis()
        assertFalse(b.awaitCommand(t0 + 250))
        assertTrue(System.currentTimeMillis() - t0 >= 240)
    }

    @Test fun percentile_matchesNumpyLinear() {
        assertEquals(1.9, CommandBuffer.percentile(listOf(1.0, 2.0, 3.0, 4.0, 10.0), 22.5), 1e-9)
        assertEquals(5.0, CommandBuffer.percentile(listOf(5.0), 10.0), 1e-9)
    }
}
