package com.jarvis.voice.mobile.wear

import android.content.Context
import android.util.Log
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.Wearable
import com.jarvis.voice.mobile.core.JarvisRuntime
import com.jarvis.voice.shared.protocol.EndReason
import com.jarvis.voice.shared.protocol.AudioFormat
import com.jarvis.voice.shared.wear.DataLayer
import com.jarvis.voice.shared.wear.WatchStream
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Stato STABILE del relay watch, in un singleton a livello di processo. Il
 * [WatchBridgeService] (WearableListenerService) viene ricreato di continuo dal sistema
 * (onBindingDied / restart): se lo stato del relay vive nell'istanza del service, a ogni
 * rebind si perde il nodo watch + il canale → lo stato (es. SPEAKING) e i frame TTS non
 * arrivano più al watch. Qui invece nodo/canale/collector vivono nel singleton e
 * sopravvivono ai rebind. Il service delega solo gli eventi.
 */
object WatchRelay {

    private const val TAG = "WatchRelay"
    private const val TTS_FRAME_MAX_SAMPLES = WatchStream.MAX_PAYLOAD / 2
    private lateinit var appCtx: Context
    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.IO +
            CoroutineExceptionHandler { _, e -> Log.w(TAG, "relay coroutine: ${e.message}") }
    )

    @Volatile private var nodeId: String? = null
    @Volatile private var tunnel: Tunnel? = null
    private var readJob: Job? = null
    private var stateJob: Job? = null

    /**
     * Verso phone→watch del canale audio. [framed] = watch nuovo ([DataLayer.PATH_AUDIO_FRAMED]):
     * ogni scrittura è un frame [WatchStream]; altrimenti PCM grezzo come prima. Le scritture
     * sono serializzate: TTS, parlato live e clear non si mescolano a metà frame.
     */
    private class Tunnel(private val out: OutputStream, val framed: Boolean) {
        @Synchronized fun write(bytes: ByteArray) {
            runCatching { out.write(bytes); out.flush() }
        }
    }

    private fun rawPcm(pcm: ShortArray): ByteArray {
        val bb = ByteBuffer.allocate(pcm.size * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (s in pcm) bb.putShort(s)
        return bb.array()
    }

    private val liveTunnel = object : WatchLiveAudio.Tunnel {
        override val framed: Boolean get() = tunnel?.framed ?: true
        override fun live(pcm: ShortArray, sampleRate: Int, comms: Boolean) {
            val t = tunnel ?: return
            if (t.framed) t.write(WatchStream.live(pcm, sampleRate, comms))
            // Watch legacy: suona solo 16 kHz grezzi (rate deciso con il tunnel non ancora aperto)
            else if (sampleRate == AudioFormat.SAMPLE_RATE) t.write(rawPcm(pcm))
        }
        override fun clear(): Boolean {
            val t = tunnel ?: return false
            if (!t.framed) return false
            t.write(WatchStream.clear())
            return true
        }
    }

    /** Sessione live sul Voice Live Hub per il watch: tunnel continuo + PATH_LIVE, clear in banda. */
    private val liveAudio = WatchLiveAudio(
        send = ::sendToWatch,
        tunnel = liveTunnel,
        fullRate = { JarvisRuntime.config.watchLive24k },
        mediaUsage = { JarvisRuntime.config.watchLiveMediaUsage },
    )

    fun init(context: Context) {
        if (::appCtx.isInitialized) { startStateRelay(); return }
        appCtx = context.applicationContext
        JarvisRuntime.relayController.liveAudio = liveAudio
        startStateRelay()
    }

    /**
     * Messaggi della modalità live verso il watch (PATH_LIVE, PATH_CLEAR) in UNA coda servita
     * da un solo invio alla volta: l'ordine resta quello di emissione (live stop non può
     * sorpassare live start). Lo stato resta sul suo collector di oggi: il watch riallinea
     * mic e player allo stato corrente quando esce dalla live, quindi non serve ordinarli.
     */
    private class Outgoing(val node: String, val path: String, val payload: ByteArray)
    private val outbox = Channel<Outgoing>(Channel.UNLIMITED)
    private var outboxJob: Job? = null

    /** Messaggio corto verso il watch (asincrono, ordinato). false = nessun watch noto. */
    private fun sendToWatch(path: String, payload: String): Boolean {
        val node = nodeId ?: return false
        return outbox.trySend(Outgoing(node, path, payload.toByteArray())).isSuccess
    }

    fun onTap(sourceNodeId: String) {
        nodeId = sourceNodeId
        JarvisRuntime.relayController.relayMode = true
        JarvisRuntime.relayController.onTap()
    }

    /** PATH_STOP dal watch; payload [DataLayer.STOP_ERROR] = il watch non regge la sessione live. */
    fun onStop(payload: String = "") {
        val c = JarvisRuntime.relayController
        if (payload == DataLayer.STOP_ERROR) c.abortLiveSession(EndReason.ERROR) else c.stopEverything()
    }

    fun onChannelOpened(channel: ChannelClient.Channel) {
        nodeId = channel.nodeId
        val cc = Wearable.getChannelClient(appCtx)
        val c = JarvisRuntime.relayController
        scope.launch {
            runCatching {
                val input = cc.getInputStream(channel).await()
                val output = cc.getOutputStream(channel).await()
                val framed = channel.path == DataLayer.PATH_AUDIO_FRAMED
                tunnel = Tunnel(output, framed)
                Log.i(TAG, "canale audio aperto (node=${channel.nodeId}, ${if (framed) "a frame" else "legacy PCM"})")

                // TTS decodificata dal relayController → PCM (LE) verso lo watch
                c.remoteTtsWriter = writer@{ pcm ->
                    val t = tunnel ?: return@writer
                    if (!t.framed) { t.write(rawPcm(pcm)); return@writer }
                    // Un frame porta al massimo MAX_PAYLOAD byte: chunk lunghi spezzati
                    var off = 0
                    while (off < pcm.size) {
                        val n = minOf(TTS_FRAME_MAX_SAMPLES, pcm.size - off)
                        t.write(WatchStream.tts(if (off == 0 && n == pcm.size) pcm else pcm.copyOfRange(off, off + n)))
                        off += n
                    }
                }

                // Mic dallo watch → relayController.feedMicFrame()
                readJob?.cancel()
                readJob = launch {
                    val buf = ByteArray(AudioFormat.FRAME_BYTES)
                    try {
                        while (true) {
                            var off = 0
                            while (off < buf.size) {
                                val n = input.read(buf, off, buf.size - off)
                                if (n < 0) return@launch
                                off += n
                            }
                            c.feedMicFrame(buf.copyOf())
                        }
                    } catch (e: Exception) {
                        Log.w(TAG, "mic read loop terminato: ${e.message}")
                    }
                }
            }.onFailure { Log.w(TAG, "channel setup: ${it.message}") }
        }
    }

    fun onChannelClosed(channel: ChannelClient.Channel, closeReason: Int) {
        readJob?.cancel(); readJob = null
        tunnel = null
        JarvisRuntime.relayController.remoteTtsWriter = null
        // nodeId lo teniamo: serve al collector di stato tra un turno e l'altro.
        // Sessione live del watch senza tunnel = sorda e muta: si chiude e si avvisa
        // l'orchestrator (chiusura dal watch = l'utente ha chiuso l'app, altrimenti errore).
        val reason = if (closeReason == ChannelClient.ChannelCallback.CLOSE_REASON_REMOTE_CLOSE) EndReason.BUTTON
                     else EndReason.ERROR
        JarvisRuntime.relayController.abortLiveSession(reason)
    }

    private fun startStateRelay() {
        if (outboxJob == null) {
            outboxJob = scope.launch {
                val mc = Wearable.getMessageClient(appCtx)
                for (m in outbox) {
                    runCatching { mc.sendMessage(m.node, m.path, m.payload).await() }
                        .onFailure { Log.w(TAG, "invio ${m.path} fallito: ${it.message}") }
                }
            }
        }
        if (stateJob != null) return
        stateJob = scope.launch {
            val mc = Wearable.getMessageClient(appCtx)
            JarvisRuntime.relayController.state.collect { s ->
                val node = nodeId ?: return@collect
                Log.i(TAG, "→ watch stato=$s (node=$node)")
                runCatching { mc.sendMessage(node, DataLayer.PATH_STATE, s.name.toByteArray()).await() }
                    .onFailure { Log.w(TAG, "invio stato fallito: ${it.message}") }
            }
        }
    }
}
