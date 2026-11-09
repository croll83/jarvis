package com.jarvis.voice.shared.hub

import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.CountDownLatch

/**
 * Fase 3 lato shared: offer_extra copiato nell'offer (19.2), HubClient con l'offer deciso dopo
 * la connect, buffer del comando prima del mic dal vivo, annunci e motivo del rifiuto.
 */
class HubPhase3Test {

    // ── HubLiveStart: req_id, origin, offer_extra ────────────────────────────

    private fun tapStart(extra: String? = """{"origin":"tap","identified":true,"command":false,"quick":true}""") =
        JSONObject("""
            {"type":"live_session_start","backend":"hub","call_id":"c1","profile":"hermes-marco","name":"Marco",
             "voice":"","location_id":"napoli","room":"Unknown","deliver":["orchestrator"],
             "hub":{"host":"10.0.0.1","port":9100},"origin":"tap","identified":true,"req_id":"r123"
             ${if (extra != null) ""","offer_extra":$extra""" else ""}}
        """.trimIndent())

    @Test fun parse_readsReqIdOriginAndOfferExtra() {
        val s = HubLiveStart.parse(tapStart()) as HubLiveStart.Start
        assertEquals("r123", s.reqId)
        assertEquals("tap", s.origin)
        assertTrue(s.fromRequest)
        assertEquals(true, JSONObject(s.offerExtra!!).getBoolean("quick"))
    }

    @Test fun offer_copiesOfferExtraNestedAndAtTopLevel_withCommandFromPeer() {
        val s = HubLiveStart.parse(tapStart()) as HubLiveStart.Start
        val offer = JSONObject(s.offerJson(command = true))
        val extra = offer.getJSONObject("offer_extra")
        assertEquals("tap", extra.getString("origin"))
        assertTrue(extra.getBoolean("identified"))
        assertTrue(extra.getBoolean("command"))   // il VAD del peer sovrascrive quello dell'orchestrator
        assertTrue(extra.getBoolean("quick"))
        // chiavi anche al primo livello, come il wakeword-server
        assertEquals("tap", offer.getString("origin"))
        assertTrue(offer.getBoolean("command"))
        assertTrue(offer.getBoolean("quick"))
        assertEquals("offer", offer.getString("type"))
        assertEquals("c1", offer.getString("call_id"))
        assertEquals("Marco", offer.getString("name"))
    }

    @Test fun offer_extraCannotOverrideProtectedOrExistingKeys() {
        val s = HubLiveStart.parse(tapStart("""{"origin":"tap","type":"evil","call_id":"x","name":"Mallory"}""")) as HubLiveStart.Start
        val offer = JSONObject(s.offerJson(command = false))
        assertEquals("offer", offer.getString("type"))
        assertEquals("c1", offer.getString("call_id"))
        assertEquals("Marco", offer.getString("name"))
        assertFalse(offer.getBoolean("command"))
    }

    @Test fun offer_tapWithoutOfferExtra_stillCarriesOrigin() {
        val s = HubLiveStart.parse(tapStart(extra = null)) as HubLiveStart.Start
        val offer = JSONObject(s.offerJson(command = false))
        assertEquals("tap", offer.getJSONObject("offer_extra").getString("origin"))
        assertEquals("tap", offer.getString("origin"))
    }

    @Test fun offer_withoutPhase3Fields_isTodays() {
        val s = HubLiveStart.Start("c1", "hermes-marco", "Marco", "", "napoli", "studio",
            listOf("orchestrator"), "127.0.0.1", 9100)
        val offer = JSONObject(s.offerJson())
        assertFalse(s.fromRequest)
        assertFalse(offer.has("offer_extra"))
        assertFalse(offer.has("origin"))
        assertFalse(offer.has("command"))
    }

    @Test fun phraseOrigin_isNotARequest() {
        val msg = tapStart("""{"origin":"phrase","identified":true,"command":false,"quick":false}""")
            .put("origin", "phrase").apply { remove("req_id") }
        val s = HubLiveStart.parse(msg) as HubLiveStart.Start
        assertFalse(s.fromRequest)
        assertEquals("", s.reqId)
        assertEquals("phrase", JSONObject(s.offerJson()).getJSONObject("offer_extra").getString("origin"))
    }

    // ── HubClient ────────────────────────────────────────────────────────────

    private val server = ServerSocket(0)
    private val events = LinkedBlockingQueue<String>()
    private val listener = object : HubClient.Listener {
        override fun onAccepted() { events.add("accepted") }
        override fun onAudio(pcm: ShortArray) {}
        override fun onClear() {}
        override fun onEnded(reason: String) { events.add("ended:$reason") }
    }

    @After fun tearDown() { server.close() }

    private class Peer(val sock: Socket) {
        val input = DataInputStream(BufferedInputStream(sock.getInputStream()))
        val out: OutputStream = sock.getOutputStream()
        fun read(): HubIpc.Frame? = HubIpc.readFrame(input)
        fun json(): JSONObject = JSONObject((read() as HubIpc.Frame.Json).text)
        fun send(json: String) { out.write(HubIpc.encodeJson(json)); out.flush() }
    }

    private fun accept(): Peer {
        server.soTimeout = 3_000
        return Peer(server.accept().also { it.soTimeout = 3_000 })
    }

    @Test fun offerProvider_runsAfterConnect_beforeTheOffer() {
        // Il provider aspetta che il socket sia già accettato dall'hub: se girasse prima della
        // connect, la connect non partirebbe e l'accept qui sotto scadrebbe.
        val connected = CountDownLatch(1)
        val c = HubClient("127.0.0.1", server.localPort, CALL, {
            val after = connected.await(2, TimeUnit.SECONDS)
            """{"type":"offer","call_id":"$CALL","profile":"p","name":"n","command":$after}"""
        }, listener, connectTimeoutMs = 2_000, acceptTimeoutMs = 3_000)
        c.start()
        val hub = accept()
        connected.countDown()
        assertTrue(hub.json().getBoolean("command"))
        c.end()
    }

    @Test fun prime_sendsTheWholeBufferInOrder_beforeLiveAudio() {
        val c = HubClient("127.0.0.1", server.localPort, CALL, """{"type":"offer","call_id":"$CALL"}""",
            listener, connectTimeoutMs = 2_000, acceptTimeoutMs = 3_000)
        assertEquals(0, c.prime(ByteArray(10)))   // prima dell'accept: niente
        c.start()
        val hub = accept()
        hub.json()
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals("accepted", events.poll(3, TimeUnit.SECONDS))
        // 6 s di buffer: oltre la coda di sendAudio (100 frame), non deve perdere l'inizio
        val buffer = ByteArray(6 * 32_000) { (it % 251).toByte() }
        assertEquals(buffer.size, c.prime(buffer))
        val live = ByteArray(640) { 7 }
        c.sendAudio(live)
        val got = ByteArrayOutputStream()
        while (got.size() < buffer.size + live.size) {
            val f = hub.read() as HubIpc.Frame.Audio
            assertEquals(CALL, f.callId)
            assertTrue(f.pcm.size <= 3_200)
            got.write(f.pcm)
        }
        assertArrayEquals(buffer + live, got.toByteArray())
        c.end()
    }

    @Test fun sendControl_announceOnlyWhenActive() {
        val c = HubClient("127.0.0.1", server.localPort, CALL, """{"type":"offer","call_id":"$CALL"}""",
            listener, connectTimeoutMs = 2_000, acceptTimeoutMs = 3_000)
        c.start()
        val hub = accept()
        hub.json()
        assertFalse(c.sendControl("""{"type":"announce"}"""))   // prima dell'accept
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals("accepted", events.poll(3, TimeUnit.SECONDS))
        assertTrue(c.sendControl("""{"type":"announce","call_id":"$CALL","text":"Lavatrice finita","urgent":false}"""))
        val a = hub.json()
        assertEquals("announce", a.getString("type"))
        assertEquals("Lavatrice finita", a.getString("text"))
        c.end()
    }

    @Test fun rejected_keepsTheReasonAsDetail() {
        val c = HubClient("127.0.0.1", server.localPort, CALL, """{"type":"offer","call_id":"$CALL"}""",
            listener, connectTimeoutMs = 2_000, acceptTimeoutMs = 3_000)
        assertEquals("", c.rejectDetail)
        c.start()
        val hub = accept()
        hub.json()
        hub.send("""{"type":"rejected","call_id":"$CALL","reason":"provider_unavailable"}""")
        assertEquals("ended:hub_rejected", events.poll(3, TimeUnit.SECONDS))
        assertEquals("provider_unavailable", c.rejectDetail)
        assertNull(events.poll(200, TimeUnit.MILLISECONDS))
    }

    private companion object {
        const val CALL = "ab12cd34"
    }
}
