package com.jarvis.voice.shared.hub

import com.jarvis.voice.shared.audio.Pcm
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.BufferedInputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/** HubClient contro un hub finto su ServerSocket locale (stesso framing di voice_live_hub). */
class HubClientTest {

    private val server = ServerSocket(0)
    private val events = LinkedBlockingQueue<String>()
    private val audio = LinkedBlockingQueue<ShortArray>()

    private val listener = object : HubClient.Listener {
        override fun onAccepted() { events.add("accepted") }
        override fun onAudio(pcm: ShortArray) { audio.add(pcm); events.add("audio") }
        override fun onClear() { events.add("clear") }
        override fun onEnded(reason: String) { events.add("ended:$reason") }
    }

    private class Peer(val sock: Socket) {
        val input = DataInputStream(BufferedInputStream(sock.getInputStream()))
        val out: OutputStream = sock.getOutputStream()
        fun read(): HubIpc.Frame? = HubIpc.readFrame(input)
        fun json(): JSONObject = JSONObject((read() as HubIpc.Frame.Json).text)
        fun send(json: String) { out.write(HubIpc.encodeJson(json)); out.flush() }
        fun sendAudio(callId: String, pcm: ByteArray) { out.write(HubIpc.encodeAudio(callId, pcm)); out.flush() }
    }

    private fun client(acceptTimeoutMs: Int = 3_000, port: Int = server.localPort) = HubClient(
        host = "127.0.0.1", port = port, callId = CALL,
        offerJson = HubLiveStart.Start(CALL, "hermes-marco", "Marco", "", "napoli", "studio",
            listOf("orchestrator"), "127.0.0.1", port).offerJson(),
        listener = listener, connectTimeoutMs = 2_000, acceptTimeoutMs = acceptTimeoutMs,
    )

    private fun accept(): Peer {
        server.soTimeout = 3_000
        return Peer(server.accept().also { it.soTimeout = 3_000 })
    }

    private fun next(): String? = events.poll(3, TimeUnit.SECONDS)

    @After fun tearDown() { server.close() }

    @Test fun fullSession_offerAcceptAudioClearHangup() {
        val c = client()
        c.start()
        val hub = accept()

        val offer = hub.json()
        assertEquals("offer", offer.getString("type"))
        assertEquals(CALL, offer.getString("call_id"))
        assertEquals("hermes-marco", offer.getString("profile"))
        assertEquals("Marco", offer.getString("name"))
        assertEquals("napoli", offer.getString("location_id"))
        assertEquals("studio", offer.getString("room"))
        assertEquals("orchestrator", offer.getJSONArray("deliver").getString(0))
        assertTrue(!offer.has("voice"))   // vuoto → non mandato

        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals("accepted", next())
        assertEquals(HubClient.State.ACTIVE, c.state)

        // uplink: mic 16 kHz → frame AUDIO col nostro call_id
        val mic = ByteArray(640) { (it % 251).toByte() }
        c.sendAudio(mic)
        val up = hub.read() as HubIpc.Frame.Audio
        assertEquals(CALL, up.callId)
        assertArrayEquals(mic, up.pcm)

        // downlink: audio di un'altra chiamata ignorato, il nostro consegnato in ShortArray
        hub.sendAudio("other", ByteArray(8))
        val speech = ShortArray(480) { (it * 37 - 9000).toShort() }
        hub.sendAudio(CALL, Pcm.toBytes(speech))
        assertEquals("audio", next())
        assertArrayEquals(speech, audio.poll(1, TimeUnit.SECONDS))
        assertNull(audio.poll(100, TimeUnit.MILLISECONDS))

        hub.send("""{"type":"clear","call_id":"$CALL"}""")
        assertEquals("clear", next())

        hub.send("""{"type":"hangup","call_id":"$CALL"}""")
        assertEquals("ended:${HubClient.REASON_HANGUP}", next())
        assertEquals(HubClient.State.CLOSED, c.state)
        // il client chiude il socket: l'hub vede la fine dello stream
        assertNull(runCatching { hub.read() }.getOrNull())

        c.end()   // già chiuso: no-op, nessun altro callback
        c.sendAudio(mic)
        assertNull(events.poll(200, TimeUnit.MILLISECONDS))
    }

    @Test fun rejectedWithoutCallId_isAbort() {
        val c = client()
        c.start()
        val hub = accept()
        hub.json()
        hub.send("""{"type":"rejected","reason":"offer needs call_id, profile and name"}""")
        assertEquals("ended:${HubClient.REASON_REJECTED}", next())
        assertEquals(HubClient.State.CLOSED, c.state)
    }

    @Test fun silentCloseBeforeAccept_isRejected() {
        val c = client()
        c.start()
        val hub = accept()
        hub.json()
        hub.sock.close()   // es. call_id duplicato: l'hub chiude senza rispondere
        assertEquals("ended:${HubClient.REASON_REJECTED}", next())
    }

    @Test fun connectionRefused_isUnreachable() {
        val dead = ServerSocket(0).also { it.close() }.localPort
        client(port = dead).start()
        assertEquals("ended:${HubClient.REASON_UNREACHABLE}", next())
    }

    @Test fun noAnswerToOffer_isUnreachable() {
        val c = client(acceptTimeoutMs = 300)
        c.start()
        val hub = accept()
        hub.json()
        assertEquals("ended:${HubClient.REASON_UNREACHABLE}", next())
        assertEquals(HubClient.State.CLOSED, c.state)
    }

    @Test fun endByDevice_sendsEndedAndCloses_noCallback() {
        val c = client()
        c.start()
        val hub = accept()
        hub.json()
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals("accepted", next())

        c.end()
        val ended = hub.json()
        assertEquals("ended", ended.getString("type"))
        assertEquals(CALL, ended.getString("call_id"))
        assertNull(runCatching { hub.read() }.getOrNull())
        assertNull(events.poll(300, TimeUnit.MILLISECONDS))
        assertEquals(HubClient.State.CLOSED, c.state)
    }

    @Test fun endWithDeadHub_closesSocketAnyway() {
        // Hub che non legge più (rete morta): il writer resta bloccato in write, ma end()
        // chiude comunque il socket dopo la grazia invece di aspettare il timeout TCP.
        fun txThreads() = Thread.getAllStackTraces().keys.filter { it.name == "jarvis-hub-tx" && it.isAlive }.toSet()
        val before = txThreads()
        server.receiveBufferSize = 4_096
        val c = HubClient(
            host = "127.0.0.1", port = server.localPort, callId = CALL,
            offerJson = """{"type":"offer","call_id":"$CALL","profile":"p","name":"n"}""",
            listener = listener, connectTimeoutMs = 2_000, acceptTimeoutMs = 3_000, endGraceMs = 200,
        )
        c.start()
        val hub = accept()
        hub.json()
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals("accepted", next())
        val tx = (txThreads() - before).single()

        // l'hub non legge: si riempiono i buffer del kernel finché il writer si blocca
        val frame = ByteArray(640)
        val until = System.currentTimeMillis() + 1_500
        while (System.currentTimeMillis() < until) {
            repeat(50) { c.sendAudio(frame) }
            Thread.sleep(1)
        }
        Thread.sleep(300)
        assertEquals("writer bloccato in write", Thread.State.RUNNABLE, tx.state)

        c.end()
        tx.join(3_000)
        assertTrue("writer ancora bloccato dopo end()", !tx.isAlive)
        assertNull(events.poll(200, TimeUnit.MILLISECONDS))
        hub.sock.close()
    }

    @Test fun dropMidSession_isError() {
        val c = client()
        c.start()
        val hub = accept()
        hub.json()
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals("accepted", next())
        hub.sock.close()
        assertEquals("ended:${HubClient.REASON_ERROR}", next())
    }

    @Test fun clearForOtherCall_ignored_andAcceptWrongCallIgnored() {
        val c = client()
        c.start()
        val hub = accept()
        hub.json()
        hub.send("""{"type":"accept","call_id":"zzz"}""")
        assertNull(events.poll(200, TimeUnit.MILLISECONDS))
        assertEquals(HubClient.State.OFFERED, c.state)
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals("accepted", next())
        hub.send("""{"type":"clear","call_id":"zzz"}""")
        hub.send("""{"type":"hangup","call_id":"zzz"}""")
        assertNull(events.poll(200, TimeUnit.MILLISECONDS))
        c.end()
    }

    private companion object {
        const val CALL = "a1b2c3d4"
    }
}
