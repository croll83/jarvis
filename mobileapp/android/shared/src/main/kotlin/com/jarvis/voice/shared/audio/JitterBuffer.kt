package com.jarvis.voice.shared.audio

import java.util.ArrayDeque

/**
 * Buffer anti-jitter del parlato live che arriva allo watch dal tunnel Data Layer.
 *
 * Il thread di playout chiede SEMPRE un frame pieno con [read]: se non c'è parlato riceve
 * silenzio, così l'AudioTrack non resta mai a secco (niente standby dell'uscita, rampa di
 * volume e amplificatore da riaccendere a ogni risposta: era la testa tagliata).
 *
 * All'inizio di ogni raffica di parlato (e dopo un buco) si aspetta di avere [prebufferMs]
 * di audio, oppure che il primo pezzo aspetti da [prebufferMs]: risposte corte e code
 * partono comunque. Durante il parlato un frame incompleto esce subito completato da zeri,
 * quindi la fine di una risposta non resta mai incastrata nel buffer.
 *
 * Oltre [maxMs] di audio in coda (tunnel fermo e poi una raffica) si buttano i campioni
 * più vecchi. Thread-safe: push/clear da un thread, read da un altro.
 */
class JitterBuffer(
    sampleRate: Int,
    prebufferMs: Int = PREBUFFER_MS,
    maxMs: Int = 3_000,
) {
    private val prebuffer = (sampleRate.toLong() * prebufferMs / 1000).toInt()
    private val prebufferNanos = prebufferMs * 1_000_000L
    private val max = (sampleRate.toLong() * maxMs / 1000).toInt().coerceAtLeast(1)

    private val chunks = ArrayDeque<ShortArray>()
    private var headOff = 0          // campioni già consumati del primo chunk
    private var buffered = 0
    private var playing = false
    private var firstAt = 0L         // nanoTime del primo pezzo in attesa (in priming)

    /** Campioni in coda. */
    val size: Int get() = synchronized(this) { buffered }

    /** Parlato in coda o in riproduzione (per il drain a fine sessione). */
    val isEmpty: Boolean get() = synchronized(this) { buffered == 0 }

    fun push(pcm: ShortArray, now: Long = System.nanoTime()) = synchronized(this) {
        if (pcm.isEmpty()) return@synchronized
        if (buffered == 0 && !playing) firstAt = now
        chunks.addLast(pcm)
        buffered += pcm.size
        while (buffered > max) {
            val head = chunks.first()
            val left = head.size - headOff
            val drop = minOf(left, buffered - max)
            headOff += drop
            buffered -= drop
            if (headOff == head.size) { chunks.removeFirst(); headOff = 0 }
        }
    }

    /** Barge-in: via tutto, la prossima raffica riparte dal priming. */
    fun clear() = synchronized(this) {
        chunks.clear(); headOff = 0; buffered = 0; playing = false
    }

    /**
     * Riempie TUTTO [out]: parlato se pronto, zeri per il resto. Ritorna i campioni di
     * parlato scritti (0 = solo silenzio).
     */
    fun read(out: ShortArray, now: Long = System.nanoTime()): Int = synchronized(this) {
        if (!playing) {
            val ready = buffered >= prebuffer || (buffered > 0 && now - firstAt >= prebufferNanos)
            if (!ready) { out.fill(0); return@synchronized 0 }
            playing = true
        }
        var n = 0
        while (n < out.size && buffered > 0) {
            val head = chunks.first()
            val take = minOf(out.size - n, head.size - headOff)
            System.arraycopy(head, headOff, out, n, take)
            n += take; headOff += take; buffered -= take
            if (headOff == head.size) { chunks.removeFirst(); headOff = 0 }
        }
        if (n < out.size) out.fill(0, n, out.size)
        // A secco: fine raffica o buco del tunnel. La prossima raffica rifà il priming.
        if (buffered == 0) playing = false
        n
    }

    companion object {
        /** Riserva contro il jitter Bluetooth del ChannelClient, aggiunta a inizio risposta. */
        const val PREBUFFER_MS = 120
    }
}
