package com.jarvis.voice.shared.protocol

/** Formato audio della catena JARVIS. Deve combaciare con l'orchestrator. */
object AudioFormat {
    const val SAMPLE_RATE = 16000
    const val FRAME_SAMPLES = 320            // 20 ms @ 16 kHz
    const val FRAME_BYTES = FRAME_SAMPLES * 2 // int16 mono
    const val OPUS_MAX_SAMPLES = 960          // buffer massimo per la decode (fino a 60 ms)
}
