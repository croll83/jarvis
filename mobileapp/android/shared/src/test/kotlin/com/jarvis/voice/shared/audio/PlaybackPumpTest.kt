package com.jarvis.voice.shared.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class PlaybackPumpTest {

    @Test fun deliversEverything_in20msPieces() {
        val got = AtomicInteger()
        val maxPiece = AtomicInteger()
        val done = CountDownLatch(1)
        val pump = PlaybackPump(24_000, sink = { p ->
            maxPiece.accumulateAndGet(p.size) { a, b -> maxOf(a, b) }
            if (got.addAndGet(p.size) == 2_000) done.countDown()
        })
        pump.start()
        pump.offer(ShortArray(1_000)); pump.offer(ShortArray(7)); pump.offer(ShortArray(993))
        assertTrue(done.await(2, TimeUnit.SECONDS))
        assertEquals(480, maxPiece.get())
        pump.stop()
    }

    @Test fun clear_dropsQueuedAndRestOfChunk() {
        val got = AtomicInteger()
        val gate = CountDownLatch(1)
        val first = CountDownLatch(1)
        val pump = PlaybackPump(24_000, sink = { p ->
            got.addAndGet(p.size)
            first.countDown()
            gate.await()          // simula un AudioTrack con buffer pieno
        })
        pump.start()
        pump.offer(ShortArray(24_000))   // 1 s di parlato
        pump.offer(ShortArray(24_000))
        assertTrue(first.await(1, TimeUnit.SECONDS))
        pump.clear()                     // barge-in mentre il sink è bloccato
        gate.countDown()
        Thread.sleep(200)
        assertEquals(480, got.get())     // solo il pezzo già consegnato
        pump.offer(ShortArray(480))      // la sessione continua dopo il clear
        Thread.sleep(200)
        assertEquals(960, got.get())
        pump.stop()
    }

    @Test fun stalledSink_queueIsBounded_dropsOldest() {
        val gate = CountDownLatch(1)
        val first = CountDownLatch(1)
        val got = mutableListOf<Short>()
        val pump = PlaybackPump(1_000, sink = { p ->
            synchronized(got) { got.addAll(p.toList()) }
            first.countDown()
            gate.await()          // tunnel/AudioTrack fermo
        }, maxQueuedMs = 100)     // 100 campioni a 1 kHz
        pump.start()
        pump.offer(ShortArray(20) { 1 })     // finisce subito nel sink (bloccato)
        assertTrue(first.await(1, TimeUnit.SECONDS))
        for (v in 2..30) pump.offer(ShortArray(20) { v.toShort() })   // 580 campioni in arrivo
        assertTrue("queued=${pump.queued}", pump.queued in 1..100)
        gate.countDown()
        Thread.sleep(300)
        pump.stop()
        // resta la coda più recente: i chunk vecchi sono stati buttati, l'ultimo c'è
        val values = synchronized(got) { got.toList() }
        assertEquals(30.toShort(), values.last())
        assertTrue("delivered=${values.size}", values.size <= 20 + 100)
        assertTrue(values.drop(20).none { it in 2..24 })
    }

    @Test fun paced_staysNearRealTime() {
        val got = AtomicInteger()
        val pump = PlaybackPump(16_000, sink = { got.addAndGet(it.size) }, paceLeadMs = 100)
        pump.start()
        pump.offer(ShortArray(16_000))   // 1 s arrivato tutto insieme
        Thread.sleep(300)
        // ~300 ms trascorsi + 100 ms di anticipo ammesso → ben meno di 1 s consegnato
        val sent = got.get()
        assertTrue("sent=$sent", sent in 3_200..9_600)
        pump.stop()
    }

    /** Tunnel watch: il marker del clear non può finire prima di un pezzo vecchio in consegna. */
    @Test fun orderedClear_markerAfterInFlightPiece_noOldAudioAfter() {
        val log = mutableListOf<String>()
        val inSink = CountDownLatch(1)
        val gate = CountDownLatch(1)
        val pump = PlaybackPump(1_000, sink = { p ->
            if (p[0] == 1.toShort() && inSink.count > 0L) { inSink.countDown(); gate.await() }
            synchronized(log) { log += "a${p[0]}" }
        }, orderedClear = true)
        pump.start()
        pump.offer(ShortArray(200) { 1 })
        assertTrue(inSink.await(1, TimeUnit.SECONDS))   // primo pezzo bloccato nel sink
        val cleared = CountDownLatch(1)
        val t = Thread { pump.clear { synchronized(log) { log += "clear" } }; cleared.countDown() }
        t.start()
        Thread.sleep(100)
        assertEquals(1L, cleared.count)                 // il clear aspetta il pezzo in consegna
        gate.countDown()
        assertTrue(cleared.await(1, TimeUnit.SECONDS))
        pump.offer(ShortArray(20) { 2 })
        Thread.sleep(200)
        pump.stop()
        assertEquals(listOf("a1", "clear", "a2"), synchronized(log) { log.toList() })
    }

    @Test fun defaultClear_runsAfterClearImmediately() {
        var ran = false
        val pump = PlaybackPump(1_000, sink = {})
        pump.clear { ran = true }
        assertTrue(ran)
    }
}
