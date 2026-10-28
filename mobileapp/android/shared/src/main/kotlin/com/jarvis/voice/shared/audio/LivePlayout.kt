package com.jarvis.voice.shared.audio

import android.media.AudioAttributes
import android.media.AudioFormat as AndroidAudioFormat
import android.media.AudioTrack
import android.os.Process
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Playout continuo del parlato live sullo watch: rete e riproduzione separate.
 *
 * Il thread di lettura del tunnel chiama [push] / [clear]; un thread audio dedicato scrive
 * nell'AudioTrack un frame da 20 ms dopo l'altro, prendendolo dal [JitterBuffer] o, se il
 * modello tace, scrivendo silenzio. L'AudioTrack resta acceso per tutta la sessione e non
 * va mai in underrun (prima restava a secco tra una risposta e l'altra e la testa della
 * successiva si perdeva nel riavvio dell'uscita). La write bloccante fa da orologio.
 *
 * Formato (rate + stream comms/media) = quello dell'ultimo frame ricevuto: se cambia, il
 * thread ricrea il track. Prima del primo frame si usa l'ultimo formato visto.
 *
 * Il thread (e il track) esiste solo tra [open] e [close]: fuori sessione live non c'è
 * niente di acceso. I frame arrivati prima di [open] restano in coda (vengono suonati
 * all'apertura se recenti: copre il parlato che arriva prima del PATH_LIVE).
 */
class LivePlayout(private val onLevel: (Float) -> Unit = {}) {

    private data class Format(val rate: Int, val comms: Boolean)

    private val tag = "LivePlayout"
    private val lock = Any()
    @Volatile private var format = Format(LiveAudio.WATCH_DEFAULT_RATE, comms = false)
    private var jitter = JitterBuffer(format.rate)
    private var lastPushNanos = 0L
    private val flushPending = AtomicBoolean(false)
    private val running = AtomicBoolean(false)
    @Volatile private var drainNanos = 0L    // > 0: close() in corso, finisci la coda entro questo istante
    private var worker: Thread? = null
    // Il thread vivo è solo quello dell'ultima open(): uno vecchio rimasto bloccato in una
    // write oltre il join di close() non deve ripartire quando open() rimette running = true.
    @Volatile private var epoch = 0

    /** Parlato live dal tunnel (thread di lettura). */
    fun push(pcm: ShortArray, sampleRate: Int, comms: Boolean) {
        synchronized(lock) {
            val f = Format(sampleRate, comms)
            if (f != format) {
                // Nuovo formato = nuova sessione o impostazione cambiata: la coda vecchia non vale più
                Log.i(tag, "formato live: ${sampleRate} Hz, ${if (comms) "comms" else "media"}")
                format = f
                jitter = JitterBuffer(sampleRate)
            }
            lastPushNanos = System.nanoTime()
            jitter.push(pcm, lastPushNanos)
        }
    }

    /** Barge-in in banda: butta la coda e il parlato già nel track. */
    fun clear() {
        synchronized(lock) {
            jitter.clear()
            flushPending.set(true)
        }
    }

    fun open() {
        if (!running.compareAndSet(false, true)) return
        synchronized(lock) {
            // Coda rimasta da una sessione precedente (frame tardivi dopo la chiusura): via.
            if (System.nanoTime() - lastPushNanos > STALE_NANOS) jitter.clear()
        }
        drainNanos = 0L
        flushPending.set(false)
        val my = ++epoch
        worker = thread(name = "jarvis-live-playout", isDaemon = true) { loop(my) }
    }

    /**
     * Fine sessione: lascia suonare il parlato in coda (al massimo [drainMs]), poi ferma il
     * track dopo che ha suonato quanto aveva in buffer. Idempotente.
     */
    fun close(drainMs: Long = 600) {
        if (!running.get()) { synchronized(lock) { jitter.clear() }; return }
        drainNanos = System.nanoTime() + drainMs * 1_000_000L
        val w = worker
        w?.join(drainMs + 500)
        running.set(false)
        epoch++
        w?.interrupt()
        w?.join(300)
        worker = null
        synchronized(lock) { jitter.clear() }
        onLevel(0f)
    }

    private fun loop(my: Int) {
        runCatching { Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO) }
        var track: AudioTrack? = null
        var trackFormat: Format? = null
        var frame = ShortArray(0)
        var lastHadAudio = false
        try {
            while (running.get() && epoch == my) {
                val f = format
                if (f != trackFormat) {
                    track?.let { stopTrack(it, drain = false) }
                    track = buildTrack(f) ?: run { Log.w(tag, "AudioTrack live non creato ($f)"); return }
                    trackFormat = f
                    frame = ShortArray(f.rate / 50)
                    Log.i(tag, "track live: ${f.rate} Hz ${if (f.comms) "USAGE_VOICE_COMMUNICATION" else "USAGE_MEDIA"}, " +
                        "buffer ${track.bufferSizeInFrames} frame")
                }
                val t = track ?: break
                val n: Int
                val flush: Boolean
                synchronized(lock) {
                    // Clear e lettura sotto lo stesso lock: dopo il flush escono solo frame nuovi
                    flush = flushPending.getAndSet(false)
                    n = jitter.read(frame)
                    // Fine sessione: coda vuota E tunnel zitto da un po' (il PATH_LIVE stop viaggia
                    // su MessageClient e può superare gli ultimi frame del canale), o scadenza.
                    val now = System.nanoTime()
                    if (drainNanos > 0 && (jitter.isEmpty && n == 0 && now - lastPushNanos > DRAIN_QUIET_NANOS ||
                            now > drainNanos)) {
                        running.set(false)
                    }
                }
                if (flush) runCatching { t.pause(); t.flush(); t.play() }
                if (!running.get() && n == 0) break
                val w = t.write(frame, 0, frame.size, AudioTrack.WRITE_BLOCKING)
                if (w < 0) {
                    // Track morto (DEAD_OBJECT dopo un cambio di uscita, ecc.): la write torna
                    // subito e il loop girerebbe a vuoto. Si ricrea il track.
                    Log.w(tag, "write live: errore $w, ricreo il track")
                    stopTrack(t, drain = false)
                    track = null; trackFormat = null
                    Thread.sleep(50)
                    continue
                }
                lastHadAudio = n > 0
                onLevel(if (n > 0) Pcm.level(frame) else 0f)
            }
        } catch (e: Exception) {
            Log.w(tag, "playout live: ${e.message}")
        } finally {
            // Chiusura con parlato appena scritto: il track lo suona tutto prima dello stop.
            // Se nel track c'è solo silenzio si ferma subito.
            track?.let { stopTrack(it, drain = drainNanos > 0 && lastHadAudio) }
        }
    }

    private fun buildTrack(f: Format): AudioTrack? = runCatching {
        val minBuf = AudioTrack.getMinBufferSize(
            f.rate, AndroidAudioFormat.CHANNEL_OUT_MONO, AndroidAudioFormat.ENCODING_PCM_16BIT,
        )
        val usage = if (f.comms) AudioAttributes.USAGE_VOICE_COMMUNICATION else AudioAttributes.USAGE_MEDIA
        AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(usage)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AndroidAudioFormat.Builder()
                    .setSampleRate(f.rate)
                    .setChannelMask(AndroidAudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AndroidAudioFormat.ENCODING_PCM_16BIT)
                    .build()
            )
            // Buffer corto: è sempre pieno (parlato o silenzio), quindi è latenza pura
            .setBufferSizeInBytes(maxOf(minBuf, f.rate / 1000 * TRACK_BUFFER_MS * 2))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
            .also { it.play() }
    }.getOrNull()

    private fun stopTrack(t: AudioTrack, drain: Boolean) {
        runCatching {
            if (drain) {
                // MODE_STREAM: stop() suona fino all'ultimo campione scritto; release() dopo
                t.stop()
                val ms = t.bufferSizeInFrames * 1000L / t.sampleRate
                Thread.sleep(ms + 40)
            } else {
                t.pause(); t.flush(); t.stop()
            }
        }
        runCatching { t.release() }
    }

    private companion object {
        const val TRACK_BUFFER_MS = 80
        const val STALE_NANOS = 3_000_000_000L
        const val DRAIN_QUIET_NANOS = 250_000_000L
    }
}
