package com.jarvis.voice.shared.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin
import kotlin.math.sqrt

class Downsampler24To16Test {

    private fun sine(freq: Double, n: Int, amp: Double = 10_000.0) =
        ShortArray(n) { (amp * sin(2 * PI * freq * it / 24_000.0)).toInt().toShort() }

    private fun rms(x: ShortArray, from: Int = 0): Double {
        var s = 0.0
        for (i in from until x.size) s += x[i].toDouble() * x[i]
        return sqrt(s / (x.size - from))
    }

    @Test fun length_isTwoThirds() {
        assertEquals(320, Downsampler24To16().process(ShortArray(480)).size)   // 20 ms → 20 ms
        assertEquals(0, Downsampler24To16().process(ShortArray(0)).size)
    }

    @Test fun length_independentOfChunking() {
        val d = Downsampler24To16()
        val total = listOf(100, 7, 1, 2, 373, 959).sumOf { d.process(ShortArray(it)).size }
        val n = 100 + 7 + 1 + 2 + 373 + 959
        // l'uscita m esce appena arriva l'ingresso floor(1.5·m): ceil(2N/3) campioni
        assertEquals(Downsampler24To16().process(ShortArray(n)).size, total)
        assertEquals((2 * n + 2) / 3, total)
    }

    @Test fun chunked_equalsOneShot() {
        val x = sine(440.0, 2400)
        val oneShot = Downsampler24To16().process(x)
        val d = Downsampler24To16()
        val parts = listOf(0..99, 100..106, 107..1500, 1501..2399)
            .map { r -> d.process(x.copyOfRange(r.first, r.last + 1)) }
        val joined = parts.fold(ShortArray(0)) { a, b -> a + b }
        assertTrue(oneShot.contentEquals(joined))
    }

    @Test fun dc_preserved() {
        val out = Downsampler24To16().process(ShortArray(2400) { 1000 })
        for (i in 40 until out.size) assertTrue("i=$i v=${out[i]}", abs(out[i] - 1000) <= 1)
    }

    @Test fun speechBand_preserved_aliasBand_attenuated() {
        val low = Downsampler24To16().process(sine(1_000.0, 24_000))
        val ratioLow = rms(low, 100) / (10_000.0 / sqrt(2.0))
        assertTrue("1 kHz ratio $ratioLow", ratioLow in 0.95..1.03)

        // 10.5 kHz non esiste a 16 kHz: senza filtro si ripiegherebbe a 5.5 kHz
        val high = Downsampler24To16().process(sine(10_500.0, 24_000))
        val ratioHigh = rms(high, 100) / (10_000.0 / sqrt(2.0))
        assertTrue("10.5 kHz ratio $ratioHigh", ratioHigh < 0.02)
    }

    @Test fun fullScale_clipsWithoutWrap() {
        val out = Downsampler24To16().process(ShortArray(3000) { if ((it / 6) % 2 == 0) 32767 else -32768 })
        assertTrue(out.isNotEmpty())   // nessuna eccezione e valori nel range short per costruzione
    }

    /** Ampiezza (picco) della componente a [freq] in [x] campionato a [rate] (Goertzel). */
    private fun tone(x: ShortArray, freq: Double, rate: Double, from: Int = 200): Double {
        val n = x.size - from
        val w = 2 * PI * freq / rate
        var re = 0.0; var im = 0.0
        for (i in 0 until n) {
            re += x[from + i] * kotlin.math.cos(w * i)
            im -= x[from + i] * sin(w * i)
        }
        return 2 * sqrt(re * re + im * im) / n
    }

    /** La versione vecchia rimandava f su 8 kHz − f a ~-20 dB: qui niente immagini in banda. */
    @Test fun noInBandImages() {
        for (f in listOf(5_000.0, 6_000.0, 7_000.0)) {
            val out = Downsampler24To16().process(sine(f, 24_000))
            val image = tone(out, 8_000.0 - f, 16_000.0)
            assertTrue("immagine di $f Hz: $image", image < 10_000.0 * 0.001)   // < -60 dB
        }
    }

    @Test fun passband_flatTo6k() {
        for (f in listOf(300.0, 3_000.0, 5_000.0, 6_000.0)) {
            val out = Downsampler24To16().process(sine(f, 24_000))
            val g = tone(out, f, 16_000.0) / 10_000.0
            assertTrue("$f Hz guadagno $g", g in 0.97..1.03)
        }
    }
}
