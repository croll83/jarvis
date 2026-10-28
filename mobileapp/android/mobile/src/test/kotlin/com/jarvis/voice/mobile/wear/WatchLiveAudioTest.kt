package com.jarvis.voice.mobile.wear

import com.jarvis.voice.shared.wear.DataLayer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Messaggi di modalità live verso lo watch: start già all'apertura, stop sempre a fine sessione. */
class WatchLiveAudioTest {

    private val sent = mutableListOf<String>()
    private var watchKnown = true

    /** Tunnel finto: registra in ordine audio (rate/stream/campioni) e clear in banda. */
    private class FakeTunnel(override var framed: Boolean = true) : WatchLiveAudio.Tunnel {
        val events = mutableListOf<String>()
        val samples = mutableListOf<Short>()
        override fun live(pcm: ShortArray, sampleRate: Int, comms: Boolean) = synchronized(this) {
            events += "live:$sampleRate:${if (comms) "comms" else "media"}:${pcm.size}"
            samples.addAll(pcm.toList())
            Unit
        }
        override fun clear(): Boolean = synchronized(this) {
            if (!framed) return false
            events += "clear"
            true
        }
        fun snapshot() = synchronized(this) { events.toList() }
    }

    private val tunnel = FakeTunnel()
    private var fullRate = false
    private var media = true
    private val port = WatchLiveAudio(
        send = { path, payload -> if (watchKnown) { sent += "$path:$payload"; true } else false },
        tunnel = tunnel,
        fullRate = { fullRate },
        mediaUsage = { media },
    )
    private val liveStart = "${DataLayer.PATH_LIVE}:${DataLayer.LIVE_START}"
    private val liveStop = "${DataLayer.PATH_LIVE}:${DataLayer.LIVE_STOP}"

    private fun awaitEvents(n: Int) {
        val until = System.currentTimeMillis() + 2_000
        while (tunnel.snapshot().size < n && System.currentTimeMillis() < until) Thread.sleep(5)
    }

    @Test fun prepareThenAccept_sendsStartOnce_thenStop_clearInBand() {
        port.prepare()
        assertEquals(listOf(liveStart), sent)
        assertTrue(port.start {})
        assertEquals(listOf(liveStart), sent)   // già in live dall'handshake
        port.clear()
        port.stop()
        // canale a frame: il clear va nel tunnel, nessun PATH_CLEAR
        assertEquals(listOf(liveStart, liveStop), sent)
        assertEquals(listOf("clear"), tunnel.snapshot())
        port.stop()   // idempotente
        assertEquals(2, sent.size)
    }

    @Test fun legacyWatch_clearFallsBackToMessage() {
        tunnel.framed = false
        assertTrue(port.start {})
        port.clear()
        port.stop()
        assertEquals(listOf(liveStart, "${DataLayer.PATH_CLEAR}:", liveStop), sent)
    }

    @Test fun preparedButRejected_stillSendsStop() {
        port.prepare()
        port.stop()   // hub_rejected / unreachable: mai arrivato l'accept
        assertEquals(listOf(liveStart, liveStop), sent)
    }

    @Test fun stopWithoutSession_sendsNothing() {
        port.stop()
        assertTrue(sent.isEmpty())
    }

    @Test fun noWatch_startFails() {
        watchKnown = false
        port.prepare()
        assertFalse(port.start {})
        port.stop()
        assertTrue(sent.isEmpty())
    }

    @Test fun startWithoutPrepare_sendsStart() {
        assertTrue(port.start {})
        port.stop()
        assertEquals(listOf(liveStart, liveStop), sent)
    }

    @Test fun default_16kMedia_resampled() {
        assertTrue(port.start {})
        port.play(ShortArray(480))           // 20 ms a 24 kHz
        awaitEvents(1)
        assertEquals(listOf("live:16000:media:320"), tunnel.snapshot())
        port.stop()
    }

    @Test fun fullRateAndComms_passThrough24k() {
        fullRate = true; media = false
        assertTrue(port.start {})
        val pcm = ShortArray(480) { it.toShort() }
        port.play(pcm)
        awaitEvents(1)
        assertEquals(listOf("live:24000:comms:480"), tunnel.snapshot())
        assertEquals(pcm.toList(), synchronized(tunnel) { tunnel.samples.toList() })   // nessun ricampionamento
        port.stop()
    }

    @Test fun legacyWatch_always16k() {
        tunnel.framed = false; fullRate = true
        assertTrue(port.start {})
        port.play(ShortArray(480))
        awaitEvents(1)
        assertEquals("live:16000", tunnel.snapshot()[0].substringBeforeLast(":").substringBeforeLast(":"))
        port.stop()
    }

    @Test fun clear_isOrderedAfterOldAudio_andBeforeNewAudio() {
        assertTrue(port.start {})
        port.play(ShortArray(24_000) { 1 })  // 1 s: il pacing ne manda solo l'anticipo
        Thread.sleep(100)
        port.clear()
        port.play(ShortArray(480) { 2 })
        Thread.sleep(300)
        port.stop()
        val ev = tunnel.snapshot()
        val clearAt = ev.indexOf("clear")
        assertTrue("clear mancante: $ev", clearAt > 0)
        assertEquals(1, ev.count { it == "clear" })
        // dopo il clear solo il pezzo nuovo (20 ms → 320 campioni a 16 kHz)
        assertEquals(listOf("live:16000:media:320"), ev.drop(clearAt + 1))
        assertTrue(clearAt <= 1 + (160 + 100 + 60) / 20)   // solo l'anticipo del tunnel prima del clear
    }
}
