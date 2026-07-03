package com.jarvis.voice.mobile.wear

import android.util.Log
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.Wearable
import com.google.android.gms.wearable.WearableListenerService
import com.jarvis.voice.mobile.core.JarvisRuntime
import com.jarvis.voice.shared.protocol.AudioFormat
import com.jarvis.voice.shared.wear.DataLayer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * RELAY lato telefono: fa da ponte tra lo watch (Wearable Data Layer) e il JarvisController
 * (che possiede la WS verso l'orchestrator + Tailscale + token).
 *
 *  - MessageClient PATH_TAP  → controller.onTap()  (+ passa in relayMode)
 *  - MessageClient PATH_STOP → controller.stopEverything()
 *  - ChannelClient PATH_AUDIO (bidirezionale):
 *       watch→phone  = frame PCM mic  → controller.feedMicFrame()
 *       phone→watch  = PCM TTS        ← controller.remoteTtsWriter
 *  - controller.state → MessageClient PATH_STATE verso lo watch
 */
class WatchBridgeService : WearableListenerService() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val channelClient: ChannelClient by lazy { Wearable.getChannelClient(this) }
    private var watchNodeId: String? = null
    private var readJob: Job? = null
    private var ttsOut: OutputStream? = null
    private var stateJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        JarvisRuntime.init(this)
        observeStateForWatch()
    }

    // ── Controllo ────────────────────────────────────────────────────────────
    override fun onMessageReceived(event: MessageEvent) {
        watchNodeId = event.sourceNodeId
        when (event.path) {
            DataLayer.PATH_TAP -> {
                JarvisRuntime.controller.relayMode = true
                JarvisRuntime.controller.onTap()
            }
            DataLayer.PATH_STOP -> JarvisRuntime.controller.stopEverything()
        }
    }

    // ── Audio (canale bidirezionale) ──────────────────────────────────────────
    override fun onChannelOpened(channel: ChannelClient.Channel) {
        if (channel.path != DataLayer.PATH_AUDIO) return
        watchNodeId = channel.nodeId
        val c = JarvisRuntime.controller
        c.relayMode = true

        scope.launch {
            runCatching {
                val input: InputStream = channelClient.getInputStream(channel).await()
                val output: OutputStream = channelClient.getOutputStream(channel).await()
                ttsOut = output

                // TTS decodificata dal controller → scrivi PCM (LE) verso lo watch
                c.remoteTtsWriter = writer@{ pcm ->
                    val out = ttsOut ?: return@writer
                    val bb = ByteBuffer.allocate(pcm.size * 2).order(ByteOrder.LITTLE_ENDIAN)
                    for (s in pcm) bb.putShort(s)
                    runCatching { out.write(bb.array()); out.flush() }
                }

                // Mic dallo watch → controller.feedMicFrame() a frame da 640 byte
                readJob = launch {
                    val buf = ByteArray(AudioFormat.FRAME_BYTES)
                    while (true) {
                        var off = 0
                        while (off < buf.size) {
                            val n = input.read(buf, off, buf.size - off)
                            if (n < 0) return@launch
                            off += n
                        }
                        c.feedMicFrame(buf.copyOf())
                    }
                }
            }.onFailure { Log.w(TAG, "channel setup: ${it.message}") }
        }
    }

    override fun onChannelClosed(channel: ChannelClient.Channel, closeReason: Int, appSpecificErrorCode: Int) {
        if (channel.path != DataLayer.PATH_AUDIO) return
        readJob?.cancel(); readJob = null
        ttsOut = null
        val c = JarvisRuntime.controller
        c.remoteTtsWriter = null
        c.relayMode = false
    }

    private fun observeStateForWatch() {
        stateJob?.cancel()
        stateJob = scope.launch {
            val messageClient = Wearable.getMessageClient(this@WatchBridgeService)
            JarvisRuntime.controller.state.collect { s ->
                val node = watchNodeId ?: return@collect
                runCatching {
                    messageClient.sendMessage(node, DataLayer.PATH_STATE, s.name.toByteArray()).await()
                }
            }
        }
    }

    override fun onDestroy() {
        readJob?.cancel()
        stateJob?.cancel()
        super.onDestroy()
    }

    private companion object { const val TAG = "WatchBridge" }
}
