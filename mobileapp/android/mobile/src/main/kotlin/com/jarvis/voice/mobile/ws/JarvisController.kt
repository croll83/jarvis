package com.jarvis.voice.mobile.ws

import android.util.Log
import com.jarvis.voice.mobile.audio.OpusDecoderWrapper
import com.jarvis.voice.mobile.config.JarvisConfig
import com.jarvis.voice.shared.HeadState
import com.jarvis.voice.shared.audio.TtsPlayer
import com.jarvis.voice.shared.protocol.Codec
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
 */
class JarvisController(
    private val config: JarvisConfig,
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

    private var ws: WebSocket? = null
    @Volatile private var streaming = false
    @Volatile private var liveSession = false
    private val lastActivity = AtomicLong(System.currentTimeMillis())

    init {
        scope.launch { ttlLoop() }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Gesto unico: TAP sulla testa
    // ─────────────────────────────────────────────────────────────────────────
    fun onTap() {
        touch()
        scope.launch {
            when (_state.value) {
                HeadState.IDLE, HeadState.ERROR -> startTurn()  // tap su errore = ritenta
                HeadState.LISTENING -> flush()          // secondo tap
                HeadState.THINKING, HeadState.SPEAKING -> bargeStop()
                HeadState.CONNECTING -> { /* ignora, in transito */ }
            }
        }
    }

    fun stopEverything() {
        scope.launch { bargeStop() }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Frame mic in ingresso (dal mic locale o dallo watch)
    // ─────────────────────────────────────────────────────────────────────────
    fun feedMicFrame(frame: ByteArray) {
        if (streaming) {
            ws?.send(frame.toByteString())
            _amplitude.value = frameLevel(frame)
        }
    }

    /** RMS del frame PCM int16 LE → 0..1 (con curva per renderlo vivo visivamente). */
    private fun frameLevel(frame: ByteArray): Float {
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
        return (rms * 9.0).coerceIn(0.0, 1.0).toFloat()  // sensibilità waveform
    }

    // ─────────────────────────────────────────────────────────────────────────
    // State machine
    // ─────────────────────────────────────────────────────────────────────────
    private suspend fun startTurn() {
        ensureConnected()
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
                flashError()
                return
            }
            setState(HeadState.CONNECTING)
            val base = config.url
                .replaceFirst("ws://", "http://")
                .replaceFirst("wss://", "https://")
            var url = "$base/ws/audio?device_id=${config.deviceId}"
            if (config.token.isNotBlank()) url += "&token=${config.token}"

            val req = Request.Builder().url(url).build()
            ws = http.newWebSocket(req, Listener())
            sendJson(JSONObject().put("type", MsgType.HELLO).put("fw", FW))
        }
    }

    private fun closeConnection(reason: String) {
        streaming = false
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
        when (msg.optString("type")) {
            MsgType.WELCOME -> {}
            MsgType.CONFIG_UPDATE -> {
                val spk = msg.optString("speaker_type", "internal")
                if (spk != "internal") Log.w(tag, "speaker_type=$spk — imposta use_internal_speaker=true!")
            }
            MsgType.READY -> {
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
            MsgType.TTS_DONE -> setState(HeadState.IDLE)
            MsgType.TRIGGER_LISTEN -> scope.launch { startTurn() }  // multiturn / prossimo turno live
            MsgType.LIVE_SESSION_START -> { liveSession = true; setState(HeadState.LISTENING) }
            MsgType.LIVE_SESSION_END -> { liveSession = false; setState(HeadState.IDLE) }
            MsgType.PING -> sendJson(JSONObject().put("type", MsgType.PONG))
            MsgType.ERROR -> Log.w(tag, "server error: ${msg.opt("msg") ?: msg.opt("message")}")
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
        val pcm = opus.decode(bytes.toByteArray())
        if (pcm.isEmpty()) return
        val remote = remoteTtsWriter
        if (remote != null) remote(pcm) else localPlayer.write(pcm)
    }

    private inner class Listener : WebSocketListener() {
        override fun onMessage(webSocket: WebSocket, text: String) = handleText(text)
        override fun onMessage(webSocket: WebSocket, bytes: ByteString) = handleBinary(bytes)
        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.w(tag, "WS failure: ${t.message}")
            if (ws === webSocket) { ws = null; streaming = false; flashError() }
        }
        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (ws === webSocket) { ws = null; streaming = false }
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Utility
    // ─────────────────────────────────────────────────────────────────────────
    private fun sendJson(obj: JSONObject) { ws?.send(obj.toString()) }
    private fun setState(s: HeadState) {
        _state.value = s
        if (s != HeadState.LISTENING) _amplitude.value = 0f
    }
    private fun touch() { lastActivity.set(System.currentTimeMillis()) }

    /** Mostra ERROR per qualche secondo, poi torna IDLE se nessun'altra transizione è avvenuta. */
    private fun flashError() {
        setState(HeadState.ERROR)
        scope.launch {
            delay(2600)
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
    }
}

/** Una coppia di turno conversazione per lo storico. */
data class DialogTurn(val user: String? = null, val jarvis: String? = null)
