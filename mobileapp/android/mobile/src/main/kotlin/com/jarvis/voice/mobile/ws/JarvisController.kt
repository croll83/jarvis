package com.jarvis.voice.mobile.ws

import android.util.Log
import com.jarvis.voice.mobile.audio.OpusDecoderWrapper
import com.jarvis.voice.mobile.config.ControllerConfig
import com.jarvis.voice.mobile.live.LiveAudioPort
import com.jarvis.voice.shared.HeadState
import com.jarvis.voice.shared.audio.LiveAudio
import com.jarvis.voice.shared.audio.TtsPlayer
import com.jarvis.voice.shared.hub.HubClient
import com.jarvis.voice.shared.hub.HubLiveStart
import com.jarvis.voice.shared.protocol.Codec
import com.jarvis.voice.shared.protocol.EndReason
import com.jarvis.voice.shared.protocol.MsgType
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * Cervello del client mobile: implementa la state machine di /ws/audio
 * (equivalente Kotlin di tools/jarvis_ptt_spike/jarvis_ptt_client.py).
 *
 * Connessione ON-DEMAND con finestra calda TTL:
 *   tap → (se serve) apri WS + hello → audio_start(pcm) → stream mic →
 *   speech_end (VAD) | flush (2° tap) → tts (Opus) → tts_done → idle.
 * Dopo `ttlMillis` di inattività (fuori da live session) la WS viene chiusa.
 *
 * Sorgente mic e sink TTS sono commutabili: in modalità standalone il telefono usa
 * il proprio mic/speaker; in modalità relay lo watch fornisce il mic e riceve la TTS.
 *
 * Sessione live su Voice Live Hub (`live_session_start` con backend "hub"): l'audio esce
 * dal turn-loop dell'orchestrator e va full-duplex su un [HubClient] TCP diretto all'hub,
 * con [liveAudio] come lato audio del target (telefono o watch). La WS resta solo per il
 * controllo (fine sessione in entrambi i versi). Senza backend "hub" tutto come prima.
 */
class JarvisController(
    private val config: ControllerConfig,
    private val deviceIdOverride: String? = null,   // usato dal relay watch (device_id distinto)
    private val hubClientFactory: (HubLiveStart.Start, HubClient.Listener) -> HubClient =
        { spec, l -> HubClient(spec.host, spec.port, spec.callId, spec.offerJson(), l) },
    private val fallbackWaitMs: Long = FALLBACK_WAIT_MS,
) {
    private val tag = "JarvisController"
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val connectMutex = Mutex()

    private val http = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)       // WS ping a livello protocollo
        .readTimeout(0, TimeUnit.MILLISECONDS)    // no read timeout (connessione lunga)
        .build()

    private val _state = MutableStateFlow(HeadState.IDLE)
    val state: StateFlow<HeadState> = _state.asStateFlow()

    private val _amplitude = MutableStateFlow(0f)
    /** Livello mic normalizzato 0..1 (alimenta la waveform in UI). Valido in LISTENING. */
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    private val _history = MutableStateFlow<List<DialogTurn>>(emptyList())
    /** Ultime coppie (utente → JARVIS) per lo storico dual-pane. */
    val history: StateFlow<List<DialogTurn>> = _history.asStateFlow()

    private val _statusMessage = MutableStateFlow<String?>(null)
    /** Messaggio d'errore leggibile (rete/config/token/timeout). Valorizzato in stato ERROR,
     *  azzerato appena si esce da ERROR. La UI lo mostra al posto della label generica. */
    val statusMessage: StateFlow<String?> = _statusMessage.asStateFlow()

    // ── Audio routing (commutabile: locale vs watch) ────────────────────────
    private val localPlayer = TtsPlayer()
    private val opus = OpusDecoderWrapper()

    /** Se true, il mic locale del telefono non viene usato: i frame arrivano via feedMicFrame(). */
    @Volatile var relayMode: Boolean = false

    /** Se non null, la TTS decodificata va qui (es. writer verso lo watch) invece che allo speaker. */
    @Volatile var remoteTtsWriter: ((ShortArray) -> Unit)? = null

    /** Callback per far partire/fermare il mic locale (impostato dal service telefono). */
    @Volatile var onLocalMicStart: (() -> Unit)? = null
    @Volatile var onLocalMicStop: (() -> Unit)? = null

    /** Lato audio delle sessioni live hub (telefono: service; watch: WatchRelay). */
    @Volatile var liveAudio: LiveAudioPort? = null

    private val _liveHub = MutableStateFlow(false)
    /** true durante una sessione live full-duplex sul Voice Live Hub (UI a due corsie). */
    val liveHub: StateFlow<Boolean> = _liveHub.asStateFlow()

    private val _jarvisLevel = MutableStateFlow(0f)
    /** Livello 0..1 del parlato del modello in sessione hub (corsia JARVIS); amplitude = corsia utente. */
    val jarvisLevel: StateFlow<Float> = _jarvisLevel.asStateFlow()

    private val hubLock = Any()
    @Volatile private var hub: HubClient? = null
    @Volatile private var hubAudioOn = false
    /** live_session_end non consegnato (WS giù): parte al welcome della prossima connessione. */
    @Volatile private var pendingLiveEnd: String? = null

    private var ws: WebSocket? = null
    @Volatile private var streaming = false
    @Volatile private var awaitingReady = false
    @Volatile private var liveSession = false
    private val lastActivity = AtomicLong(System.currentTimeMillis())
    private val stateSince = AtomicLong(System.currentTimeMillis())
    private val stateSeq = AtomicLong()   // conta le transizioni (attese a tempo che non devono scavalcarle)

    init {
        scope.launch { ttlLoop() }
        scope.launch { watchdog() }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Gesto unico: TAP sulla testa
    // ─────────────────────────────────────────────────────────────────────────
    fun onTap() {
        touch()
        scope.launch {
            // In sessione hub il tap (telefono o watch) chiude la sessione.
            if (hub != null) { endHubSession(null, EndReason.BUTTON); return@launch }
            when (_state.value) {
                HeadState.IDLE, HeadState.ERROR -> startTurn()  // tap su errore = ritenta
                HeadState.LISTENING -> flush()          // secondo tap
                HeadState.THINKING, HeadState.SPEAKING -> bargeStop()
                HeadState.CONNECTING -> { /* ignora, in transito */ }
            }
        }
    }

    fun stopEverything() {
        scope.launch {
            if (hub != null) endHubSession(null, EndReason.BUTTON) else bargeStop()
        }
    }

    /** Azione "Termina" della UI in sessione live hub. */
    fun endLiveSession() {
        scope.launch { endHubSession(null, EndReason.BUTTON) }
    }

    /**
     * Chiude SUBITO l'eventuale sessione hub perché il lato audio non la regge più (service
     * distrutto, tunnel watch chiuso, watch senza mic) e avvisa l'orchestrator con [reason].
     * Senza sessione hub non fa nulla.
     */
    fun abortLiveSession(reason: String) {
        if (hub != null) endHubSession(null, reason)
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Frame mic in ingresso (dal mic locale o dallo watch)
    // ─────────────────────────────────────────────────────────────────────────
    fun feedMicFrame(frame: ByteArray) {
        val h = hub
        if (h != null) {
            // Sessione hub: mic continuo verso l'hub, anche mentre il modello parla.
            if (hubAudioOn) {
                h.sendAudio(frame)
                // Mic live senza gain x8: stessa curva, riportata in scala per la corsia utente.
                _amplitude.value = frameLevel(frame, LiveAudio.LIVE_LEVEL_BOOST)
            }
            return
        }
        if (streaming) {
            ws?.send(frame.toByteString())
            _amplitude.value = frameLevel(frame)
        }
    }

    /** RMS del frame PCM int16 LE → 0..1 (con curva per renderlo vivo visivamente). */
    private fun frameLevel(frame: ByteArray, boost: Float = 1f): Float {
        val n = frame.size / 2
        if (n == 0) return 0f
        var sum = 0.0
        var i = 0
        while (i + 1 < frame.size) {
            val v = ((frame[i].toInt() and 0xFF) or (frame[i + 1].toInt() shl 8)).toShort().toInt()
            sum += v.toDouble() * v
            i += 2
        }
        val rms = Math.sqrt(sum / n) / 32768.0
        return (rms * 9.0 * boost).coerceIn(0.0, 1.0).toFloat()  // sensibilità waveform
    }

    // ─────────────────────────────────────────────────────────────────────────
    // State machine
    // ─────────────────────────────────────────────────────────────────────────
    private suspend fun startTurn() {
        ensureConnected()
        if (ws == null) return   // connessione fallita: ensureConnected ha già segnalato
        awaitingReady = true
        setState(HeadState.LISTENING)
        sendJson(JSONObject().put("type", MsgType.AUDIO_START).put("codec", Codec.PCM))
        // streaming parte davvero su 'ready' (vedi handleText)
    }

    private fun flush() {
        setState(HeadState.THINKING)
        streaming = false
        sendJson(JSONObject().put("type", MsgType.AUDIO_FLUSH))
    }

    private fun bargeStop() {
        streaming = false
        awaitingReady = false
        onLocalMicStop?.invoke()
        sendJson(JSONObject().put("type", MsgType.SPEAKER_STOP))
        localPlayer.stop()
        setState(HeadState.IDLE)
    }

    private suspend fun ensureConnected() {
        connectMutex.withLock {
            if (ws != null) return
            if (!config.isConfigured) {
                Log.w(tag, "Orchestrator non configurato (URL mancante)")
                flashError("Configura l'indirizzo dell'orchestratore nelle impostazioni")
                return
            }
            setState(HeadState.CONNECTING)
            // Tollerante sullo schema: accetta ws://, wss://, http(s):// o solo host:porta.
            val raw = config.url.trim()
            val base = when {
                raw.startsWith("ws://") -> "http://" + raw.removePrefix("ws://")
                raw.startsWith("wss://") -> "https://" + raw.removePrefix("wss://")
                raw.startsWith("http://") || raw.startsWith("https://") -> raw
                else -> "http://$raw"
            }
            var url = "$base/ws/audio?device_id=${deviceIdOverride ?: config.deviceId}"
            if (config.token.isNotBlank()) url += "&token=${config.token}"

            val req = Request.Builder().url(url).build()
            ws = http.newWebSocket(req, Listener())
            sendJson(
                JSONObject()
                    .put("type", MsgType.HELLO)
                    .put("fw", FW)
                    // Classificazione esplicita lato orchestrator (fallback: euristica fw)
                    .put("device_type", if (relayMode) "AndroidWear" else "AndroidPhone")
            )
        }
    }

    private fun closeConnection(reason: String) {
        streaming = false
        awaitingReady = false
        onLocalMicStop?.invoke()
        localPlayer.stop()
        ws?.close(1000, reason)
        ws = null
        setState(HeadState.IDLE)
        Log.i(tag, "WS chiusa: $reason")
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Eventi dall'orchestrator
    // ─────────────────────────────────────────────────────────────────────────
    private fun handleText(text: String) {
        touch()
        val msg = runCatching { JSONObject(text) }.getOrNull() ?: return
        val type = msg.optString("type")
        // Sessione hub: il turn-loop dell'orchestrator è fuori dal giro, i suoi eventi di
        // turno non devono toccare stato/mic (altrimenti tts_done → IDLE e lo watch si chiude).
        if (hub != null && type in TURN_EVENTS) {
            Log.w(tag, "'$type' ignorato in sessione hub")
            return
        }
        when (type) {
            MsgType.WELCOME -> flushPendingLiveEnd()
            MsgType.CONFIG_UPDATE -> {
                val spk = msg.optString("speaker_type", "internal")
                if (spk != "internal") Log.w(tag, "speaker_type=$spk — imposta use_internal_speaker=true!")
            }
            MsgType.READY -> {
                // Accetta solo il ready del turno che ABBIAMO chiesto: il server (compat
                // legacy AtomS3R) auto-crea una sessione se gli arriva un frame vagante e
                // risponde ready — senza guard il mic si riarmerebbe da solo in loop.
                if (!awaitingReady) {
                    Log.w(tag, "ready non richiesto (nessun turno pendente) — ignorato")
                    return
                }
                awaitingReady = false
                streaming = true
                setState(HeadState.LISTENING)
                if (!relayMode) onLocalMicStart?.invoke()
            }
            MsgType.SPEECH_END -> {
                streaming = false
                if (!relayMode) onLocalMicStop?.invoke()
                setState(HeadState.THINKING)
            }
            MsgType.TTS_START -> {
                if (remoteTtsWriter == null) localPlayer.start()
                setState(HeadState.SPEAKING)
            }
            MsgType.TTS_DONE -> {
                // Il path "session timeout without speech" del server manda SOLO tts_done
                // (niente speech_end): senza questo reset streaming resterebbe armato e i
                // frame residui del watch verrebbero inoltrati, innescando l'auto-create
                // legacy lato server (loop zombie da 62s).
                streaming = false
                if (!relayMode) onLocalMicStop?.invoke()
                setState(HeadState.IDLE)
            }
            MsgType.TRIGGER_LISTEN -> scope.launch {
                // Multiturn: l'orchestrator manda trigger_listen ma il device sta ancora
                // riproducendo la coda del TTS della domanda → attendo che finisca di suonare,
                // altrimenti il mic la ricattura come risposta ("...intendi?").
                delay(TRIGGER_LISTEN_DELAY_MS)
                startTurn()
            }
            MsgType.LIVE_SESSION_START -> when (val start = HubLiveStart.parse(msg)) {
                HubLiveStart.Legacy -> {
                    if (hub != null) endHubSession(null, null)
                    liveSession = true; setState(HeadState.LISTENING)
                }
                is HubLiveStart.Invalid -> {
                    Log.w(tag, "live_session_start hub non valido: ${start.reason}")
                    if (hub != null) endHubSession(null, null)
                    sendLiveEnd(start.callId, start.reason)
                    liveSession = false
                    afterHubEnd(start.callId, start.reason)
                }
                is HubLiveStart.Start -> startHubSession(start)
            }
            MsgType.LIVE_SESSION_END -> {
                // Fine decisa dall'orchestrator: per la sessione hub manda "ended" all'hub e
                // smonta, senza rispondere con un altro live_session_end.
                if (hub != null) endHubSession(null, null)
                else { liveSession = false; setState(HeadState.IDLE) }
            }
            MsgType.PING -> sendJson(JSONObject().put("type", MsgType.PONG))
            MsgType.ERROR -> {
                val m = (msg.opt("msg") ?: msg.opt("message"))?.toString()
                Log.w(tag, "server error: $m")
                // Non interrompere la riproduzione in corso; altrove segnala l'errore all'utente.
                if (_state.value != HeadState.SPEAKING && hub == null) {
                    flashError(m?.takeIf { it.isNotBlank() } ?: "Errore dall'orchestratore")
                }
            }
            MsgType.TRANSCRIPT -> onTranscript(msg.optString("text"))
            MsgType.RESPONSE -> onResponse(msg.optString("text"))
        }
    }

    // ── Storico conversazione ────────────────────────────────────────────────
    private fun onTranscript(text: String) {
        if (text.isBlank()) return
        val list = _history.value.toMutableList()
        list.add(DialogTurn(user = text))
        while (list.size > MAX_HISTORY) list.removeAt(0)
        _history.value = list
    }

    private fun onResponse(text: String) {
        if (text.isBlank()) return
        val list = _history.value.toMutableList()
        val idx = list.indexOfLast { it.jarvis == null }  // aggancia all'ultimo turno aperto
        if (idx >= 0) {
            list[idx] = list[idx].copy(jarvis = text)
        } else {
            list.add(DialogTurn(jarvis = text))
            while (list.size > MAX_HISTORY) list.removeAt(0)
        }
        _history.value = list
    }

    private fun handleBinary(bytes: ByteString) {
        touch()
        if (hub != null) return   // in sessione hub il parlato arriva dall'hub, non dalla WS
        val pcm = opus.decode(bytes.toByteArray())
        if (pcm.isEmpty()) return
        val remote = remoteTtsWriter
        if (remote != null) remote(pcm) else localPlayer.write(pcm)
    }

    private inner class Listener : WebSocketListener() {
        override fun onMessage(webSocket: WebSocket, text: String) = handleText(text)
        override fun onMessage(webSocket: WebSocket, bytes: ByteString) = handleBinary(bytes)
        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            val code = response?.code
            Log.w(tag, "WS failure: ${t.message} (http $code)")
            if (ws !== webSocket) return
            ws = null
            streaming = false
            awaitingReady = false
            onLocalMicStop?.invoke()
            // Sessione hub senza WS: l'orchestrator non potrebbe più chiuderla né sapere che
            // finisce. Si chiude qui; il live_session_end parte alla prossima connessione.
            if (hub != null) endHubSession(null, EndReason.ERROR)
            val msg = when {
                code == 401 || code == 403 -> "Accesso negato: token non valido o mancante"
                code != null && code >= 500 -> "Errore dell'orchestratore ($code)"
                else -> "Orchestratore irraggiungibile. Controlla rete e VPN."
            }
            flashError(msg)
        }
        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (ws === webSocket) {
                ws = null; streaming = false; awaitingReady = false
                if (hub != null) endHubSession(null, EndReason.ERROR)   // come onFailure
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Sessione live su Voice Live Hub (full-duplex, orchestrator fuori dal loop audio)
    // ─────────────────────────────────────────────────────────────────────────
    private fun startHubSession(spec: HubLiveStart.Start) {
        if (hub != null) endHubSession(null, null)   // sessione precedente orfana
        // Chiudi il turno half-duplex in corso: da qui il mic lo gestisce la modalità live.
        streaming = false
        awaitingReady = false
        onLocalMicStop?.invoke()
        localPlayer.stop()
        if (liveAudio == null) {
            Log.w(tag, "sessione hub senza lato audio (service/relay non pronti)")
            sendLiveEnd(spec.callId, EndReason.ERROR)
            setState(HeadState.IDLE)
            return
        }
        val events = HubEvents()
        // Watch: VAD meno sensibile all'eco del suo speaker (il telefono resta al default dell'hub).
        val offerSpec = if (relayMode) spec.copy(vad = HubLiveStart.SPEAKER_NEAR_MIC_VAD) else spec
        val client = hubClientFactory(offerSpec, events)
        events.client = client
        synchronized(hubLock) {
            hub = client; hubAudioOn = false
            liveAudio?.prepare()   // watch: modalità live (mic AEC) già prima dell'accept
        }
        liveSession = true
        _liveHub.value = true
        setState(HeadState.LISTENING)
        Log.i(tag, "sessione hub ${spec.callId} → ${spec.host}:${spec.port}")
        client.start()
    }

    /**
     * Smonta la sessione hub. [expected] != null: solo se è ancora quella (callback tardivi).
     * [reason] != null: avvisa l'orchestrator con live_session_end(reason).
     */
    private fun endHubSession(expected: HubClient?, reason: String?) {
        val c: HubClient
        synchronized(hubLock) {
            c = hub ?: return
            if (expected != null && c !== expected) return
            hub = null
            hubAudioOn = false
            c.end()   // "ended" all'hub, no-op se ha già chiuso lui
            // Sotto hubLock come lo start() dell'accept: uno stop tardivo non può spegnere
            // il lato audio di una sessione nuova. Idempotente (anche dopo il solo prepare()).
            liveAudio?.stop()
        }
        Log.i(tag, "sessione hub ${c.callId} chiusa (${reason ?: "orchestrator"})")
        // Stato locale PRIMA di avvisare l'orchestrator: il suo live_session_start di ripiego
        // non deve trovare ancora liveSession=true ed essere poi sovrascritto
        if (hub == null) {   // altrimenti nel frattempo è partita una sessione nuova: stato suo
            _liveHub.value = false
            _jarvisLevel.value = 0f
            liveSession = false
            if (reason != null) afterHubEnd(c.callId, reason) else setState(HeadState.IDLE)
        }
        if (reason != null) sendLiveEnd(c.callId, reason)
    }

    /**
     * Stato dopo un live_session_end mandato da noi. Se l'orchestrator ripiega sulla sessione
     * live di oggi (hub che rifiuta / non risponde, error) il device NON entra da
     * solo nella vecchia modalità: resta in attesa (THINKING, lo watch non si chiude) del
     * live_session_start semplice. Se non arriva nulla entro [fallbackWaitMs] torna IDLE.
     */
    private fun afterHubEnd(callId: String, reason: String) {
        if (!EndReason.awaitsFallback(callId, reason)) { setState(HeadState.IDLE); return }
        setState(HeadState.THINKING)
        val seq = stateSeq.get()
        scope.launch {
            delay(fallbackWaitMs)
            if (stateSeq.get() == seq && hub == null && !liveSession) {
                Log.w(tag, "nessun ripiego dall'orchestrator dopo $reason")
                setState(HeadState.IDLE)
            }
        }
    }

    private fun sendLiveEnd(callId: String, reason: String) {
        val text = JSONObject().put("type", MsgType.LIVE_SESSION_END).put("call_id", callId).put("reason", reason)
            .toString()
        if (ws?.send(text) != true) {
            // WS giù: l'orchestrator deve saperlo comunque (altrimenti tiene una sessione hub
            // fantasma). Resta solo l'ultimo; parte al welcome della prossima connessione.
            pendingLiveEnd = text
            Log.w(tag, "live_session_end ($reason) in attesa della WS")
        }
    }

    private fun flushPendingLiveEnd() {
        val text = pendingLiveEnd ?: return
        if (ws?.send(text) == true) {
            pendingLiveEnd = null
            Log.i(tag, "live_session_end in attesa consegnato")
        }
    }

    /** Eventi del HubClient (thread di lettura del socket). */
    private inner class HubEvents : HubClient.Listener {
        lateinit var client: HubClient

        override fun onAccepted() {
            val port = liveAudio
            val ok = synchronized(hubLock) {
                if (hub !== client) return
                val started = port?.start { lvl -> _jarvisLevel.value = lvl } == true
                hubAudioOn = started
                started
            }
            if (!ok) {
                Log.w(tag, "modalità live non avviata (permesso mic / watch assente)")
                endHubSession(client, EndReason.ERROR)
            }
        }

        override fun onAudio(pcm: ShortArray) {
            if (hub === client && hubAudioOn) liveAudio?.play(pcm)
        }

        override fun onClear() {
            if (hub === client && hubAudioOn) liveAudio?.clear()
        }

        override fun onEnded(reason: String) = endHubSession(client, reason)
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Utility
    // ─────────────────────────────────────────────────────────────────────────
    private fun sendJson(obj: JSONObject) { ws?.send(obj.toString()) }
    private fun setState(s: HeadState) {
        if (s != HeadState.ERROR) _statusMessage.value = null  // il messaggio vive solo in ERROR
        _state.value = s
        stateSeq.incrementAndGet()
        stateSince.set(System.currentTimeMillis())
        if (s != HeadState.LISTENING) _amplitude.value = 0f
    }

    /**
     * Watchdog anti-stuck: se uno stato d'attesa resta appeso troppo a lungo (es. nessun
     * tts_done arriva → THINKING infinito), resetta a IDLE (o ERROR per CONNECTING).
     * In live session i tempi sono più lunghi → nessun timeout.
     */
    private suspend fun watchdog() {
        while (true) {
            delay(2_000)
            if (liveSession) { stateSince.set(System.currentTimeMillis()); continue }
            val s = _state.value
            val limit = when (s) {
                HeadState.CONNECTING -> 12_000L
                // LISTENING sotto i 60s del session timeout server: così il watchdog vince
                // la corsa e ferma mic+streaming PRIMA che il server mandi il tts_done nudo.
                HeadState.LISTENING -> 55_000L
                HeadState.THINKING -> 45_000L
                HeadState.SPEAKING -> 90_000L
                else -> Long.MAX_VALUE   // IDLE / ERROR: nessun timeout
            }
            if (System.currentTimeMillis() - stateSince.get() > limit) {
                Log.w(tag, "watchdog: stato $s bloccato → reset")
                streaming = false
                awaitingReady = false
                onLocalMicStop?.invoke()
                localPlayer.stop()
                if (s == HeadState.CONNECTING) {
                    ws?.cancel(); ws = null
                    flashError("Timeout: l'orchestratore non risponde")
                } else {
                    setState(HeadState.IDLE)
                }
            }
        }
    }
    private fun touch() { lastActivity.set(System.currentTimeMillis()) }

    /**
     * Mostra ERROR con un messaggio leggibile, poi torna IDLE se nessun'altra transizione
     * è avvenuta. [message] null = usa la label generica di ERROR in UI.
     */
    private fun flashError(message: String? = null) {
        _statusMessage.value = message
        setState(HeadState.ERROR)
        scope.launch {
            delay(ERROR_FLASH_MS)
            if (_state.value == HeadState.ERROR) setState(HeadState.IDLE)
        }
    }

    private suspend fun ttlLoop() {
        while (true) {
            delay(1_000)
            if (ws == null) continue
            if (liveSession || _state.value != HeadState.IDLE) { touch(); continue }
            if (System.currentTimeMillis() - lastActivity.get() > config.ttlMillis) {
                closeConnection("idle > ${config.ttlMillis}ms")
            }
        }
    }

    private companion object {
        const val FW = "android-mobile-0.1.0"
        const val MAX_HISTORY = 10
        const val TRIGGER_LISTEN_DELAY_MS = 800L  // attesa fine coda TTS prima del multiturn
        const val ERROR_FLASH_MS = 3600L          // durata visualizzazione messaggio d'errore
        const val FALLBACK_WAIT_MS = 5_000L       // attesa del ripiego dell'orchestrator dopo hub_rejected/unreachable

        /** Eventi del turn-loop orchestrator ignorati durante una sessione hub. */
        val TURN_EVENTS = setOf(
            MsgType.READY, MsgType.SPEECH_END, MsgType.TTS_START, MsgType.TTS_DONE, MsgType.TRIGGER_LISTEN,
        )
    }
}

/** Una coppia di turno conversazione per lo storico (ts = per ordinamento cronologico). */
data class DialogTurn(
    val user: String? = null,
    val jarvis: String? = null,
    val ts: Long = System.currentTimeMillis(),
)
