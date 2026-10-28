package com.jarvis.voice.wear

import android.content.Context
import android.util.Log
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.MessageClient
import com.google.android.gms.wearable.Wearable
import com.jarvis.voice.shared.HeadState
import com.jarvis.voice.shared.audio.LiveAudio
import com.jarvis.voice.shared.audio.LiveAudioRouting
import com.jarvis.voice.shared.audio.LivePlayout
import com.jarvis.voice.shared.audio.MicRecorder
import com.jarvis.voice.shared.audio.TtsPlayer
import com.jarvis.voice.shared.wear.DataLayer
import com.jarvis.voice.shared.wear.WatchStream
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.tasks.await
import java.io.InputStream
import java.io.OutputStream

/**
 * Link Data Layer verso il telefono-relay. Lo watch è stateless: apre un canale audio
 * bidirezionale, manda i tap via MessageClient e riceve gli aggiornamenti di HeadState.
 *
 *  tap → PATH_TAP → (phone) → state LISTENING → parte il mic (scrive PCM sul canale)
 *  TTS PCM ← canale → TtsPlayer
 *
 * Sessione live hub: PATH_LIVE start/stop dal telefono. In live il mic è VOICE_COMMUNICATION
 * con AEC LOCALE (cancella lo speaker del watch dal mic del watch) e resta aperto anche mentre
 * suona. Il parlato live arriva a frame nel canale ([WatchStream]) e lo suona [LivePlayout]:
 * buffer anti-jitter + silenzio continuo, formato (rate, stream media/comms) deciso dal
 * telefono, barge-in = frame CLEAR in ordine con l'audio. La rete resta il tunnel via
 * telefono. Fuori sessione tutto come prima (half-duplex guidato da HeadState).
 */
class PhoneLink(private val context: Context) {

    private val tag = "PhoneLink"
    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.IO +
            CoroutineExceptionHandler { _, e -> Log.w(tag, "link coroutine: ${e.message}") }
    )
    private val channelClient: ChannelClient = Wearable.getChannelClient(context)
    private val messageClient: MessageClient = Wearable.getMessageClient(context)
    private val channelMutex = Mutex()

    private val mic = MicRecorder()
    private val player = TtsPlayer()   // path di oggi (TTS dell'orchestrator), spento in live

    // Solo durante una sessione live hub. liveWanted = ultima richiesta del telefono (PATH_LIVE),
    // live = modalità davvero attiva; liveLock serializza entrata/uscita e i cambi di mic.
    private val liveLock = Any()
    @Volatile private var live = false
    @Volatile private var liveWanted = false
    @Volatile private var stopped = false
    @Volatile private var liveNode: String? = null   // telefono che ha chiesto la live
    private var liveMic: MicRecorder? = null
    private var routing: LiveAudioRouting? = null

    private var nodeId: String? = null
    private var channel: ChannelClient.Channel? = null
    private var micOut: OutputStream? = null
    private var readJob: Job? = null

    private val _state = MutableStateFlow(HeadState.IDLE)
    val state: StateFlow<HeadState> = _state.asStateFlow()

    private val _amplitude = MutableStateFlow(0f)
    /** Livello mic 0..1 (per la waveform), calcolato localmente sul watch durante l'ascolto. */
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    private val _live = MutableStateFlow(false)
    /** true in sessione live hub (vista a due corsie). */
    val liveState: StateFlow<Boolean> = _live.asStateFlow()

    private val _jarvisLevel = MutableStateFlow(0f)
    /** Livello 0..1 del parlato del modello suonato qui (corsia JARVIS), solo in live. */
    val jarvisLevel: StateFlow<Float> = _jarvisLevel.asStateFlow()

    /** Parlato della sessione live: thread audio suo, acceso solo in live (open/close). */
    private val livePlayout = LivePlayout(onLevel = { if (live) _jarvisLevel.value = it })

    private val stateListener = MessageClient.OnMessageReceivedListener { event ->
        when (event.path) {
            DataLayer.PATH_STATE -> {
                val s = runCatching { HeadState.valueOf(String(event.data)) }.getOrNull() ?: return@OnMessageReceivedListener
                Log.i(tag, "stato ← $s")
                onNewState(s)
            }
            DataLayer.PATH_LIVE -> {
                // Subito, sul thread del listener: da qui lo stato non riaccende il mic di oggi.
                liveWanted = String(event.data) == DataLayer.LIVE_START
                liveNode = event.sourceNodeId
                Log.i(tag, "live ← ${String(event.data)}")
                scope.launch { syncLive() }
            }
            // PATH_CLEAR non arriva più: sul canale a frame il clear è in banda.
        }
    }

    fun start() { messageClient.addListener(stateListener) }
    fun stop() {
        messageClient.removeListener(stateListener)
        // Sotto liveLock: un enterLive in corso finisce prima, uno successivo vede stopped.
        // Ripristina MODE_IN_COMMUNICATION anche se l'Activity muore in sessione.
        synchronized(liveLock) {
            stopped = true
            liveWanted = false
            exitLive(drainMs = 0)   // Activity che muore: niente drain a bloccare il main thread
        }
        mic.stop(); player.stop(); livePlayout.close(drainMs = 0); readJob?.cancel()
        channel?.let { channelClient.close(it) }
        scope.cancel()
    }

    /** Gesto unico: tap sulla testa. Il telefono decide (start / flush / barge-in). */
    fun tap() {
        scope.launch {
            ensureChannel()
            val node = nodeId ?: run { Log.w(tag, "tap ma nessun telefono connesso"); return@launch }
            Log.i(tag, "tap → node=$node")
            runCatching { messageClient.sendMessage(node, DataLayer.PATH_TAP, ByteArray(0)).await() }
                .onFailure { Log.w(tag, "tap send: ${it.message}") }
        }
    }

    private fun onNewState(s: HeadState) {
        _state.value = s
        synchronized(liveLock) { applyState(s) }
    }

    /** Mic e player del path di oggi secondo lo stato. Chiamare sotto liveLock. */
    private fun applyState(s: HeadState) {
        // In live (o in entrata) mic e player li gestisce la modalità live, non lo stato.
        if (live || liveWanted || stopped) return
        if (s != HeadState.LISTENING) _amplitude.value = 0f
        when (s) {
            HeadState.LISTENING -> startMic()
            HeadState.SPEAKING -> player.start()
            HeadState.THINKING, HeadState.IDLE -> mic.stop()
            else -> {}
        }
    }

    private fun startMic() {
        val out = micOut
        if (out == null) { Log.w(tag, "startMic: micOut è null (canale non pronto)"); return }
        val ok = mic.start { frame ->
            runCatching { out.write(frame); out.flush() }
            _amplitude.value = frameLevel(frame)
        }
        if (!ok) Log.w(tag, "mic.start fallito (permesso RECORD_AUDIO?)")
    }

    /** Porta la modalità live all'ultima richiesta del telefono (i messaggi possono arrivare a raffica). */
    private suspend fun syncLive() {
        if (liveWanted) runCatching { ensureChannel() }.onFailure { Log.w(tag, "live: canale: ${it.message}") }
        synchronized(liveLock) {
            if (stopped) return
            // Live mai entrato (start e stop arrivati insieme): exitLive non farebbe nulla,
            // lo stato saltato va riapplicato comunque
            if (liveWanted) enterLive()
            else if (live) exitLive()
            else { livePlayout.close(); applyState(_state.value) }   // via il parlato arrivato senza live
        }
    }

    /** Lo watch non regge la sessione live: il telefono la chiude con reason "error". */
    private fun abandonLive(why: String) {
        Log.w(tag, "live: $why, chiudo la sessione")
        liveWanted = false
        val node = liveNode ?: nodeId ?: return
        scope.launch {
            runCatching { messageClient.sendMessage(node, DataLayer.PATH_STOP, DataLayer.STOP_ERROR.toByteArray()).await() }
        }
    }

    /** Entra in live: mic comms + AEC locale continuo sul canale, playout live continuo. Sotto liveLock. */
    private fun enterLive() {
        if (live) return
        // Sessione sorda: meglio chiuderla subito che lasciarla aperta fino al timeout dell'hub.
        val out = micOut ?: run { abandonLive("canale audio non pronto"); return }
        mic.stop()
        live = true
        _live.value = true
        routing = LiveAudioRouting(context).also { it.enter() }   // prima del record (AEC)
        // Il track live parte subito (silenzio): l'uscita è già calda alla prima risposta.
        livePlayout.open()
        player.stop()
        val m = MicRecorder(live = true)
        val ok = m.start { frame ->
            runCatching { out.write(frame); out.flush() }
            _amplitude.value = frameLevel(frame, LiveAudio.LIVE_LEVEL_BOOST)
        }
        if (!ok) {
            abandonLive("mic non partito")
            exitLive()
            return
        }
        if (!m.echoCancelerActive) Log.w(tag, "live: AcousticEchoCanceler non disponibile sul watch")
        liveMic = m
    }

    /** Esce dalla live e torna al path di oggi. Idempotente. Sotto liveLock. */
    private fun exitLive(drainMs: Long = 600) {
        if (!live) return
        live = false
        liveMic?.stop(); liveMic = null
        livePlayout.close(drainMs)   // finisce di suonare la coda (se c'è) prima di fermare il track
        routing?.exit(); routing = null
        player.start()        // warm-up del path di oggi, come all'apertura del canale
        _live.value = false
        _amplitude.value = 0f
        _jarvisLevel.value = 0f
        // Gli stati arrivati durante la live sono stati saltati: riallinea mic e player a
        // quello corrente (es. il LISTENING della sessione di oggi dopo un ripiego).
        applyState(_state.value)
    }

    /** RMS del frame PCM int16 LE → 0..1 (con guadagno per una waveform viva). */
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
        return (Math.sqrt(sum / n) / 32768.0 * 9.0 * boost).coerceIn(0.0, 1.0).toFloat()
    }

    private suspend fun ensureChannel() {
        channelMutex.withLock {
            if (channel != null) return
            val node = pickNode() ?: run { Log.w(tag, "nessun telefono connesso"); return }
            nodeId = node
            val ch = channelClient.openChannel(node, DataLayer.PATH_AUDIO_FRAMED).await()
            channel = ch
            micOut = channelClient.getOutputStream(ch).await()
            val input: InputStream = channelClient.getInputStream(ch).await()
            player.start()   // warm-up: AudioTrack pronto PRIMA dei frame TTS (evita testa troncata)

            // Frame dal telefono: TTS di oggi → player; parlato live e clear → playout live.
            // Solo il TTS di oggi scrive bloccando qui: il live va in coda e lo suona il suo thread.
            readJob = scope.launch {
                val reader = WatchStream.Reader(input)
                try {
                    while (true) {
                        when (val f = reader.next() ?: return@launch) {
                            is WatchStream.Frame.Tts -> runCatching { player.write(f.pcm) }
                            is WatchStream.Frame.Live -> livePlayout.push(f.pcm, f.sampleRate, f.comms)
                            WatchStream.Frame.Clear -> livePlayout.clear()
                            is WatchStream.Frame.Unknown -> Log.w(tag, "frame sconosciuto ${f.type}")
                        }
                    }
                } catch (e: Exception) {
                    Log.w(tag, "TTS read loop terminato: ${e.message}")  // canale chiuso: normale
                }
            }
        }
    }

    private suspend fun pickNode(): String? {
        val nodes = Wearable.getNodeClient(context).connectedNodes.await()
        return nodes.firstOrNull { it.isNearby }?.id ?: nodes.firstOrNull()?.id
    }
}
