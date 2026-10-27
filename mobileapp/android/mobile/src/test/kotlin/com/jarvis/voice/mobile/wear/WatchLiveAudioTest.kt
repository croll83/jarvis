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
    private val port = WatchLiveAudio(
        send = { path, payload -> if (watchKnown) { sent += "$path:$payload"; true } else false },
        sink = {},
    )
    private val liveStart = "${DataLayer.PATH_LIVE}:${DataLayer.LIVE_START}"
    private val liveStop = "${DataLayer.PATH_LIVE}:${DataLayer.LIVE_STOP}"

    @Test fun prepareThenAccept_sendsStartOnce_thenStop() {
        port.prepare()
        assertEquals(listOf(liveStart), sent)
        assertTrue(port.start {})
        assertEquals(listOf(liveStart), sent)   // già in live dall'handshake
        port.clear()
        port.stop()
        assertEquals(listOf(liveStart, "${DataLayer.PATH_CLEAR}:", liveStop), sent)
        port.stop()   // idempotente
        assertEquals(3, sent.size)
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
}
