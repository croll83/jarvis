/**
 * =============================================================================
 * JARVIS AtomS3R - Speaker Module (ESP-IDF)
 * =============================================================================
 *
 * Audio output via jarvis_codec (shared I2S TX):
 * - Path: ESP32 → I2S TX (DOUT=GPIO5) → ES8311 DAC → NS4150B → Speaker
 * - Playback of short PCM sounds (wake word feedback)
 * - Non-blocking playback via FreeRTOS task
 *
 * NOTA: jarvis_codec_init() DEVE essere chiamato prima di jarvis_speaker_init()
 */

#ifndef JARVIS_SPEAKER_H
#define JARVIS_SPEAKER_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize speaker module
 * @return true on success
 */
bool jarvis_speaker_init(void);

/**
 * @brief Deinitialize speaker
 */
void jarvis_speaker_deinit(void);

/**
 * @brief Play the wake word feedback sound (non-blocking)
 *
 * Starts a FreeRTOS task to play the harmonic_rise sound.
 * If playback is already in progress, does nothing.
 */
void jarvis_speaker_play_wake_sound(void);

/**
 * @brief Play a short reject buzz (non-blocking)
 *
 * Brief low-frequency buzz (~80ms, 400Hz) as feedback when
 * a wake word trigger is rejected by the false-positive filter.
 * Useful for tuning without serial monitor.
 * If playback is already in progress, does nothing.
 */
void jarvis_speaker_play_buzz(void);

/**
 * @brief Play a short listening beep (non-blocking)
 *
 * Higher-pitched beep (~100ms, 880Hz) as feedback when
 * multi-turn listening reactivates. Signals "speak now" to the user
 * without requiring them to look at the screen.
 * If playback is already in progress, does nothing.
 */
void jarvis_speaker_play_listening_beep(void);

/**
 * @brief Play raw PCM data (blocking)
 * @param pcm_data PCM 16-bit signed mono samples
 * @param num_samples Number of samples
 */
void jarvis_speaker_play_pcm(const int16_t* pcm_data, size_t num_samples);

/**
 * @brief Check if speaker is currently playing
 */
bool jarvis_speaker_is_playing(void);

/**
 * @brief Stop current playback
 */
void jarvis_speaker_stop(void);

/**
 * @brief Wait for current playback to finish
 * @param timeout_ms Maximum wait time in milliseconds
 * @return true if playback finished, false on timeout
 */
bool jarvis_speaker_wait_done(uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_SPEAKER_H
