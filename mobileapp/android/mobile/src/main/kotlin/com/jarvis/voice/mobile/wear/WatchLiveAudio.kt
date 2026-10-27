package com.jarvis.voice.mobile.wear

import android.util.Log
import com.jarvis.voice.mobile.live.LiveAudioPort
import com.jarvis.voice.shared.audio.Downsampler24To16
import com.jarvis.voice.shared.audio.PlaybackPump
import com.jarvis.voice.shared.protocol.AudioFormat
import com.jarvis.voice.shared.wear.DataLayer

/**
 * Sessione live dello watch, lato telefono. L'AEC è un effetto LOCALE dello watch, quindi
 * qui c'è solo il tunnel: PATH_LIVE accende la modalità live sul watch, il parlato del
 * modello (24 kHz) viene ricampionato a 16 kHz e spinto nel ChannelClient a ritmo di tempo
 * reale, il barge-in svuota la coda qui e manda PATH_CLEAR al watch. Il mic dello watch
 * risale dal canale come oggi (WatchRelay → feedMicFrame).
 */
class WatchLiveAudio(
    private val send: (path: String, payload: String) -> Boolean,
    private val sink: (ShortArray) -> Unit,
) : LiveAudioPort {

    private val tag = "WatchLiveAudio"
    private val lock = Any()
    private var pump: PlaybackPump? = null
    private var down: Downsampler24To16? = null
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
        down = Downsampler24To16()
        pump = PlaybackPump(AudioFormat.SAMPLE_RATE, sink = sink, paceLeadMs = TUNNEL_LEAD_MS, onLevel = onPlaybackLevel)
            .also { it.start() }
        true
    }

    // Chiamato solo dal thread di lettura dell'hub (il Downsampler non è thread-safe).
    override fun play(pcm: ShortArray) {
        val d = down ?: return
        pump?.offer(d.process(pcm))
    }

    override fun clear() {
        pump?.clear()
        send(DataLayer.PATH_CLEAR, "")
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
