package com.jarvis.voice.mobile.ws

import com.jarvis.voice.mobile.config.ControllerConfig
import com.jarvis.voice.mobile.live.LiveAudioPort
import com.jarvis.voice.shared.HeadState
import com.jarvis.voice.shared.hub.HubIpc
import com.jarvis.voice.shared.protocol.EndReason
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.math.PI
import kotlin.math.sin
import kotlin.random.Random

/**
 * Fase 3 lato app (design-voice-live-hub.md 19, decisioni 19.12): il tap apre direttamente la
 * sessione hub. Orchestrator finto (MockWebServer, WS vera) e hub finto (ServerSocket, framing
 * di voice_live_hub), con il contratto come lo implementano main.py e il wakeword-server.
 */
class JarvisControllerLiveTapTest {

    private val server = MockWebServer()
    private val hubServer = ServerSocket(0)
    private val orch = FakeOrchestrator()
    private val port = FakePort()
    private val cfg = Cfg()
    private var controller: JarvisController? = null
    private val c: JarvisController get() = controller!!

    private class Cfg : ControllerConfig {
        override var url = ""
        override val token = ""
        override val ttlMillis = 600_000L
        override val deviceId = "AABBCCDDEEFF"
        override val isConfigured = true
        @Volatile var allowed = true
        override val liveOnTapAllowed: Boolean get() = allowed
        val cache = ConcurrentHashMap<String, Boolean>()
        override fun cachedLiveOnWake(deviceId: String) = cache[deviceId] ?: false
        override fun cacheLiveOnWake(deviceId: String, enabled: Boolean) { cache[deviceId] = enabled }
    }

    private class FakePort : LiveAudioPort {
        val calls = LinkedBlockingQueue<String>()
        override fun prepare() { calls.add("prepare") }
        override fun capture(): Boolean { calls.add("capture"); return true }
        override fun start(onPlaybackLevel: (Float) -> Unit): Boolean { calls.add("start"); return true }
        override fun play(pcm: ShortArray) {}
        override fun clear() {}
        override fun stop() { calls.add("stop") }
        fun drain(): List<String> = mutableListOf<String>().also { calls.drainTo(it) }
    }

    /** Orchestrator finto: welcome con live_on_wake all'apertura, raccoglie i messaggi del device. */
    private inner class FakeOrchestrator : WebSocketListener() {
        val inbound = LinkedBlockingQueue<JSONObject>()
        @Volatile var ws: WebSocket? = null
        @Volatile var liveOnWake = true
        fun expectConnection() { server.enqueue(MockResponse().withWebSocketUpgrade(this)) }
        override fun onOpen(webSocket: WebSocket, response: Response) {
            ws = webSocket
            webSocket.send("""{"type":"welcome","server_time":0,"wake_voice_gate":false,"live_on_wake":$liveOnWake}""")
        }
        override fun onMessage(webSocket: WebSocket, text: String) { inbound.add(JSONObject(text)) }
        fun send(json: String) { assertTrue(ws!!.send(json)) }

        fun next(type: String, timeoutMs: Long = 3_000): JSONObject {
            val until = System.currentTimeMillis() + timeoutMs
            while (true) {
                val left = until - System.currentTimeMillis()
                if (left <= 0) fail("nessun '$type' dal device")
                val m = inbound.poll(left, TimeUnit.MILLISECONDS) ?: throw AssertionError("nessun '$type' dal device")
                if (m.optString("type") == type) return m
            }
        }

        /** Prossimo messaggio qualsiasi (per controllare l'ordine). */
        fun nextAny(timeoutMs: Long = 3_000): JSONObject =
            inbound.poll(timeoutMs, TimeUnit.MILLISECONDS) ?: throw AssertionError("nessun messaggio dal device")

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
        fun nextJson(): JSONObject? {
            while (true) {
                when (val f = read() ?: return null) {
                    is HubIpc.Frame.Json -> return JSONObject(f.text)
                    is HubIpc.Frame.Audio -> {}
                }
            }
        }
    }

    private fun build(
        answerTimeoutMs: Long = 2_500,
        commandWindowMs: Long = 400,
        fallbackWaitMs: Long = 10_000,   // lungo: con origine tap non si deve MAI aspettare il ripiego
        relay: Boolean = false,
    ) {
        server.start()
        orch.expectConnection()
        cfg.url = "ws://${server.hostName}:${server.port}"
        controller = JarvisController(
            cfg,
            hubClientFactory = { spec, offer, l ->
                JarvisController.defaultHubClient(spec, offer, l,
                    connectTimeoutMs = 2_000, acceptTimeoutMs = if (spec.fromRequest) 2_500 else 2_000, endGraceMs = 300)
            },
            fallbackWaitMs = fallbackWaitMs,
            liveAnswerTimeoutMs = answerTimeoutMs,
            commandWindowMs = commandWindowMs,
        ).also {
            it.liveAudio = port
            it.remoteTtsWriter = {}   // niente AudioTrack nei test JVM
            it.relayMode = relay
        }
    }

    @After fun tearDown() {
        runCatching { server.shutdown() }
        hubServer.close()
    }

    private fun awaitState(s: HeadState, timeoutMs: Long = 3_000) {
        val until = System.currentTimeMillis() + timeoutMs
        while (c.state.value != s) {
            if (System.currentTimeMillis() > until) fail("stato ${c.state.value}, atteso $s")
            Thread.sleep(5)
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

    // ── audio di prova ───────────────────────────────────────────────────────

    private val rnd = Random(3)
    private var ph = 0
    private fun frame(sample: () -> Int): ByteArray {
        val out = ByteArray(640)
        for (i in 0 until 320) {
            val v = sample()
            out[i * 2] = (v and 0xFF).toByte(); out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }
    private fun noise() = frame { rnd.nextInt(-40, 41) }
    private fun voice() = frame { (3_000 * sin(2 * PI * 220 * (ph++) / 16_000.0)).toInt() }

    // ── helper di flusso ─────────────────────────────────────────────────────

    /** Prima connessione: un turno classico, il welcome mette live_on_wake in cache, poi a riposo. */
    private fun learnFlag() {
        c.onTap()
        orch.next("hello")
        orch.next("audio_start")
        orch.send("""{"type":"tts_done"}""")
        awaitState(HeadState.IDLE)
        port.drain()
    }

    /** Tap con il flag in cache: live_start{req_id, trigger:"tap"}. */
    private fun tapLive(): String {
        c.onTap()
        val ls = orch.next("live_start")
        assertEquals("tap", ls.getString("trigger"))
        val req = ls.getString("req_id")
        assertTrue(req.isNotEmpty() && req.length <= 64)
        awaitCall("capture")
        return req
    }

    private fun hubStart(req: String, callId: String = CALL, hubPort: Int = hubServer.localPort, identified: Boolean = true) = """
        {"type":"live_session_start","backend":"hub","call_id":"$callId","profile":"hermes-marco",
         "name":"Marco","voice":"","location_id":"napoli","room":"Unknown","deliver":["orchestrator"],
         "hub":{"host":"127.0.0.1","port":$hubPort},"origin":"tap","identified":$identified,
         "offer_extra":{"origin":"tap","identified":$identified,"command":false,"quick":true},"req_id":"$req"}
    """.trimIndent().replace("\n", "")

    private fun acceptHub(): HubPeer {
        hubServer.soTimeout = 3_000
        return HubPeer(hubServer.accept().also { it.soTimeout = 3_000 })
    }

    // ── flag spento = oggi ───────────────────────────────────────────────────

    @Test fun flagOff_tapIsTodaysClassicTurn() {
        orch.liveOnWake = false
        build()
        learnFlag()
        c.onTap()
        orch.next("audio_start")
        orch.assertNone("live_start", 300)
        assertNull(port.calls.poll())   // nessun capture/prepare/start: mic di oggi
        assertFalse(cfg.cachedLiveOnWake(cfg.deviceId))
    }

    @Test fun firstTapWithoutCache_isClassic_thenWelcomeEnablesLive() {
        build()
        c.onTap()
        orch.next("hello")
        orch.next("audio_start")   // cache vuota: classico
        orch.assertNone("live_start", 200)
        assertTrue(cfg.cachedLiveOnWake(cfg.deviceId))   // dal welcome, persistito
        orch.send("""{"type":"tts_done"}""")
        awaitState(HeadState.IDLE)
        tapLive()
    }

    @Test fun appSwitchOff_classicEvenWithFlagOn() {
        build()
        learnFlag()
        cfg.allowed = false
        c.onTap()
        orch.next("audio_start")
        orch.assertNone("live_start", 300)
    }

    @Test fun pushedFlag_takesEffectWithoutReconnecting() {
        orch.liveOnWake = false
        build()
        learnFlag()
        orch.send("""{"type":"live_on_wake","live_on_wake":true}""")
        Thread.sleep(150)
        tapLive()
        orch.send("""{"type":"live_declined","req_id":"x","reason":"busy"}""")   // req vecchio: ignorato
        orch.send("""{"type":"live_on_wake","live_on_wake":false}""")
        Thread.sleep(150)
        c.onTap()   // annulla la richiesta in corso
        orch.next("live_cancel")
        awaitState(HeadState.IDLE)
        c.onTap()
        orch.next("audio_start")   // flag tolto: di nuovo classico
    }

    // ── percorso felice: accept, buffer del comando, live_session_active ────

    @Test fun tapLive_commandBufferFirst_offerExtraCommand_andSessionActive() {
        build()
        learnFlag()
        val req = tapLive()
        assertEquals(HeadState.LISTENING, c.state.value)
        assertFalse(c.liveHub.value)
        // "accendi la luce" detto subito dopo il tap, prima della risposta dell'orchestrator
        val buffered = ByteArrayOutputStream()
        repeat(15) { noise().also { buffered.write(it); c.feedMicFrame(it) } }
        repeat(30) { voice().also { buffered.write(it); c.feedMicFrame(it) } }
        orch.send(hubStart(req))
        val hub = acceptHub()
        val offer = hub.json()
        assertEquals("offer", offer.getString("type"))
        assertEquals(CALL, offer.getString("call_id"))
        val extra = offer.getJSONObject("offer_extra")
        assertEquals("tap", extra.getString("origin"))
        assertTrue(extra.getBoolean("identified"))
        assertTrue(extra.getBoolean("quick"))
        assertTrue("VAD del peer: c'è un comando", extra.getBoolean("command"))
        assertTrue(offer.getBoolean("command"))      // anche al primo livello
        assertEquals("tap", offer.getString("origin"))
        assertFalse(offer.has("vad"))                // telefono: VAD di default dell'hub
        // durante l'handshake il mic continua nel buffer
        repeat(5) { voice().also { buffered.write(it); c.feedMicFrame(it) } }
        assertTrue(c.liveHub.value)
        orch.assertNone("live_session_active", 100)  // non prima dell'accept

        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        awaitCall("start")
        val active = orch.next("live_session_active")
        assertEquals(CALL, active.getString("call_id"))
        assertEquals(req, active.getString("req_id"))
        // dopo l'accept: mic dal vivo, DOPO tutto il buffer
        val live = ByteArray(640) { 9 }
        c.feedMicFrame(live)
        val got = ByteArrayOutputStream()
        val expected = buffered.toByteArray() + live
        while (got.size() < expected.size) {
            val f = hub.read() as HubIpc.Frame.Audio
            assertEquals(CALL, f.callId)
            got.write(f.pcm)
        }
        assertArrayEquals(expected, got.toByteArray())
        assertEquals(HeadState.LISTENING, c.state.value)

        // il tap a sessione avviata la chiude come oggi
        c.onTap()
        assertEquals(EndReason.BUTTON, orch.next("live_session_end").getString("reason"))
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitState(HeadState.IDLE)
    }

    @Test fun noSpeechAfterTap_commandFalse_offerWaitsAtMostTheWindow() {
        build(commandWindowMs = 400)
        learnFlag()
        val req = tapLive()
        val t0 = System.currentTimeMillis()
        repeat(20) { c.feedMicFrame(noise()) }
        orch.send(hubStart(req))
        val hub = acceptHub()
        val offer = hub.json()
        assertFalse(offer.getJSONObject("offer_extra").getBoolean("command"))
        assertFalse(offer.getBoolean("command"))
        assertTrue("offer entro la finestra del comando", System.currentTimeMillis() - t0 < 1_500)
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        orch.next("live_session_active")
    }

    // ── rifiuti e guasti: subito il classico ─────────────────────────────────

    @Test fun declinedBusy_startsClassicTurnAtOnce() {
        build()
        learnFlag()
        val req = tapLive()
        orch.send("""{"type":"live_declined","req_id":"$req","reason":"busy"}""")
        val t0 = System.currentTimeMillis()
        orch.next("audio_start", timeoutMs = 1_000)
        assertTrue(System.currentTimeMillis() - t0 < 1_000)
        awaitCall("stop")   // mic della live spento prima del classico
        awaitState(HeadState.LISTENING)
        orch.assertNone("live_cancel", 200)   // l'orchestrator lo sa già
    }

    @Test fun declinedDuplicateOrVoiceGate_restsWithoutClassic() {
        build()
        learnFlag()
        for (reason in listOf("duplicate", "voice_gate")) {
            val req = tapLive()
            orch.send("""{"type":"live_declined","req_id":"$req","reason":"$reason"}""")
            awaitCall("stop")
            awaitState(HeadState.IDLE)
            orch.assertNone("audio_start", 300)
        }
    }

    @Test fun hubRejectedWithOriginTap_reportsDetail_andClassicWithoutWaiting() {
        build(fallbackWaitMs = 10_000)
        learnFlag()
        val req = tapLive()
        orch.send(hubStart(req))
        val hub = acceptHub()
        hub.json()
        hub.send("""{"type":"rejected","call_id":"$CALL","reason":"provider_unavailable"}""")
        val end = orch.next("live_session_end")
        assertEquals(CALL, end.getString("call_id"))
        assertEquals(EndReason.HUB_REJECTED, end.getString("reason"))
        assertEquals("provider_unavailable", end.getString("detail"))
        // subito dopo, e nell'ordine: il turno classico (nessuna attesa in THINKING)
        val next = orch.nextAny(timeoutMs = 1_000)
        assertEquals("audio_start", next.getString("type"))
        assertNotEquals(HeadState.THINKING, c.state.value)
        awaitState(HeadState.LISTENING)
        assertFalse(c.liveHub.value)
        orch.assertNone("live_session_active", 200)
    }

    @Test fun hubUnreachableWithOriginTap_classicAtOnce() {
        build()
        learnFlag()
        val dead = ServerSocket(0).let { val p = it.localPort; it.close(); p }
        val req = tapLive()
        orch.send(hubStart(req, hubPort = dead))
        assertEquals(EndReason.HUB_UNREACHABLE, orch.next("live_session_end").getString("reason"))
        orch.next("audio_start", timeoutMs = 1_000)
    }

    @Test fun acceptTimeoutWithOriginTap_isHubUnreachable_thenClassic() {
        build()
        learnFlag()
        val req = tapLive()
        orch.send(hubStart(req))
        val hub = acceptHub()
        hub.json()   // offer, poi niente accept: scade l'accept tardivo (2,5 s nel test, 7 s in app)
        assertEquals(EndReason.HUB_UNREACHABLE, orch.next("live_session_end", timeoutMs = 5_000).getString("reason"))
        orch.next("audio_start", timeoutMs = 1_000)
    }

    @Test fun answerTimeout_sendsLiveCancel_thenClassic() {
        build(answerTimeoutMs = 300)
        learnFlag()
        val req = tapLive()
        val cancel = orch.next("live_cancel", timeoutMs = 2_000)
        assertEquals(req, cancel.getString("req_id"))
        orch.next("audio_start", timeoutMs = 1_000)
        awaitCall("stop")
        // risposta tardiva col req vecchio: ignorata, nessun hub
        orch.send(hubStart(req))
        hubServer.soTimeout = 400
        try { hubServer.accept().close(); fail("connessione all'hub con un req_id scaduto") } catch (_: SocketTimeoutException) {}
    }

    @Test fun staleReqId_ignored_pendingStillWaits() {
        build()
        learnFlag()
        val req = tapLive()
        orch.send(hubStart("vecchio"))
        orch.send("""{"type":"live_declined","req_id":"vecchio","reason":"busy"}""")
        hubServer.soTimeout = 400
        try { hubServer.accept().close(); fail("connessione all'hub con un req_id diverso") } catch (_: SocketTimeoutException) {}
        orch.assertNone("audio_start", 100)
        // la risposta giusta invece apre la sessione
        orch.send(hubStart(req))
        val hub = acceptHub()
        assertEquals("offer", hub.json().getString("type"))
    }

    // ── rinuncia dell'utente ─────────────────────────────────────────────────

    @Test fun tapWhileWaitingForOrchestrator_liveCancel_andIdle() {
        build()
        learnFlag()
        val req = tapLive()
        c.onTap()
        assertEquals(req, orch.next("live_cancel").getString("req_id"))
        awaitCall("stop")
        awaitState(HeadState.IDLE)
        orch.assertNone("audio_start", 300)
    }

    @Test fun tapDuringHubHandshake_liveCancel_endedToHub_noLiveSessionEnd() {
        build()
        learnFlag()
        val req = tapLive()
        orch.send(hubStart(req))
        val hub = acceptHub()
        hub.json()
        c.onTap()
        assertEquals(req, orch.next("live_cancel").getString("req_id"))
        assertEquals("ended", hub.nextJson()!!.getString("type"))
        awaitState(HeadState.IDLE)
        assertFalse(c.liveHub.value)
        orch.assertNone("live_session_end", 300)
        orch.assertNone("audio_start", 100)
    }

    // ── sessione avviata: annunci e guasti ───────────────────────────────────

    private fun openTapSession(): Pair<String, HubPeer> {
        learnFlag()
        val req = tapLive()
        orch.send(hubStart(req))
        val hub = acceptHub()
        hub.json()
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        orch.next("live_session_active")
        return req to hub
    }

    @Test fun announce_relayedToHub_onlyForTheCurrentCall() {
        build()
        val (_, hub) = openTapSession()
        orch.send("""{"type":"announce","call_id":"altra","text":"non mio","urgent":false}""")
        orch.send("""{"type":"announce","call_id":"$CALL","text":"La lavatrice ha finito","urgent":true}""")
        val a = hub.nextJson()!!
        assertEquals("announce", a.getString("type"))
        assertEquals(CALL, a.getString("call_id"))
        assertEquals("La lavatrice ha finito", a.getString("text"))
        assertTrue(a.getBoolean("urgent"))
    }

    @Test fun errorAfterAccept_withOriginTap_goesIdle_noClassic() {
        build()
        val (_, hub) = openTapSession()
        hub.sock.close()   // hub che cade a sessione avviata
        assertEquals(EndReason.ERROR, orch.next("live_session_end").getString("reason"))
        awaitState(HeadState.IDLE, timeoutMs = 1_000)   // niente THINKING: con origine tap non c'è ripiego
        orch.assertNone("audio_start", 300)
    }

    @Test fun turnEventsDuringPending_ignored() {
        build()
        learnFlag()
        tapLive()
        orch.send("""{"type":"tts_done"}""")
        Thread.sleep(200)
        assertEquals(HeadState.LISTENING, c.state.value)
    }

    // ── watch (relay) ────────────────────────────────────────────────────────

    @Test fun watchTap_capturesViaLiveMode_connectingState_lowSensitivityVad() {
        build(relay = true)
        c.onTap()
        orch.next("hello")
        orch.next("audio_start")
        orch.send("""{"type":"tts_done"}""")
        awaitState(HeadState.IDLE)
        port.drain()
        val req = tapLive()
        // Watch: niente LISTENING durante l'attesa, così non apre il mic di oggi (x8)
        awaitState(HeadState.CONNECTING)
        repeat(15) { c.feedMicFrame(noise()) }   // mic AEC del watch dal canale
        repeat(25) { c.feedMicFrame(voice()) }
        orch.send(hubStart(req))
        val hub = acceptHub()
        val offer = hub.json()
        assertEquals("START_SENSITIVITY_LOW", offer.getJSONObject("vad").getString("startOfSpeechSensitivity"))
        assertTrue(offer.getJSONObject("offer_extra").getBoolean("command"))
        hub.send("""{"type":"accept","call_id":"$CALL"}""")
        assertEquals(req, orch.next("live_session_active").getString("req_id"))
        assertEquals(HeadState.LISTENING, c.state.value)
    }

    private companion object {
        const val CALL = "ab12cd34"
    }
}
