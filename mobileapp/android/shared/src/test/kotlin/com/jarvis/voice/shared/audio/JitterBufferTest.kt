package com.jarvis.voice.shared.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 1 kHz: 1 campione = 1 ms, prebuffer 100 ms = 100 campioni. Tempo in ms → nanos. */
class JitterBufferTest {

    private fun ms(v: Long) = v * 1_000_000L
    private val jb = JitterBuffer(1_000, prebufferMs = 100, maxMs = 1_000)
    private val out = ShortArray(20)

    @Test fun silenceWhenEmpty_fullFrameOfZeros() {
        out.fill(7)
        assertEquals(0, jb.read(out, ms(0)))
        assertTrue(out.all { it == 0.toShort() })
    }

    @Test fun primesUntilPrebuffer_thenPlaysEverything() {
        jb.push(ShortArray(60) { 1 }, ms(0))
        assertEquals(0, jb.read(out, ms(10)))          // 60 < 100 e aspetta da 10 ms
        jb.push(ShortArray(50) { 2 }, ms(20))
        assertEquals(20, jb.read(out, ms(20)))         // 110 ≥ 100: parte
        assertTrue(out.all { it == 1.toShort() })
        var total = 20
        while (true) { val n = jb.read(out, ms(30)); if (n == 0) break; total += n }
        assertEquals(110, total)                       // nessun campione perso
    }

    @Test fun shortUtterance_startsAfterPrebufferTime() {
        jb.push(ShortArray(30) { 3 }, ms(0))
        assertEquals(0, jb.read(out, ms(99)))
        assertEquals(20, jb.read(out, ms(100)))        // aspettato abbastanza: parte comunque
        assertEquals(10, jb.read(out, ms(120)))        // coda: frame parziale + zeri, subito
        assertTrue(out.copyOfRange(0, 10).all { it == 3.toShort() })
        assertTrue(out.copyOfRange(10, 20).all { it == 0.toShort() })
    }

    @Test fun underrun_rePrimes() {
        jb.push(ShortArray(100) { 1 }, ms(0))
        repeat(5) { assertEquals(20, jb.read(out, ms(0))) }
        assertEquals(0, jb.read(out, ms(10)))
        jb.push(ShortArray(20) { 2 }, ms(200))          // buco: nuova raffica
        assertEquals(0, jb.read(out, ms(250)))          // priming di nuovo
        assertEquals(20, jb.read(out, ms(300)))
    }

    @Test fun clear_dropsAndRePrimes() {
        jb.push(ShortArray(200) { 1 }, ms(0))
        assertEquals(20, jb.read(out, ms(0)))
        jb.clear()
        assertEquals(0, jb.read(out, ms(1)))
        assertTrue(jb.isEmpty)
        jb.push(ShortArray(100) { 2 }, ms(5))
        assertEquals(20, jb.read(out, ms(5)))
        assertTrue(out.all { it == 2.toShort() })
    }

    @Test fun overMax_dropsOldest() {
        jb.push(ShortArray(800) { 1 }, ms(0))
        jb.push(ShortArray(400) { 2 }, ms(0))
        assertEquals(1_000, jb.size)
        val first = ShortArray(200)
        jb.read(first, ms(0))
        assertTrue(first.all { it == 1.toShort() })   // buttati i 200 "1" più vecchi: ne restano 600
        val all = ShortArray(800)
        jb.read(all, ms(0))
        assertEquals(1.toShort(), all[399]); assertEquals(2.toShort(), all[400]); assertEquals(2.toShort(), all[799])
    }
}
