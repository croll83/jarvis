package com.jarvis.voice.shared.audio

import kotlin.math.PI
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Ricampionamento in streaming 24 kHz → 16 kHz (rapporto 3:2) per il parlato del modello
 * diretto allo watch.
 *
 * Polifase classico: sovracampionamento x2 (48 kHz), passa-basso FIR sinc con finestra di
 * Kaiser ([TAPS] coefficienti a 48 kHz, taglio [CUTOFF_HZ]), decimazione /3. Ogni campione
 * d'uscita usa uno dei due rami polifase (metà dei coefficienti), normalizzati a guadagno 1
 * in continua ciascuno. Risposta piatta fino a ~6.5 kHz, oltre 8.3 kHz attenuata di ~80 dB:
 * niente immagini in banda né aliasing udibile (la versione precedente, 31 tap con una
 * media a due campioni, rimandava 5–7 kHz su 1–3 kHz a -20 dB: il suono "ruvido").
 *
 * Lo stato tra un chunk e l'altro è conservato: N campioni producono 2N/3 campioni
 * indipendentemente da come sono spezzati. Non thread-safe: un solo produttore.
 */
class Downsampler24To16 {

    private val hist = FloatArray(HALF)   // ultimi HALF ingressi, ring
    private var pos = 0                   // cella del più recente
    private var phase = 0                 // indice dell'ingresso modulo 3

    fun process(input: ShortArray): ShortArray {
        val out = ShortArray(input.size * 2 / 3 + 2)
        var n = 0
        for (s in input) {
            pos = if (pos + 1 == HALF) 0 else pos + 1
            hist[pos] = s.toFloat()
            // Uscita m all'istante 1.5·m dell'ingresso: m pari esce con l'ingresso 3q (ramo 0),
            // m dispari con l'ingresso 3q+1 (ramo 1); l'ingresso 3q+2 non produce uscite.
            when (phase) {
                0 -> out[n++] = clip(branch(PHASE0))
                1 -> out[n++] = clip(branch(PHASE1))
            }
            phase = if (phase == 2) 0 else phase + 1
        }
        return out.copyOf(n)
    }

    fun reset() {
        hist.fill(0f); pos = 0; phase = 0
    }

    /** Σ c[i] · x[k-i], i = 0 è il campione più recente. */
    private fun branch(c: FloatArray): Float {
        var acc = 0f
        var idx = pos
        for (i in 0 until HALF) {
            acc += c[i] * hist[idx]
            idx = if (idx == 0) HALF - 1 else idx - 1
        }
        return acc
    }

    private fun clip(v: Float): Short = v.roundToInt().coerceIn(-32768, 32767).toShort()

    companion object {
        const val IN_RATE = 24_000
        const val OUT_RATE = 16_000
        private const val UP_RATE = 48_000
        private const val TAPS = 144
        private const val HALF = TAPS / 2
        private const val CUTOFF_HZ = 7_500.0
        private const val KAISER_BETA = 8.0

        private val PROTO: DoubleArray = run {
            val fc = CUTOFF_HZ / UP_RATE
            val m = (TAPS - 1) / 2.0
            val i0b = besselI0(KAISER_BETA)
            DoubleArray(TAPS) { n ->
                val x = n - m
                val sinc = if (x == 0.0) 2 * fc else sin(2 * PI * fc * x) / (PI * x)
                val r = x / m
                sinc * besselI0(KAISER_BETA * sqrt((1 - r * r).coerceAtLeast(0.0))) / i0b
            }
        }

        /** Ramo polifase [p] (0/1): coefficienti h[p + 2i], normalizzati a somma 1. */
        private fun branchOf(p: Int): FloatArray {
            val c = DoubleArray(HALF) { i -> PROTO[p + 2 * i] }
            val sum = c.sum()
            return FloatArray(HALF) { (c[it] / sum).toFloat() }
        }

        private val PHASE0 = branchOf(0)
        private val PHASE1 = branchOf(1)

        private fun besselI0(x: Double): Double {
            var sum = 1.0
            var term = 1.0
            val q = x * x / 4
            var k = 1
            while (k < 50) {
                term *= q / (k.toDouble() * k)
                sum += term
                if (term < 1e-12 * sum) break
                k++
            }
            return sum
        }
    }
}
