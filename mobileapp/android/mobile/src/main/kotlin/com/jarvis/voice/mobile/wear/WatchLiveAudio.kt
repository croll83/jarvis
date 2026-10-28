package com.jarvis.voice.mobile.wear

import android.util.Log
import com.jarvis.voice.mobile.live.LiveAudioPort
import com.jarvis.voice.shared.audio.Downsampler24To16
import com.jarvis.voice.shared.audio.LiveAudio
import com.jarvis.voice.shared.audio.PlaybackPump
import com.jarvis.voice.shared.wear.DataLayer

/**
 * Sessione live dello watch, lato telefono. L'AEC è un effetto LOCALE dello watch, quindi
 * qui c'è solo il tunnel: PATH_LIVE accende la modalità live sul watch, il parlato del
 * modello (24 kHz) viene spinto nel ChannelClient a ritmo di tempo reale, a 16 kHz
 * (ricampionato) o a 24 kHz pieni se [fullRate]. Il barge-in svuota la coda qui e manda il
 * clear IN BANDA, nello stesso stream e in ordine con l'audio (watch sul canale a frame);
 * solo per uno watch legacy (PCM grezzo) resta il PATH_CLEAR su MessageClient.
 * Il mic dello watch risale dal canale come oggi (WatchRelay → feedMicFrame).
 *
 * [mediaUsage]: lo watch suona il parlato live su USAGE_MEDIA (banda piena) invece che
 * sullo stream comms; il mic resta VOICE_COMMUNICATION + AEC in entrambi i casi.
 */
class WatchLiveAudio(
    private val send: (path: String, payload: String) -> Boolean,
    private val tunnel: Tunnel,
    private val fullRate: () -> Boolean = { false },
    private val mediaUsage: () -> Boolean = { true },
) : LiveAudioPort {

    /** Verso phone→watch del canale audio. */
    interface Tunnel {
        /** true = canale a frame ([com.jarvis.voice.shared.wear.WatchStream]); false = watch legacy. */
        val framed: Boolean
        fun live(pcm: ShortArray, sampleRate: Int, comms: Boolean)
        /** Clear in banda. false = non possibile (canale legacy o assente): serve PATH_CLEAR. */
        fun clear(): Boolean
    }

    private val tag = "WatchLiveAudio"
    private val lock = Any()
    @Volatile private var pump: PlaybackPump? = null
    @Volatile private var down: Downsampler24To16? = null
    private var watchLive = false   // PATH_LIVE start mandato e non ancora revocato

    /** Lo watch entra in live già durante l'handshake con l'hub (mic AEC pronto all'accept). */
    override fun prepare() {
        synchronized(lock) {
            if (watchLive) return
            watchLive = send(DataLayer.PATH_LIVE, DataLayer.LIVE_START)
            if (!watchLive) Log.w(tag, "prepare: nessuno watch noto")
        }
    }

    override fun start(onPlaybackLevel: (Float) -> Unit): Boolean = synchronized(lock) {
        if (pump != null) return true
        if (!watchLive && !send(DataLayer.PATH_LIVE, DataLayer.LIVE_START)) {
            Log.w(tag, "nessuno watch raggiungibile per la sessione live")
            return false
        }
        watchLive = true
        // Watch legacy: PCM grezzo, sempre 16 kHz, lo stream lo sceglie lui.
        val framed = tunnel.framed
        val rate = if (framed && fullRate()) LiveAudio.MODEL_SAMPLE_RATE else LiveAudio.WATCH_DEFAULT_RATE
        val comms = !mediaUsage()
        Log.i(tag, "tunnel live: $rate Hz, ${if (comms) "comms" else "media"}, ${if (framed) "a frame" else "legacy"}")
        down = if (rate == LiveAudio.MODEL_SAMPLE_RATE) null else Downsampler24To16()
        pump = PlaybackPump(
            rate,
            sink = { tunnel.live(it, rate, comms) },
            paceLeadMs = TUNNEL_LEAD_MS,
            onLevel = onPlaybackLevel,
            orderedClear = true,
        ).also { it.start() }
        true
    }

    // Chiamato solo dal thread di lettura dell'hub (il Downsampler non è thread-safe).
    override fun play(pcm: ShortArray) {
        val p = pump ?: return
        p.offer(down?.process(pcm) ?: pcm)
    }

    // Stesso thread di play(): l'audio offerto dopo il clear esce dopo il marker.
    override fun clear() {
        val p = pump ?: return
        var inBand = false
        p.clear { inBand = tunnel.clear() }
        if (!inBand) send(DataLayer.PATH_CLEAR, "")
    }

    override fun stop() {
        synchronized(lock) {
            if (pump == null && !watchLive) return
            pump?.stop()
            pump = null
            down = null
            watchLive = false
            send(DataLayer.PATH_LIVE, DataLayer.LIVE_STOP)
        }
    }

    private companion object {
        // Anticipo massimo nel tunnel: abbastanza da coprire il jitter BT, poco da lasciare
        // pochi ms di coda dopo un barge-in.
        const val TUNNEL_LEAD_MS = 160
    }
}
