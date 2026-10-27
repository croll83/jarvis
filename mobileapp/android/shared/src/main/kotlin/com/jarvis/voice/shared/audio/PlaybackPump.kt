package com.jarvis.voice.shared.audio

import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * Coda del parlato del modello in sessione live, tra il socket dell'hub e il sink
 * (AudioTrack locale o tunnel verso lo watch).
 *
 * L'hub manda l'audio più veloce del tempo reale: senza questa coda la write bloccante
 * dell'AudioTrack fermerebbe il thread che legge il socket e il "clear" del barge-in
 * arriverebbe in ritardo. Qui i chunk vengono spezzati in pezzi da 20 ms e [clear] butta
 * subito la coda e il resto del chunk in corso.
 *
 * [paceLeadMs] = 0: il sink fa da freno (AudioTrack WRITE_BLOCKING). > 0: il sink non
 * frena (stream del tunnel watch), quindi si manda al massimo [paceLeadMs] di audio in
 * anticipo sul tempo reale, così al barge-in nel tunnel resta poca coda.
 *
 * La coda è limitata a [maxQueuedMs] di audio: se il sink si pianta (tunnel fermo) si
 * buttano i chunk più vecchi invece di crescere senza limite per tutta la risposta.
 */
class PlaybackPump(
    private val sampleRate: Int,
    private val sink: (ShortArray) -> Unit,
    private val paceLeadMs: Int = 0,
    private val onLevel: (Float) -> Unit = {},
    maxQueuedMs: Int = 30_000,
) {
    private val queue = LinkedBlockingQueue<ShortArray>()
    private val queuedSamples = AtomicInteger()
    private val maxQueuedSamples = (sampleRate.toLong() * maxQueuedMs / 1000).toInt().coerceAtLeast(1)
    private val generation = AtomicInteger()
    private val running = AtomicBoolean(false)
    private var worker: Thread? = null
    private val pieceSamples = sampleRate / 50   // 20 ms

    fun start() {
        if (!running.compareAndSet(false, true)) return
        worker = thread(name = "jarvis-live-play", isDaemon = true) { loop() }
    }

    fun offer(pcm: ShortArray) {
        if (!running.get() || pcm.isEmpty()) return
        queue.offer(pcm)
        var total = queuedSamples.addAndGet(pcm.size)
        // Oltre il tetto: via i chunk più vecchi (mai quello appena arrivato).
        while (total > maxQueuedSamples && queue.size > 1) {
            val old = queue.poll() ?: break
            total = queuedSamples.addAndGet(-old.size)
        }
    }

    /** Campioni in coda non ancora passati al sink (escluso il chunk in corso). */
    val queued: Int get() = queuedSamples.get().coerceAtLeast(0)

    /** Barge-in: scarta tutto ciò che non è ancora passato al sink. */
    fun clear() {
        generation.incrementAndGet()
        queue.clear()
        queuedSamples.set(0)
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        clear()
        worker?.interrupt()
        worker?.join(500)
        worker = null
        onLevel(0f)
    }

    private fun loop() {
        var deadline = 0L   // istante (nanoTime) in cui finisce di suonare quanto già mandato
        var lastGen = generation.get()
        var idle = true
        try {
            while (running.get()) {
                val chunk = queue.poll(IDLE_POLL_MS, TimeUnit.MILLISECONDS)
                if (chunk == null) {
                    if (!idle) { onLevel(0f); idle = true }
                    continue
                }
                queuedSamples.addAndGet(-chunk.size)
                idle = false
                val gen = generation.get()
                if (gen != lastGen) { deadline = 0L; lastGen = gen }
                var off = 0
                while (off < chunk.size && running.get() && gen == generation.get()) {
                    val n = minOf(pieceSamples, chunk.size - off)
                    if (paceLeadMs > 0) {
                        val now = System.nanoTime()
                        if (deadline < now) deadline = now
                        val ahead = deadline - now - paceLeadMs * 1_000_000L
                        if (ahead > 0) Thread.sleep(ahead / 1_000_000L, (ahead % 1_000_000L).toInt())
                        if (gen != generation.get()) break
                        deadline += n * 1_000_000_000L / sampleRate
                    }
                    val piece = chunk.copyOfRange(off, off + n)
                    sink(piece)
                    onLevel(Pcm.level(piece))
                    off += n
                }
            }
        } catch (_: InterruptedException) {
            // stop()
        }
    }

    private companion object {
        const val IDLE_POLL_MS = 120L
    }
}
