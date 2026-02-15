/**
 * =============================================================================
 * JARVIS AtomS3R - Audio Module (ESP-SR WakeNet + Dual-Path)
 * =============================================================================
 *
 * Manages:
 * - Wake word detection with ESP-SR WakeNet (model "jarvis")
 * - Dual-path audio feed: raw ring buffer (for WebRTC) + AFE (for WakeNet)
 *
 * Hardware init (I2S, I2C, ES8311, amplifier) is handled by jarvis_codec.
 * This module only handles ESP-SR (AFE/WakeNet) and the raw audio ring buffer.
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
 * @brief Initialize audio module: ESP-SR AFE/WakeNet + ring buffer.
 * jarvis_codec_init() must be called before this.
 * @return true on success
 */
bool jarvis_audio_init(void);

/**
 * @brief Deinitialize audio module
 */
void jarvis_audio_deinit(void);

/**
 * @brief Start listening for wake word (enables WakeNet)
 */
void jarvis_audio_start_listening(void);

/**
 * @brief Stop listening for wake word (disables WakeNet)
 */
void jarvis_audio_stop_listening(void);

/**
 * @brief Check if currently listening for wake word
 */
bool jarvis_audio_is_listening(void);

/**
 * @brief Enable/disable raw audio ring buffer for WebRTC streaming.
 * When enabled, the feed task writes raw mic audio to a ring buffer
 * that jarvis_webrtc can read from. Also switches PGA gain:
 *   true  → 0dB  (clean audio for transcription)
 *   false → +12dB (sensitive for wake word detection)
 *
 * @param enable true to start buffering, false to stop
 */
void jarvis_audio_set_streaming(bool enable);

/**
 * @brief Read raw mono PCM samples from the ring buffer.
 * Used by jarvis_webrtc to feed the Opus encoder.
 *
 * @param buf       Output buffer for mono 16-bit PCM
 * @param num_samples Number of samples to read
 * @param timeout_ms Max wait time in milliseconds
 * @return Number of samples read, or 0 on timeout/error
 */
size_t jarvis_audio_read_raw(int16_t *buf, size_t num_samples, uint32_t timeout_ms);

/**
 * @brief Process audio (call from main loop or task).
 * Fetches AFE results and checks for wake word detection.
 */
void jarvis_audio_process(void);

/**
 * @brief Get current audio level (0.0 - 1.0)
 */
float jarvis_audio_get_level(void);

/**
 * @brief Check if voice is currently active (AFE VAD)
 */
bool jarvis_audio_is_voice_active(void);

/**
 * @brief Set wake word detection callback
 */
void jarvis_audio_set_wake_callback(wake_word_callback_t cb);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_AUDIO_H
