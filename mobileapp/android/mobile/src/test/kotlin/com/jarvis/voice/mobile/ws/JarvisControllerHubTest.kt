package com.jarvis.voice.mobile.ws

import com.jarvis.voice.mobile.config.ControllerConfig
import com.jarvis.voice.mobile.live.LiveAudioPort
import com.jarvis.voice.shared.HeadState
import com.jarvis.voice.shared.hub.HubClient
import com.jarvis.voice.shared.hub.HubIpc
import com.jarvis.voice.shared.protocol.EndReason
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.BufferedInputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * JarvisController in sessione hub contro un orchestrator finto (MockWebServer, WS vera) e un
 * hub finto (ServerSocket, framing di voice_live_hub). Copre le decisioni D1/D3/D4/D8/D9 lato app
 * e il path di oggi senza backend "hub".
 */
class JarvisControllerHubTest {

    private var server = MockWebServer()
    private val hubServer = ServerSocket(0)
    private val orch = FakeOrchestrator()
    private val port = FakePort()
    private lateinit var c: JarvisController
    private lateinit var cfg: Cfg

    private class Cfg(@Volatile override var url: String) : ControllerConfig {
        override val token = ""
        override val ttlMillis = 600_000L
        override val deviceId = "AABBCCDDEEFF"
        override val isConfigured = true
    }

    private class FakePort : LiveAudioPort {
        val calls = LinkedBlockingQueue<String>()
        @Volatile var startOk = true
        override fun prepare() { calls.add("prepare") }
        override fun start(onPlaybackLevel: (Float) -> Unit): Boolean { calls.add("start"); return startOk }
        override fun play(pcm: ShortArray) { calls.add("play") }
        override fun clear() { calls.add("clear") }
        override fun stop() { calls.add("stop") }
    }

    /** Orchestrator finto: manda welcome all'apertura (come ws_audio_handler) e raccoglie i messaggi. */
    private inner class FakeOrchestrator : WebSocketListener() {
        val inbound = LinkedBlockingQueue<JSONObject>()
        @Volatile var ws: WebSocket? = null
        fun expectConnection() { server.enqueue(MockResponse().withWebSocketUpgrade(this)) }
        override fun onOpen(webSocket: WebSocket, response: Response) {
            ws = webSocket
            webSocket.send("""{"type":"welcome","server_time":0}""")
        }
        override fun onMessage(webSocket: WebSocket, text: String) { inbound.add(JSONObject(text)) }
        fun send(json: String) { assertTrue(ws!!.send(json)) }

        /** Prossimo messaggio del tipo dato (gli altri vengono scartati). */
        fun next(type: String, timeoutMs: Long = 3_000): JSONObject {
            val until = System.currentTimeMillis() + timeoutMs
            while (true) {
                val left = until - System.currentTimeMillis()
                if (left <= 0) fail("nessun '$type' dal device")
                val m = inbound.poll(left, TimeUnit.MILLISECONDS) ?: throw AssertionError("nessun '$type' dal device")
                if (m.optString("type") == type) return m
            }
        }

        fun assertNone(type: String, ms: Long) {
            val until = System.currentTimeMillis() + ms
            while (true) {
                val left = until - System.currentTimeMillis()
                if (left <= 0) return
                val m = inbound.poll(left, TimeUnit.MILLISECONDS) ?: return
                if (m.optString("type") == type) fail("'$type' inatteso: $m")
            }
        }
    }

    private class HubPeer(val sock: Socket) {
        val input = DataInputStream(BufferedInputStream(sock.getInputStream()))
        val out: OutputStream = sock.getOutputStream()
        fun read(): HubIpc.Frame? = HubIpc.readFrame(input)
        fun json(): JSONObject = JSONObject((read() as HubIpc.Frame.Json).text)
        fun send(json: String) { out.write(HubIpc.encodeJson(json)); out.flush() }
        /** Prossimo frame JSON (salta l'audio del mic). */
        fun nextJson(): JSONObject? {
            while (true) {
                when (val f = read() ?: return null) {
                    is HubIpc.Frame.Json -> return JSONObject(f.text)
                    is HubIpc.Frame.Audio -> {}
                }
            }
        }
    }

    @Before fun setUp() {
        server.start()
        orch.expectConnection()
        cfg = Cfg("ws://${server.hostName}:${server.port}")
        c = JarvisController(
            cfg,
            hubClientFactory = { spec, l ->
                HubClient(spec.host, spec.port, spec.callId, spec.offerJson(), l,
                    connectTimeoutMs = 2_000, acceptTimeoutMs = 2_000, endGraceMs = 300)
            },
            fallbackWaitMs = 400,
        )
        c.liveAudio = port
        c.remoteTtsWriter = {}   // niente AudioTrack nei test JVM
    }

    @After fun tearDown() {
        runCatching { server.shutdown() }
        hubServer.close()
    }

    private fun awaitState(s: HeadState, timeoutMs: Long = 3_000) {
        val until = System.currentTimeMillis() + timeoutMs
        while (c.state.value != s) {
            if (System.currentTimeMillis() > until) fail("stato ${c.state.value}, atteso $s")
            Thread.sleep(10)
        }
    }

    private fun awaitCall(name: String) {
        val until = System.currentTimeMillis() + 3_000
        while (true) {
            val left = until - System.currentTimeMillis()
            if (left <= 0) fail("il lato audio non ha ricevuto $name")
            if (port.calls.poll(left, TimeUnit.MILLISECONDS) == name) return
        }
    }

    /** Tap → WS + hello + audio_start, come un turno normale. */
    private fun connect() {
        c.onTap()
        orch.next("hello")
        orch.next("audio_start")
    }

    private fun hubStart(callId: String = CALL, hubPort: Int = hubServer.localPort) = """
        {"type":"live_session_start","backend":"hub","call_id":"$callId","profile":"hermes-marco",
         "name":"Marco","voice":"","location_id":"napoli","room":"studio","deliver":["orchestrator"],
         "hub":{"host":"127.0.0.1","port":$hubPort}}
    """.trimIndent().replace("\n", "")

    /** live_session_start(hub) → offer all'hub finto → accept. */
    private fun openActiveSession(doConnect: Boolean = true): HubPeer {
        if (doConnect) connect()
        orch.send(hubStart())
        hubServer.soTimeout = 3_000
        val hub = HubPeer(hubServer.accept().also { it.soTimeout = 3_000 })
        val offer = hub.json()
        assertEquals("offer", offer.getString("type"))
        assertEquals(CALL, offer.getString("call_id"))
        awaitCall("prepare")
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        awaitCall("start")
        assertTrue(c.liveHub.value)
        assertEquals(HeadState.LISTENING, c.state.value)
        return hub
    }

    // ── feature off ──────────────────────────────────────────────────────────

    @Test fun legacyLiveSession_untouched() {
        connect()
        orch.send("""{"type":"live_session_start"}""")
        awaitState(HeadState.LISTENING)
        Thread.sleep(200)
        assertFalse(c.liveHub.value)
        assertNull(port.calls.poll())   // nessun prepare/start/stop sul lato audio live
        orch.send("""{"type":"live_session_end"}""")
        awaitState(HeadState.IDLE)
        orch.assertNone("live_session_end", 300)   // nessuna risposta in ingresso
        assertNull(port.calls.poll())
    }

    // ── VAD nell'offer: solo per lo watch ────────────────────────────────────

    private fun offerVad(relay: Boolean): JSONObject? {
        c.relayMode = relay
        connect()
        orch.send(hubStart())
        hubServer.soTimeout = 3_000
        val hub = HubPeer(hubServer.accept().also { it.soTimeout = 3_000 })
        val offer = hub.json()
        assertEquals("offer", offer.getString("type"))
        hub.sock.close()
        return offer.optJSONObject("vad")
    }

    @Test fun watchOffer_hasLowSensitivityVad() {
        val vad = offerVad(relay = true)!!
        assertEquals("START_SENSITIVITY_LOW", vad.getString("startOfSpeechSensitivity"))
        assertEquals(300, vad.getInt("prefixPaddingMs"))
    }

    @Test fun phoneOffer_hasNoVad() {
        assertNull(offerVad(relay = false))
    }

    // ── sessione completa + D8 + fine col tap ────────────────────────────────

    @Test fun tapEndsSession_withButton_andTurnEventsIgnored() {
        val hub = openActiveSession()

        // mic → hub
        c.feedMicFrame(ByteArray(640) { 1 })
        assertTrue(hub.read() is HubIpc.Frame.Audio)

        // D8: gli eventi di turno dell'orchestrator non toccano la sessione
        orch.send("""{"type":"tts_done"}""")
        orch.send("""{"type":"trigger_listen"}""")
        Thread.sleep(1_100)   // oltre il ritardo del trigger_listen
        assertEquals(HeadState.LISTENING, c.state.value)
        assertTrue(c.liveHub.value)
        orch.assertNone("audio_start", 100)

        c.onTap()
        val end = orch.next("live_session_end")
        assertEquals(CALL, end.getString("call_id"))
        assertEquals(EndReason.BUTTON, end.getString("reason"))
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitCall("stop")
        awaitState(HeadState.IDLE)
        assertFalse(c.liveHub.value)

        // D1: eco dell'orchestrator (live_session_end + tts_done) senza effetti
        orch.send("""{"type":"live_session_end"}""")
        orch.send("""{"type":"tts_done"}""")
        Thread.sleep(200)
        assertEquals(HeadState.IDLE, c.state.value)
        orch.assertNone("live_session_end", 300)
    }

    // ── D1: fine decisa dall'orchestrator ────────────────────────────────────

    @Test fun orchestratorEnd_endsHub_withoutReply_thenTtsDoneIdle() {
        val hub = openActiveSession()
        orch.send("""{"type":"live_session_end"}""")
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitCall("stop")
        awaitState(HeadState.IDLE)
        orch.send("""{"type":"tts_done"}""")
        Thread.sleep(200)
        assertEquals(HeadState.IDLE, c.state.value)
        orch.assertNone("live_session_end", 400)
    }

    @Test fun hubHangup_reportsHubHangup_andGoesIdle() {
        val hub = openActiveSession()
        hub.send("""{"type":"hangup","call_id":"$CALL"}""")
        val end = orch.next("live_session_end")
        assertEquals(EndReason.HUB_HANGUP, end.getString("reason"))
        awaitCall("stop")
        awaitState(HeadState.IDLE)   // nessuna attesa di ripiego
    }

    // ── D3: ripiego deciso dall'orchestrator ─────────────────────────────────

    @Test fun hubRejected_reportsOnly_waitsForOrchestratorFallback() {
        connect()
        orch.send(hubStart())
        hubServer.soTimeout = 3_000
        val hub = HubPeer(hubServer.accept().also { it.soTimeout = 3_000 })
        hub.json()
        hub.send("""{"type":"rejected","reason":"busy"}""")

        val end = orch.next("live_session_end")
        assertEquals(CALL, end.getString("call_id"))
        assertEquals(EndReason.HUB_REJECTED, end.getString("reason"))
        awaitCall("prepare")
        awaitCall("stop")          // lato audio (watch) riportato al path di oggi
        awaitState(HeadState.THINKING)   // in attesa: NON entra da solo nella vecchia live
        assertFalse(c.liveHub.value)

        // conversione sul posto: live_session_start semplice → sessione di oggi
        orch.send("""{"type":"live_session_start"}""")
        awaitState(HeadState.LISTENING)
        Thread.sleep(600)   // oltre l'attesa del ripiego: resta nella sessione di oggi
        assertEquals(HeadState.LISTENING, c.state.value)
        assertFalse(c.liveHub.value)
    }

    @Test fun silentCloseBeforeAccept_isHubRejected() {
        connect()
        orch.send(hubStart())
        hubServer.soTimeout = 3_000
        val hub = hubServer.accept()
        HubPeer(hub).json()
        hub.close()   // come l'hub con un call_id duplicato
        assertEquals(EndReason.HUB_REJECTED, orch.next("live_session_end").getString("reason"))
        awaitState(HeadState.THINKING)
    }

    @Test fun hubUnreachable_noFallbackFromOrchestrator_goesIdleAfterWait() {
        val dead = ServerSocket(0).let { val p = it.localPort; it.close(); p }
        connect()
        orch.send(hubStart(hubPort = dead))
        assertEquals(EndReason.HUB_UNREACHABLE, orch.next("live_session_end").getString("reason"))
        awaitState(HeadState.THINKING)
        awaitState(HeadState.IDLE, timeoutMs = 2_000)
    }

    @Test fun hubStartWithoutCallId_reportsEmptyCallIdError_andWaits() {
        connect()
        orch.send("""{"type":"live_session_start","backend":"hub","hub":{"host":"127.0.0.1","port":1}}""")
        val end = orch.next("live_session_end")
        assertEquals("", end.getString("call_id"))
        assertEquals(EndReason.ERROR, end.getString("reason"))
        awaitState(HeadState.THINKING)
        assertNull(port.calls.poll(200, TimeUnit.MILLISECONDS))   // nessun hub, nessun lato audio
    }

    @Test fun liveAudioFailsAtAccept_reportsError_andGoesIdle() {
        port.startOk = false
        connect()
        orch.send(hubStart())
        hubServer.soTimeout = 3_000
        val hub = HubPeer(hubServer.accept().also { it.soTimeout = 3_000 })
        hub.json()
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals(EndReason.ERROR, orch.next("live_session_end").getString("reason"))
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitState(HeadState.IDLE)
    }

    // ── D9: abbandoni lato app ───────────────────────────────────────────────

    @Test fun wsLoss_endsHub_andReportsOnNextConnection() {
        val hub = openActiveSession()
        server.shutdown()   // l'orchestrator sparisce (connessione troncata)
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitCall("stop")
        awaitState(HeadState.ERROR)
        assertFalse(c.liveHub.value)

        // prossima connessione: dopo il welcome parte il live_session_end rimasto in sospeso
        server = MockWebServer().also { it.start() }
        cfg.url = "ws://${server.hostName}:${server.port}"
        orch.expectConnection()
        c.onTap()
        orch.next("hello")
        val end = orch.next("live_session_end")
        assertEquals(CALL, end.getString("call_id"))
        assertEquals(EndReason.ERROR, end.getString("reason"))
        orch.assertNone("live_session_end", 300)   // una volta sola
    }

    @Test fun tapDuringHandshake_endsWithButton() {
        connect()
        orch.send(hubStart())
        hubServer.soTimeout = 3_000
        val hub = HubPeer(hubServer.accept().also { it.soTimeout = 3_000 })
        hub.json()   // offer, nessun accept
        c.onTap()
        val end = orch.next("live_session_end")
        assertEquals(CALL, end.getString("call_id"))
        assertEquals(EndReason.BUTTON, end.getString("reason"))
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitState(HeadState.IDLE)
    }

    @Test fun abortLiveSession_reportsError_andIsNoOpOutsideSession() {
        connect()
        c.abortLiveSession(EndReason.ERROR)   // fuori sessione: nulla
        orch.assertNone("live_session_end", 300)

        val hub = openActiveSession(doConnect = false)
        c.abortLiveSession(EndReason.ERROR)   // es. service distrutto, watch senza mic
        assertEquals(EndReason.ERROR, orch.next("live_session_end").getString("reason"))
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitCall("stop")
        awaitState(HeadState.IDLE)
    }

    private companion object {
        const val CALL = "ab12cd34"
    }
}
