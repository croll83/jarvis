package com.jarvis.voice.mobile.live

/**
 * Lato audio di una sessione live sul Voice Live Hub, per un target (telefono o watch).
 * Il controller lo accende all'accept dell'hub e lo spegne a fine sessione: fuori da una
 * sessione live non esiste nulla di acceso (mic comms, AEC, MODE_IN_COMMUNICATION, code).
 *
 * L'uplink del mic arriva al controller via feedMicFrame() come oggi (mic locale del
 * telefono o tunnel dello watch); qui passa solo il downlink e il barge-in.
 */
interface LiveAudioPort {
    /**
     * Sessione appena aperta, l'hub non ha ancora accettato: un target remoto può già
     * passare in modalità live (lo watch apre il mic AEC prima dell'accept, così l'hub non
     * riceve mai il mic x8 senza AEC). Dopo prepare() la fine sessione chiama sempre [stop].
     */
    fun prepare() {}

    /** Entra in modalità live (mic continuo + AEC). [onPlaybackLevel] = livello 0..1 del parlato del modello. */
    fun start(onPlaybackLevel: (Float) -> Unit): Boolean

    /** Parlato del modello, PCM 24 kHz mono (non bloccante). */
    fun play(pcm: ShortArray)

    /** Barge-in: butta SUBITO il parlato non ancora suonato. */
    fun clear()

    /** Esce dalla modalità live e ripristina tutto. Idempotente. */
    fun stop()
}
