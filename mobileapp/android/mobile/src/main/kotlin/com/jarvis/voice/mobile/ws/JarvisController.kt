package com.jarvis.voice.mobile.ws

import android.util.Log
import com.jarvis.voice.mobile.audio.OpusDecoderWrapper
import com.jarvis.voice.mobile.config.ControllerConfig
import com.jarvis.voice.mobile.live.LiveAudioPort
import com.jarvis.voice.shared.HeadState
import com.jarvis.voice.shared.audio.LiveAudio
import com.jarvis.voice.shared.audio.TtsPlayer
import com.jarvis.voice.shared.hub.CommandBuffer
import com.jarvis.voice.shared.hub.HubClient
import com.jarvis.voice.shared.hub.HubLiveStart
import com.jarvis.voice.shared.protocol.Codec
import com.jarvis.voice.shared.protocol.EndReason
import com.jarvis.voice.shared.protocol.LiveWake
import com.jarvis.voice.shared.protocol.MsgType
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
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
 *
 * Fase 3 (design-voice-live-hub.md sez. 19, decisioni 19.12): sul device abilitato (flag
 * `live_on_wake` del welcome, in cache) il tap in IDLE apre subito la sessione hub:
 *   tap → mic live acceso ([LiveAudioPort.capture]) nel buffer del comando → live_start{req_id,
 *   trigger:"tap"} → live_session_start{req_id} entro 2,5 s → offer con offer_extra (command dal
 *   VAD sul buffer) → accept tardivo entro 7 s → buffer all'hub per primo, poi mic dal vivo, e
 *   live_session_active all'orchestrator.
 * live_declined (salvo duplicate/voice_gate), timeout (con live_cancel) o hub che fallisce prima
 * dell'accept → subito lo [startTurn] di oggi: l'orchestrator con origine tap non ripiega.
 * Flag spento (o interruttore dell'app spento): il tap fa lo startTurn di oggi, identico.
 */
class JarvisController(
    private val config: ControllerConfig,
    private val deviceIdOverride: String? = null,   // usato dal relay watch (device_id distinto)
    /** (spec, offer, listener): `offer` gira sul thread dell'hub subito prima dell'offer. */
    private val hubClientFactory: (HubLiveStart.Start, () -> String, HubClient.Listener) -> HubClient =
        { spec, offer, l -> defaultHubClient(spec, offer, l) },
    private val fallbackWaitMs: Long = FALLBACK_WAIT_MS,
    private val liveAnswerTimeoutMs: Long = LiveWake.ANSWER_TIMEOUT_MS,
    private val commandWindowMs: Long = COMMAND_WINDOW_MS,
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
    /** Spec della sessione hub in corso (origine, req_id) e accept già ricevuto. Sotto hubLock. */
    @Volatile private var hubSpec: HubLiveStart.Start? = null
    @Volatile private var hubAccepted = false

    // ── Fase 3: tap che chiede la sessione hub ───────────────────────────────
    private enum class Phase { ORCH, HUB }

    /**
     * Tap in attesa: [Phase.ORCH] = live_start mandato, attesa della risposta (2,5 s);
     * [Phase.HUB] = handshake con l'hub, fino all'accept. Il buffer del comando cresce dal tap
     * all'accept. Transizioni sotto hubLock.
     */
    private class LivePending(val reqId: String, val t0: Long) {
        @Volatile var phase = Phase.ORCH
        val buffer = CommandBuffer()
        @Volatile var captured = false
        @Volatile var timer: Job? = null
    }

    @Volatile private var pending: LivePending? = null

    /** Flag live_on_wake in memoria (device_id → valore); senza, quello persistito in config. */
    @Volatile private var liveFlag: Pair<String, Boolean>? = null
    /** device_id con cui è stata aperta la WS corrente (il welcome parla di questo). */
    @Volatile private var wsDeviceId: String = ""

    /** Fase 3: cue al tap che chiede la sessione live (telefono: vibrazione). */
    @Volatile var onLiveRequest: (() -> Unit)? = null
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
            // Tap mentre la sessione live chiesta col tap si sta aprendo: l'utente rinuncia.
            pending?.let { abortPending(it, "tap"); return@launch }
            // In sessione hub il tap (telefono o watch) chiude la sessione.
            if (hub != null) { endHubSession(null, EndReason.BUTTON); return@launch }
            when (_state.value) {
                // tap su errore = ritenta
                HeadState.IDLE, HeadState.ERROR -> if (wantsLiveOnTap()) startLiveRequest() else startTurn()
                HeadState.LISTENING -> flush()          // secondo tap
                HeadState.THINKING, HeadState.SPEAKING -> bargeStop()
                HeadState.CONNECTING -> { /* ignora, in transito */ }
            }
        }
    }

    fun stopEverything() {
        scope.launch {
            val p = pending
            when {
                p != null -> abortPending(p, "stop")
                hub != null -> endHubSession(null, EndReason.BUTTON)
                else -> bargeStop()
            }
        }
    }

    /** Azione "Termina" della UI in sessione live hub. */
    fun endLiveSession() {
        scope.launch {
            val p = pending
            if (p != null) abortPending(p, "ui") else endHubSession(null, EndReason.BUTTON)
        }
    }

    /**
     * Chiude SUBITO l'eventuale sessione hub perché il lato audio non la regge più (service
     * distrutto, tunnel watch chiuso, watch senza mic) e avvisa l'orchestrator con [reason].
     * Senza sessione hub non fa nulla.
     */
    fun abortLiveSession(reason: String) {
        val p = pending
        if (p != null) abortPending(p, "audio: $reason")   // live mai partita: live_cancel e a riposo
        else if (hub != null) endHubSession(null, reason)
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
                return
            }
            // Handshake di una sessione chiesta col tap: il mic va nel buffer del comando. Sotto
            // hubLock come l'accept, che manda il buffer e poi accende hubAudioOn: nessun frame
            // dal vivo scavalca il buffer, nessun frame finisce nel buffer dopo che è partito.
            // (Senza tap in attesa, cioè "avvia una sessione live", il frame si scarta come oggi.)
            var used = false
            synchronized(hubLock) {
                if (hub === h) {
                    val p = pending
                    if (hubAudioOn) { h.sendAudio(frame); used = true }
                    else if (p != null && p.phase == Phase.HUB) { p.buffer.add(frame); used = true }
                }
            }
            if (used) _amplitude.value = frameLevel(frame, LiveAudio.LIVE_LEVEL_BOOST)
            return
        }
        val p = pending
        if (p != null) {
            // Attesa della risposta dell'orchestrator: il comando detto dopo il tap
            p.buffer.add(frame)
            _amplitude.value = frameLevel(frame, LiveAudio.LIVE_LEVEL_BOOST)
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
            wsDeviceId = currentDeviceId()
            var url = "$base/ws/audio?device_id=$wsDeviceId"
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
        // Stessa cosa con un tap in attesa della sessione live: nessun turno è stato chiesto.
        if ((hub != null || pending != null) && type in TURN_EVENTS) {
            Log.w(tag, "'$type' ignorato in sessione hub")
            return
        }
        when (type) {
            MsgType.WELCOME -> { updateLiveFlag(msg); flushPendingLiveEnd() }
            MsgType.LIVE_ON_WAKE -> updateLiveFlag(msg)
            MsgType.LIVE_DECLINED -> onLiveDeclined(msg.optString("req_id"), msg.optString("reason"))
            MsgType.ANNOUNCE -> relayAnnounce(msg)
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
            MsgType.LIVE_SESSION_START -> if (!onLiveSessionStartForRequest(msg)) when (val start = HubLiveStart.parse(msg)) {
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
                val p = pending
                if (p != null && hub == null) {
                    // Tap in attesa della risposta: l'orchestrator ha chiuso tutto, a riposo
                    if (takePending(p)) releaseCapture(p)
                    liveSession = false; setState(HeadState.IDLE)
                } else if (hub != null) endHubSession(null, null)
                else { liveSession = false; setState(HeadState.IDLE) }
            }
            MsgType.PING -> sendJson(JSONObject().put("type", MsgType.PONG))
            MsgType.ERROR -> {
                val m = (msg.opt("msg") ?: msg.opt("message"))?.toString()
                Log.w(tag, "server error: $m")
                // Non interrompere la riproduzione in corso; altrove segnala l'errore all'utente.
                if (_state.value != HeadState.SPEAKING && hub == null && pending == null) {
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
            // Tap in attesa della sessione live senza orchestrator: niente live e niente classico
            // (fallirebbe anche lui); il live_cancel parte alla prossima connessione.
            pending?.let { abortPending(it, "ws persa", toIdle = false) }
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
                pending?.let { abortPending(it, "ws chiusa") }   // come onFailure
                if (hub != null) endHubSession(null, EndReason.ERROR)   // come onFailure
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Sessione live su Voice Live Hub (full-duplex, orchestrator fuori dal loop audio)
    // ─────────────────────────────────────────────────────────────────────────
    /** [p] != null: risposta al live_start di un tap (origine tap, buffer del comando in corso). */
    private fun startHubSession(spec: HubLiveStart.Start, p: LivePending? = null) {
        if (hub != null) endHubSession(null, null)   // sessione precedente orfana
        // Chiudi il turno half-duplex in corso: da qui il mic lo gestisce la modalità live.
        streaming = false
        awaitingReady = false
        onLocalMicStop?.invoke()
        localPlayer.stop()
        if (liveAudio == null) {
            Log.w(tag, "sessione hub senza lato audio (service/relay non pronti)")
            sendLiveEnd(spec.callId, EndReason.ERROR)
            if (p != null && takePending(p)) { releaseCapture(p); fallbackToClassic("lato audio assente"); return }
            setState(HeadState.IDLE)
            return
        }
        val events = HubEvents()
        // Watch: VAD meno sensibile all'eco del suo speaker (il telefono resta al default dell'hub).
        val offerSpec = if (relayMode) spec.copy(vad = HubLiveStart.SPEAKER_NEAR_MIC_VAD) else spec
        // Tap: offer_extra.command dal VAD sul buffer, deciso dopo la connect (il buffer cresce
        // ancora) e al più tardi a [commandWindowMs] dal tap; appena c'è parlato si parte.
        val offer: () -> String = if (p != null) {
            { offerSpec.offerJson(command = decideCommand(p)) }
        } else {
            { offerSpec.offerJson() }
        }
        val client = hubClientFactory(offerSpec, offer, events)
        events.client = client
        synchronized(hubLock) {
            if (p != null) {
                if (pending !== p) return   // annullato nel frattempo (tap, timeout)
                p.phase = Phase.HUB
                p.timer?.cancel(); p.timer = null
            }
            hub = client; hubAudioOn = false
            hubSpec = spec; hubAccepted = false
            liveAudio?.prepare()   // watch: modalità live (mic AEC) già prima dell'accept
        }
        liveSession = true
        _liveHub.value = true
        setState(HeadState.LISTENING)
        Log.i(tag, "sessione hub ${spec.callId} → ${spec.host}:${spec.port}" +
            (if (spec.origin.isNotEmpty()) " (origine ${spec.origin}${if (p != null) ", req ${p.reqId}" else ""})" else ""))
        client.start()
    }

    private fun decideCommand(p: LivePending): Boolean {
        val command = p.buffer.awaitCommand(p.t0 + commandWindowMs)
        Log.i(tag, "buffer del comando ${p.buffer.length / CommandBuffer.BYTES_PER_MS} ms, " +
            "parlato ${p.buffer.speechMs()} ms → command=$command (req ${p.reqId})")
        return command
    }

    /**
     * Smonta la sessione hub. [expected] != null: solo se è ancora quella (callback tardivi).
     * [reason] != null: avvisa l'orchestrator con live_session_end(reason).
     */
    private fun endHubSession(expected: HubClient?, reason: String?) {
        val c: HubClient
        val spec: HubLiveStart.Start?
        val accepted: Boolean
        synchronized(hubLock) {
            c = hub ?: return
            if (expected != null && c !== expected) return
            hub = null
            hubAudioOn = false
            spec = hubSpec; hubSpec = null
            accepted = hubAccepted; hubAccepted = false
            pending?.let { p -> if (p.phase == Phase.HUB) { pending = null; p.timer?.cancel(); p.timer = null } }
            c.end()   // "ended" all'hub, no-op se ha già chiuso lui
            // Sotto hubLock come lo start() dell'accept: uno stop tardivo non può spegnere
            // il lato audio di una sessione nuova. Idempotente (anche dopo il solo prepare()).
            liveAudio?.stop()
        }
        Log.i(tag, "sessione hub ${c.callId} chiusa (${reason ?: "orchestrator"})")
        val fromRequest = spec?.fromRequest == true
        if (fromRequest && !accepted && reason != null) {
            // Tap la cui sessione non è mai partita (19.2/19.3): l'orchestrator la toglie in
            // silenzio, senza ripiego. Rinuncia dell'utente → live_cancel; guasto dell'hub →
            // live_session_end{reason,detail} e SUBITO il turno classico di oggi.
            if (hub == null) {
                _liveHub.value = false
                _jarvisLevel.value = 0f
                liveSession = false
            }
            if (reason == EndReason.BUTTON) {
                sendLiveCancel(spec!!.reqId)
                if (hub == null) setState(HeadState.IDLE)
                return
            }
            sendLiveEnd(c.callId, reason, c.rejectDetail)
            if (hub == null) {
                if (EndReason.awaitsFallback(c.callId, reason)) fallbackToClassic("hub: $reason")
                else setState(HeadState.IDLE)
            }
            return
        }
        // Stato locale PRIMA di avvisare l'orchestrator: il suo live_session_start di ripiego
        // non deve trovare ancora liveSession=true ed essere poi sovrascritto
        if (hub == null) {   // altrimenti nel frattempo è partita una sessione nuova: stato suo
            _liveHub.value = false
            _jarvisLevel.value = 0f
            liveSession = false
            when {
                reason == null -> setState(HeadState.IDLE)
                // Sessione dal tap già avviata: l'orchestrator non converte, niente da aspettare
                fromRequest -> setState(HeadState.IDLE)
                else -> afterHubEnd(c.callId, reason)
            }
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

    private fun sendLiveEnd(callId: String, reason: String, detail: String = "") {
        val o = JSONObject().put("type", MsgType.LIVE_SESSION_END).put("call_id", callId).put("reason", reason)
        if (detail.isNotEmpty()) o.put("detail", detail)
        sendOrKeep(o.toString(), "live_session_end ($reason)")
    }

    /** live_cancel{req_id}: l'orchestrator scarta la richiesta o toglie in silenzio la sessione nata da lì. */
    private fun sendLiveCancel(reqId: String) {
        if (reqId.isEmpty()) return
        sendOrKeep(JSONObject().put("type", MsgType.LIVE_CANCEL).put("req_id", reqId).toString(), "live_cancel")
    }

    private fun sendOrKeep(text: String, what: String) {
        if (ws?.send(text) != true) {
            // WS giù: l'orchestrator deve saperlo comunque (altrimenti tiene una sessione hub
            // fantasma). Resta solo l'ultimo; parte al welcome della prossima connessione.
            pendingLiveEnd = text
            Log.w(tag, "$what in attesa della WS")
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
            var spec: HubLiveStart.Start? = null
            var primedMs = -1
            val ok = synchronized(hubLock) {
                if (hub !== client) return
                val started = port?.start { lvl -> _jarvisLevel.value = lvl } == true
                if (started) {
                    spec = hubSpec
                    hubAccepted = true
                    // Tap: il buffer del comando va all'hub per primo, poi il mic dal vivo
                    // (hubAudioOn acceso dopo, sotto lo stesso lock di feedMicFrame).
                    val p = pending
                    if (p != null && p.phase == Phase.HUB) {
                        pending = null
                        p.timer?.cancel(); p.timer = null
                        primedMs = client.prime(p.buffer.snapshot()) / CommandBuffer.BYTES_PER_MS
                    }
                }
                hubAudioOn = started
                started
            }
            if (!ok) {
                Log.w(tag, "modalità live non avviata (permesso mic / watch assente)")
                endHubSession(client, EndReason.ERROR)
                return
            }
            if (primedMs >= 0) Log.i(tag, "accept hub ${client.callId}: buffer del comando $primedMs ms all'hub")
            // Da qui l'orchestrator muta la stanza (origine tap) e inoltra gli annunci (19.12 D6)
            val active = JSONObject().put("type", MsgType.LIVE_SESSION_ACTIVE).put("call_id", client.callId)
            spec?.reqId?.takeIf { it.isNotEmpty() }?.let { active.put("req_id", it) }
            sendJson(active)
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

    // ─────────────────────────────────────────────────────────────────────────
    // Fase 3: tap → sessione hub (live_start / live_declined / live_cancel)
    // ─────────────────────────────────────────────────────────────────────────
    private fun currentDeviceId(): String = deviceIdOverride ?: config.deviceId

    /** Il tap chiede la sessione live: interruttore dell'app, flag del device in cache, lato audio pronto. */
    private fun wantsLiveOnTap(): Boolean {
        if (!config.liveOnTapAllowed || liveAudio == null) return false
        val id = currentDeviceId()
        val f = liveFlag
        return if (f != null && f.first == id) f.second else config.cachedLiveOnWake(id)
    }

    /** live_on_wake dal welcome o dal push dell'admin: in cache per il prossimo tap. */
    private fun updateLiveFlag(msg: JSONObject) {
        if (!msg.has("live_on_wake")) return
        val enabled = msg.optBoolean("live_on_wake", false)
        val id = wsDeviceId.ifEmpty { currentDeviceId() }
        if (liveFlag?.first != id || liveFlag?.second != enabled) Log.i(tag, "live_on_wake=$enabled per $id")
        liveFlag = id to enabled
        config.cacheLiveOnWake(id, enabled)
    }

    private suspend fun startLiveRequest() {
        ensureConnected()
        if (ws == null) return   // connessione fallita: ensureConnected ha già segnalato
        val p = LivePending(newReqId(), System.currentTimeMillis())
        synchronized(hubLock) { pending = p }
        onLiveRequest?.invoke()
        // Mic della modalità live subito (watch: PATH_LIVE start, mai il mic di oggi): il comando
        // detto dopo il tap finisce nel buffer
        p.captured = liveAudio?.capture() == true
        if (!p.captured) Log.w(tag, "live_start senza buffer del comando (mic non disponibile)")
        if (pending !== p) { releaseCapture(p); return }   // annullato mentre partiva il mic
        // Watch: CONNECTING, non LISTENING, così non apre il mic di oggi prima di PATH_LIVE
        setState(if (relayMode) HeadState.CONNECTING else HeadState.LISTENING)
        val sent = ws?.send(
            JSONObject().put("type", MsgType.LIVE_START).put("req_id", p.reqId).put("trigger", LiveWake.TRIGGER_TAP)
                .toString()
        ) == true
        if (!sent) {
            if (takePending(p)) { releaseCapture(p); fallbackToClassic("live_start non inviato") }
            return
        }
        Log.i(tag, "live_start req ${p.reqId} (tap)")
        p.timer = scope.launch {
            delay(liveAnswerTimeoutMs)
            // Fase ricontrollata sotto hubLock (takePending): un live_session_start arrivato tra il
            // controllo e la presa avrebbe già acceso l'handshake, e qui partirebbe anche il classico.
            if (takePending(p, Phase.ORCH)) {
                Log.w(tag, "nessuna risposta al live_start ${p.reqId} in ${liveAnswerTimeoutMs} ms: classico")
                releaseCapture(p)
                sendLiveCancel(p.reqId)
                fallbackToClassic("timeout")
            }
        }
    }

    /**
     * live_session_start con req_id: risposta a un live_start. true = gestito qui (anche se
     * ignorato perché vecchio). Senza req_id: false, e se c'era un tap in attesa decade (la
     * sessione l'ha decisa l'orchestrator per altro) prima del percorso di oggi.
     */
    private fun onLiveSessionStartForRequest(msg: JSONObject): Boolean {
        val reqId = msg.optString("req_id")
        val p = pending
        if (reqId.isEmpty()) {
            if (p != null) abortPending(p, "live_session_start senza req_id", toIdle = false)
            return false
        }
        if (p == null || p.reqId != reqId || p.phase != Phase.ORCH) {
            Log.i(tag, "live_session_start per req $reqId ignorato (in attesa: ${p?.reqId ?: "-"})")
            return true
        }
        when (val start = HubLiveStart.parse(msg)) {
            is HubLiveStart.Start -> startHubSession(start, p)
            is HubLiveStart.Invalid -> {
                Log.w(tag, "live_session_start (req $reqId) non valido: ${start.reason}")
                if (!takePending(p, Phase.ORCH)) return true
                releaseCapture(p)
                if (start.callId.isNotEmpty()) sendLiveEnd(start.callId, start.reason) else sendLiveCancel(reqId)
                fallbackToClassic("live_session_start non valido")
            }
            HubLiveStart.Legacy -> {
                // Risposta a un live_start senza backend hub: non prevista, come il wakeword-server
                if (!takePending(p, Phase.ORCH)) return true
                releaseCapture(p)
                sendLiveCancel(reqId)
                fallbackToClassic("live_session_start senza hub")
            }
        }
        return true
    }

    private fun onLiveDeclined(reqId: String, reason: String) {
        val p = pending
        if (p == null || p.reqId != reqId || p.phase != Phase.ORCH) {
            Log.i(tag, "live_declined per req $reqId ignorato")
            return
        }
        if (!takePending(p, Phase.ORCH)) return
        releaseCapture(p)
        if (reason in LiveWake.SILENT_DECLINES) {
            Log.i(tag, "live_declined ($reason): a riposo senza turno classico")
            setState(HeadState.IDLE)
        } else {
            fallbackToClassic("live_declined ${reason.ifEmpty { "?" }}")
        }
    }

    /**
     * L'utente rinuncia (tap, stop, Termina) o la WS cade prima che la sessione parta:
     * live_cancel all'orchestrator (o alla prossima connessione), handshake con l'hub chiuso
     * SENZA live_session_end (lo copre il live_cancel), mic spento, a riposo.
     */
    private fun abortPending(p: LivePending, why: String, toIdle: Boolean = true) {
        val wasHub: Boolean
        synchronized(hubLock) {
            if (pending !== p) return
            pending = null
            p.timer?.cancel(); p.timer = null
            wasHub = p.phase == Phase.HUB
            if (wasHub) {
                hub?.end()
                hub = null; hubAudioOn = false; hubSpec = null; hubAccepted = false
                liveAudio?.stop()
            } else if (p.captured && hub == null) {
                liveAudio?.stop()
            }
        }
        Log.i(tag, "richiesta live ${p.reqId} annullata ($why)")
        if (wasHub) { _liveHub.value = false; _jarvisLevel.value = 0f; liveSession = false }
        sendLiveCancel(p.reqId)
        if (toIdle) setState(HeadState.IDLE)
    }

    /** Toglie [p] se è ancora il tap in attesa (e, con [phase], ancora in quella fase). false = già chiuso da altri. */
    private fun takePending(p: LivePending, phase: Phase? = null): Boolean = synchronized(hubLock) {
        if (pending !== p) return false
        if (phase != null && p.phase != phase) return false
        pending = null
        p.timer?.cancel(); p.timer = null
        true
    }

    /** Spegne il mic acceso al tap, se nessuna sessione hub lo ha preso in carico. */
    private fun releaseCapture(p: LivePending) {
        if (!p.captured) return
        synchronized(hubLock) { if (hub == null) liveAudio?.stop() }
    }

    /** Ripiego: il turno classico di oggi, subito (con origine tap l'orchestrator non ripiega). */
    private fun fallbackToClassic(why: String) {
        Log.i(tag, "live dal tap non disponibile ($why): turno classico")
        liveSession = false
        scope.launch { startTurn() }
    }

    /** announce{call_id,text,urgent} dall'orchestrator (19.12 D6): all'hub, lo legge Gemini. */
    private fun relayAnnounce(msg: JSONObject) {
        val h = hub
        val cid = msg.optString("call_id")
        if (h == null || (cid.isNotEmpty() && cid != h.callId)) {
            Log.w(tag, "announce per la call ${cid.ifEmpty { "-" }} scartato (nessuna sessione hub)")
            return
        }
        msg.put("call_id", h.callId)
        if (h.sendControl(msg.toString())) Log.i(tag, "announce inoltrato all'hub (call ${h.callId})")
        else Log.w(tag, "announce non inoltrato: sessione hub non attiva")
    }

    private fun newReqId(): String = java.util.UUID.randomUUID().toString().replace("-", "").take(16)
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

    companion object {
        private const val FW = "android-mobile-0.1.0"
        private const val MAX_HISTORY = 10
        private const val TRIGGER_LISTEN_DELAY_MS = 800L  // attesa fine coda TTS prima del multiturn
        private const val ERROR_FLASH_MS = 3600L          // durata visualizzazione messaggio d'errore
        private const val FALLBACK_WAIT_MS = 5_000L       // attesa del ripiego dell'orchestrator dopo hub_rejected/unreachable
        /**
         * Tap con live: l'offer all'hub aspetta al più fino a qui (dal tap) che nel buffer compaia
         * un comando, perché `offer_extra.command` decide l'apertura ("Eccomi" o sessione rapida)
         * e dopo un tap il parlato inizia con 0,3-0,8 s di ritardo. Con il parlato già nel buffer
         * l'offer parte subito; senza comando l'"Eccomi" arriva al più ~1 s dopo.
         */
        const val COMMAND_WINDOW_MS = 1_200L

        /** HubClient di produzione: accept tardivo (7 s) per le sessioni dal tap, 8 s come oggi per le altre. */
        fun defaultHubClient(
            spec: HubLiveStart.Start,
            offer: () -> String,
            listener: HubClient.Listener,
            connectTimeoutMs: Int = 5_000,
            acceptTimeoutMs: Int = if (spec.fromRequest) LiveWake.ACCEPT_TIMEOUT_MS else 8_000,
            endGraceMs: Long = 1_000,
        ): HubClient = HubClient(spec.host, spec.port, spec.callId, offer, listener,
            connectTimeoutMs = connectTimeoutMs, acceptTimeoutMs = acceptTimeoutMs, endGraceMs = endGraceMs)

        /** Eventi del turn-loop orchestrator ignorati durante una sessione hub. */
        private val TURN_EVENTS = setOf(
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
