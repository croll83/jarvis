package com.jarvis.voice.mobile.wear

import android.content.Context
import android.util.Log
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.Wearable
import com.jarvis.voice.mobile.core.JarvisRuntime
import com.jarvis.voice.shared.protocol.AudioFormat
import com.jarvis.voice.shared.wear.DataLayer
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
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
    private lateinit var appCtx: Context
    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.IO +
            CoroutineExceptionHandler { _, e -> Log.w(TAG, "relay coroutine: ${e.message}") }
    )

    @Volatile private var nodeId: String? = null
    @Volatile private var ttsOut: OutputStream? = null
    private var readJob: Job? = null
    private var stateJob: Job? = null

    fun init(context: Context) {
        if (::appCtx.isInitialized) { startStateRelay(); return }
        appCtx = context.applicationContext
        startStateRelay()
    }

    fun onTap(sourceNodeId: String) {
        nodeId = sourceNodeId
        JarvisRuntime.relayController.relayMode = true
        JarvisRuntime.relayController.onTap()
    }

    fun onStop() = JarvisRuntime.relayController.stopEverything()

    fun onChannelOpened(channel: ChannelClient.Channel) {
        nodeId = channel.nodeId
        val cc = Wearable.getChannelClient(appCtx)
        val c = JarvisRuntime.relayController
        scope.launch {
            runCatching {
                val input = cc.getInputStream(channel).await()
                val output = cc.getOutputStream(channel).await()
                ttsOut = output
                Log.i(TAG, "canale audio aperto (node=${channel.nodeId})")

                // TTS decodificata dal relayController → PCM (LE) verso lo watch
                c.remoteTtsWriter = writer@{ pcm ->
                    val out = ttsOut ?: return@writer
                    val bb = ByteBuffer.allocate(pcm.size * 2).order(ByteOrder.LITTLE_ENDIAN)
                    for (s in pcm) bb.putShort(s)
                    runCatching { out.write(bb.array()); out.flush() }
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

    fun onChannelClosed(channel: ChannelClient.Channel) {
        readJob?.cancel(); readJob = null
        ttsOut = null
        JarvisRuntime.relayController.remoteTtsWriter = null
        // nodeId lo teniamo: serve al collector di stato tra un turno e l'altro.
    }

    private fun startStateRelay() {
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
