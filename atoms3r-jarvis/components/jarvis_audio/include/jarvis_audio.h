/**
 * =============================================================================
 * JARVIS AtomS3R - Audio Module (microWakeWord TFLite + Dual-Path)
 * =============================================================================
 *
 * Manages:
 * - Wake word detection with microWakeWord (TFLite Micro model)
 * - Dual-path audio feed: raw ring buffer (for WS audio) + TFLite inference
 *
 * Hardware init (I2S, I2C, ES8311, amplifier) is handled by jarvis_codec.
 * This module handles wake word inference and the raw audio ring buffer.
 */

#ifndef JARVIS_AUDIO_H
#define JARVIS_AUDIO_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Callback type for wake word detection
typedef void (*wake_word_callback_t)(void);

/**
 * @brief Initialize audio module: microWakeWord TFLite + ring buffer.
 * jarvis_codec_init() must be called before this.
 * @return true on success
 */
bool jarvis_audio_init(void);

/**
 * @brief Deinitialize audio module
 */
void jarvis_audio_deinit(void);

/**
 * @brief Start listening for wake word (enables microWakeWord inference)
 */
void jarvis_audio_start_listening(void);

/**
 * @brief Stop listening for wake word (disables microWakeWord inference)
 */
void jarvis_audio_stop_listening(void);

/**
 * @brief Check if currently listening for wake word
 */
bool jarvis_audio_is_listening(void);

/**
 * @brief Enable/disable raw audio ring buffer for audio streaming.
 * When enabled, the feed task writes raw mic audio to a ring buffer
 * that jarvis_ws_audio can read from.
 *
 * @param enable true to start buffering, false to stop
 */
void jarvis_audio_set_streaming(bool enable);

/**
 * @brief Read raw mono PCM samples from the ring buffer.
 * Used by jarvis_ws_audio to feed the Opus encoder.
 *
 * @param buf       Output buffer for mono 16-bit PCM
 * @param num_samples Number of samples to read
 * @param timeout_ms Max wait time in milliseconds
 * @return Number of samples read, or 0 on timeout/error
 */
size_t jarvis_audio_read_raw(int16_t *buf, size_t num_samples, uint32_t timeout_ms);

/**
 * @brief Process audio (call from main loop or task).
 * No-op — wake word detection runs in dedicated task.
 */
void jarvis_audio_process(void);

/**
 * @brief Get current audio level (0.0 - 1.0)
 */
float jarvis_audio_get_level(void);

/**
 * @brief Check if voice is currently active (energy-based VAD)
 */
bool jarvis_audio_is_voice_active(void);

/**
 * @brief Set wake word detection callback
 */
void jarvis_audio_set_wake_callback(wake_word_callback_t cb);

/**
 * @brief Set wake word detection sensitivity (probability cutoff) at runtime.
 * @param threshold Value between 0.0 and 1.0 (lower = more sensitive, higher = more precise).
 *                  Typical range: 0.5 - 0.98. Default: 0.82
 */
void jarvis_audio_set_sensitivity(float threshold);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_AUDIO_H
