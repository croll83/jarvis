/**
 * =============================================================================
 * JARVIS AtomS3R - Audio Module (ESP-IDF + ESP-SR)
 * =============================================================================
 *
 * Gestisce:
 * - Microfono PDM I2S
 * - Wake word detection con ESP-SR WakeNet (modello "jarvis")
 * - Streaming audio con VAD (Voice Activity Detection)
 */

#ifndef JARVIS_AUDIO_H
#define JARVIS_AUDIO_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// Callback types
typedef void (*wake_word_callback_t)(void);
typedef bool (*stream_chunk_callback_t)(int16_t* chunk, size_t samples);
typedef void (*stream_end_callback_t)(void);

/**
 * @brief Initialize audio system with ESP-SR
 * @return true on success
 */
bool jarvis_audio_init(void);

/**
 * @brief Deinitialize audio system
 */
void jarvis_audio_deinit(void);

/**
 * @brief Start listening for wake word
 */
void jarvis_audio_start_listening(void);

/**
 * @brief Stop listening for wake word
 */
void jarvis_audio_stop_listening(void);

/**
 * @brief Check if currently listening
 */
bool jarvis_audio_is_listening(void);

/**
 * @brief Start streaming audio
 */
void jarvis_audio_start_streaming(void);

/**
 * @brief Stop streaming audio
 */
void jarvis_audio_stop_streaming(void);

/**
 * @brief Check if currently streaming
 */
bool jarvis_audio_is_streaming(void);

/**
 * @brief Process audio (call from main loop or task)
 */
void jarvis_audio_process(void);

/**
 * @brief Get current audio level (0.0 - 1.0)
 */
float jarvis_audio_get_level(void);

/**
 * @brief Check if voice is currently active
 */
bool jarvis_audio_is_voice_active(void);

/**
 * @brief Set callbacks
 */
void jarvis_audio_set_callbacks(
    wake_word_callback_t wake_cb,
    stream_chunk_callback_t chunk_cb,
    stream_end_callback_t end_cb
);

#endif // JARVIS_AUDIO_H
