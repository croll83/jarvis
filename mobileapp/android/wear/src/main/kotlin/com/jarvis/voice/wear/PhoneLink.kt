package com.jarvis.voice.wear

import android.content.Context
import android.util.Log
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.MessageClient
import com.google.android.gms.wearable.Wearable
import com.jarvis.voice.shared.HeadState
import com.jarvis.voice.shared.audio.MicRecorder
import com.jarvis.voice.shared.audio.TtsPlayer
import com.jarvis.voice.shared.protocol.AudioFormat
import com.jarvis.voice.shared.wear.DataLayer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
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
 */
class PhoneLink(private val context: Context) {

    private val tag = "PhoneLink"
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val channelClient: ChannelClient = Wearable.getChannelClient(context)
    private val messageClient: MessageClient = Wearable.getMessageClient(context)
    private val channelMutex = Mutex()

    private val mic = MicRecorder()
    private val player = TtsPlayer()

    private var nodeId: String? = null
    private var channel: ChannelClient.Channel? = null
    private var micOut: OutputStream? = null
    private var readJob: Job? = null

    private val _state = MutableStateFlow(HeadState.IDLE)
    val state: StateFlow<HeadState> = _state.asStateFlow()

    private val _amplitude = MutableStateFlow(0f)
    /** Livello mic 0..1 (per la waveform), calcolato localmente sul watch durante l'ascolto. */
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    private val stateListener = MessageClient.OnMessageReceivedListener { event ->
        if (event.path == DataLayer.PATH_STATE) {
            val s = runCatching { HeadState.valueOf(String(event.data)) }.getOrNull() ?: return@OnMessageReceivedListener
            Log.i(tag, "stato ← $s")
            onNewState(s)
        }
    }

    fun start() { messageClient.addListener(stateListener) }
    fun stop() {
        messageClient.removeListener(stateListener)
        mic.stop(); player.stop(); readJob?.cancel()
        channel?.let { channelClient.close(it) }
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
        if (s != HeadState.LISTENING) _amplitude.value = 0f
        when (s) {
            HeadState.LISTENING -> startMic()
            HeadState.SPEAKING -> player.start()
            HeadState.THINKING, HeadState.IDLE -> mic.stop()
            else -> {}
        }
    }

    private fun startMic() {
        val out = micOut ?: return
        mic.start { frame ->
            runCatching { out.write(frame); out.flush() }
            _amplitude.value = frameLevel(frame)
        }
    }

    /** RMS del frame PCM int16 LE → 0..1 (con guadagno per una waveform viva). */
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
        return (Math.sqrt(sum / n) / 32768.0 * 9.0).coerceIn(0.0, 1.0).toFloat()
    }

    private suspend fun ensureChannel() {
        channelMutex.withLock {
            if (channel != null) return
            val node = pickNode() ?: run { Log.w(tag, "nessun telefono connesso"); return }
            nodeId = node
            val ch = channelClient.openChannel(node, DataLayer.PATH_AUDIO).await()
            channel = ch
            micOut = channelClient.getOutputStream(ch).await()
            val input: InputStream = channelClient.getInputStream(ch).await()

            // TTS PCM dal telefono → player
            readJob = scope.launch {
                val buf = ByteArray(AudioFormat.FRAME_BYTES)
                val shorts = ShortArray(AudioFormat.FRAME_SAMPLES)
                while (true) {
                    var off = 0
                    while (off < buf.size) {
                        val n = input.read(buf, off, buf.size - off)
                        if (n < 0) return@launch
                        off += n
                    }
                    var i = 0
                    while (i < AudioFormat.FRAME_SAMPLES) {
                        val lo = buf[i * 2].toInt() and 0xFF
                        val hi = buf[i * 2 + 1].toInt()
                        shorts[i] = ((hi shl 8) or lo).toShort()
                        i++
                    }
                    player.write(shorts)
                }
            }
        }
    }

    private suspend fun pickNode(): String? {
        val nodes = Wearable.getNodeClient(context).connectedNodes.await()
        return nodes.firstOrNull { it.isNearby }?.id ?: nodes.firstOrNull()?.id
    }
}
