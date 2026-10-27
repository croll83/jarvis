package com.jarvis.voice.shared.audio

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Ricampionamento in streaming 24 kHz → 16 kHz (rapporto 3:2) per il parlato del modello
 * diretto allo watch, il cui player e il tunnel ChannelClient lavorano a 16 kHz.
 *
 * Passa-basso FIR (sinc finestrato Hamming, taglio 7 kHz) alla frequenza d'ingresso contro
 * l'aliasing, poi per ogni 3 campioni d'ingresso ne escono 2: t = 3k (campione esatto) e
 * t = 3k + 1.5 (media dei campioni 3k+1 e 3k+2). Lo stato tra un chunk e l'altro è
 * conservato, quindi N campioni in totale producono 2N/3 campioni indipendentemente
 * da come sono spezzati. Non thread-safe: un solo produttore.
 */
class Downsampler24To16 {

    private val hist = FloatArray(TAPS)
    private var pos = 0     // prossima cella libera del ring
    private var phase = 0   // indice dell'ingresso modulo 3
    private var prev = 0f   // uscita filtrata della fase 1, in attesa della fase 2

    fun process(input: ShortArray): ShortArray {
        val out = ShortArray(input.size * 2 / 3 + 2)
        var n = 0
        for (s in input) {
            hist[pos] = s.toFloat()
            pos = (pos + 1) % TAPS
            val y = filtered()
            when (phase) {
                0 -> out[n++] = clip(y)
                1 -> prev = y
                2 -> out[n++] = clip((prev + y) * 0.5f)
            }
            phase = (phase + 1) % 3
        }
        return out.copyOf(n)
    }

    fun reset() {
        hist.fill(0f); pos = 0; phase = 0; prev = 0f
    }

    private fun filtered(): Float {
        var acc = 0f
        var idx = pos   // il più vecchio
        for (k in TAPS - 1 downTo 0) {
            acc += COEF[k] * hist[idx]
            idx++; if (idx == TAPS) idx = 0
        }
        return acc
    }

    private fun clip(v: Float): Short = v.roundToInt().coerceIn(-32768, 32767).toShort()

    companion object {
        const val IN_RATE = 24_000
        const val OUT_RATE = 16_000
        private const val TAPS = 31
        private const val CUTOFF_HZ = 7_000.0

        private val COEF: FloatArray = run {
            val fc = CUTOFF_HZ / IN_RATE
            val m = (TAPS - 1) / 2.0
            val h = DoubleArray(TAPS) { i ->
                val x = i - m
                val sinc = if (x == 0.0) 2 * fc else sin(2 * PI * fc * x) / (PI * x)
                sinc * (0.54 - 0.46 * cos(2 * PI * i / (TAPS - 1)))
            }
            val sum = h.sum()
            FloatArray(TAPS) { (h[it] / sum).toFloat() }   // guadagno in continua = 1
        }
    }
}
