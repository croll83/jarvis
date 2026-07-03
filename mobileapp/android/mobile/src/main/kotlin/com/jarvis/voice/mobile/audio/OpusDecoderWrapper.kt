package com.jarvis.voice.mobile.audio

import com.jarvis.voice.shared.protocol.AudioFormat
import io.github.jaredmdobson.concentus.OpusDecoder

/**
 * Decoder Opus per la TTS di ritorno (16 kHz mono). Usa Concentus (pure Java, no NDK).
 * L'orchestrator invia frame Opus da 20 ms via internal_tts.py.
 */
class OpusDecoderWrapper {

    private val decoder = OpusDecoder(AudioFormat.SAMPLE_RATE, 1)
    private val pcm = ShortArray(AudioFormat.OPUS_MAX_SAMPLES)

    /** Decodifica un frame Opus in PCM int16. Ritorna array vuoto in caso di errore. */
    fun decode(opus: ByteArray): ShortArray = try {
        val n = decoder.decode(opus, 0, opus.size, pcm, 0, AudioFormat.OPUS_MAX_SAMPLES, false)
        if (n > 0) pcm.copyOf(n) else ShortArray(0)
    } catch (_: Exception) {
        ShortArray(0)
    }
}
