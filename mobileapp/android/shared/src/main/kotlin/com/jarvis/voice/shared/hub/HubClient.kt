package com.jarvis.voice.shared.hub

import com.jarvis.voice.shared.audio.Pcm
import com.jarvis.voice.shared.protocol.EndReason
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Client TCP verso il Voice Live Hub per UNA sessione live (una connessione = una sessione).
 *
 *   start() → connect → offer → accept → audio full-duplex (mic 16 kHz su, modello 24 kHz giù)
 *   hub "clear"   → [Listener.onClear] (barge-in: svuota il playback SUBITO)
 *   hub "hangup"  → [Listener.onEnded] "hub_hangup" (end_call o timeout lato hub)
 *   end()         → manda {"type":"ended"} e chiude (fine decisa dal device, nessun callback)
 *
 * Esiste solo tra inizio e fine della sessione live: nessun thread/socket fuori sessione.
 * I callback arrivano dal thread di lettura (onEnded anche da quello di scrittura).
 */
class HubClient(
    private val host: String,
    private val port: Int,
    val callId: String,
    private val offerJson: String,
    private val listener: Listener,
    private val connectTimeoutMs: Int = 5_000,
    private val acceptTimeoutMs: Int = 8_000,
    private val endGraceMs: Long = 1_000,
) {
    interface Listener {
        fun onAccepted()
        /** Parlato del modello, PCM 24 kHz mono. */
        fun onAudio(pcm: ShortArray)
        fun onClear()
        /** Fine NON chiesta da noi: reason = motivo del contratto (hub_hangup, hub_rejected, ...). */
        fun onEnded(reason: String)
    }

    enum class State { IDLE, CONNECTING, OFFERED, ACTIVE, CLOSED }

    @Volatile var state: State = State.IDLE
        private set

    private val closed = AtomicBoolean(false)
    private val sendQueue = LinkedBlockingQueue<ByteArray>(MAX_QUEUED)
    @Volatile private var socket: Socket? = null

    fun start() {
        if (state != State.IDLE) return
        state = State.CONNECTING
        thread(name = "jarvis-hub-rx", isDaemon = true) { runReader() }
    }

    /** Frame mic (PCM 16 kHz s16le). Scartato finché l'offer non è partito o a sessione chiusa. */
    fun sendAudio(pcm: ByteArray) {
        val s = state
        if (closed.get() || (s != State.OFFERED && s != State.ACTIVE)) return
        val frame = HubIpc.encodeAudio(callId, pcm)
        // Rete lenta: meglio perdere audio vecchio che bloccare il thread del mic.
        while (!sendQueue.offer(frame)) sendQueue.poll()
    }

    /** Fine decisa dal device (tap, fine orchestrator, errore locale): ended + chiusura. */
    fun end() {
        if (!closed.compareAndSet(false, true)) return
        val wasConnected = socket != null
        state = State.CLOSED
        if (wasConnected) {
            sendQueue.clear()
            sendQueue.offer(HubIpc.encodeJson(JSONObject().put("type", "ended").put("call_id", callId).toString()))
            sendQueue.offer(POISON)
            // Rete verso l'hub morta: il writer può restare bloccato in write per minuti.
            // Dopo un attimo di grazia per far uscire "ended", il socket si chiude comunque.
            thread(name = "jarvis-hub-close", isDaemon = true) {
                runCatching { Thread.sleep(endGraceMs) }
                runCatching { socket?.close() }
            }
        }
        // Se la connect è ancora in corso ci pensa runReader a chiudere il socket.
    }

    private fun runReader() {
        val s = Socket()
        try {
            s.tcpNoDelay = true
            s.keepAlive = true   // dopo l'accept niente timeout di lettura: almeno il keepalive TCP
            s.connect(InetSocketAddress(host, port), connectTimeoutMs)
            s.soTimeout = acceptTimeoutMs   // l'hub deve rispondere all'offer in tempi brevi
        } catch (e: IOException) {
            runCatching { s.close() }
            finish(REASON_UNREACHABLE)
            return
        }
        socket = s
        // end() durante la connect: chiudi senza offer (ricontrollo DOPO aver pubblicato il socket).
        if (closed.get()) { runCatching { s.close() }; return }

        val output = runCatching { BufferedOutputStream(s.getOutputStream()) }.getOrNull()
        val input = runCatching { DataInputStream(BufferedInputStream(s.getInputStream())) }.getOrNull()
        if (output == null || input == null) { finish(REASON_UNREACHABLE); return }

        sendQueue.offer(HubIpc.encodeJson(offerJson))   // primo frame: l'offer
        if (state == State.CONNECTING) state = State.OFFERED
        thread(name = "jarvis-hub-tx", isDaemon = true) { runWriter(output) }

        try {
            while (true) {
                val f = HubIpc.readFrame(input) ?: break
                when (f) {
                    is HubIpc.Frame.Json -> if (!handleJson(f.text, s)) return
                    is HubIpc.Frame.Audio ->
                        if (state == State.ACTIVE && f.callId == callId && f.pcm.size >= 2) {
                            listener.onAudio(Pcm.toShorts(f.pcm))
                        }
                }
            }
            // EOF senza hangup: prima dell'accept = l'hub ci ha rifiutati in silenzio
            finish(if (state == State.ACTIVE) REASON_ERROR else REASON_REJECTED)
        } catch (e: SocketTimeoutException) {
            finish(REASON_UNREACHABLE)
        } catch (e: Exception) {
            finish(if (state == State.ACTIVE) REASON_ERROR else REASON_UNREACHABLE)
        }
    }

    /** false = sessione chiusa, il reader si ferma. */
    private fun handleJson(text: String, s: Socket): Boolean {
        val msg = runCatching { JSONObject(text) }.getOrNull() ?: return true
        val cid = msg.optString("call_id", "")
        val ours = cid.isEmpty() || cid == callId
        when (msg.optString("type")) {
            "accept" -> if (cid == callId && state == State.OFFERED) {
                runCatching { s.soTimeout = 0 }
                state = State.ACTIVE
                listener.onAccepted()
            }
            // Qualsiasi "rejected" è un abort, anche senza call_id.
            "rejected" -> { finish(REASON_REJECTED); return false }
            "clear" -> if (ours && state == State.ACTIVE) listener.onClear()
            "hangup" -> if (ours) { finish(REASON_HANGUP); return false }
        }
        return true
    }

    private fun runWriter(out: OutputStream) {
        try {
            while (true) {
                val f = sendQueue.take()
                if (f === POISON) break
                out.write(f)
                if (sendQueue.isEmpty()) out.flush()
            }
            out.flush()
        } catch (e: InterruptedException) {
            // chiusura
        } catch (e: IOException) {
            finish(if (state == State.ACTIVE) REASON_ERROR else REASON_UNREACHABLE)
        } finally {
            if (closed.get()) runCatching { socket?.close() }
        }
    }

    /** Fine non chiesta da noi: chiude tutto e notifica una sola volta. */
    private fun finish(reason: String) {
        if (!closed.compareAndSet(false, true)) return
        state = State.CLOSED
        sendQueue.clear()
        sendQueue.offer(POISON)
        runCatching { socket?.close() }
        listener.onEnded(reason)
    }

    companion object {
        const val REASON_HANGUP = EndReason.HUB_HANGUP
        const val REASON_REJECTED = EndReason.HUB_REJECTED
        const val REASON_UNREACHABLE = EndReason.HUB_UNREACHABLE
        const val REASON_ERROR = EndReason.ERROR

        private const val MAX_QUEUED = 100   // ~2 s di frame da 20 ms
        private val POISON = ByteArray(0)
    }
}
