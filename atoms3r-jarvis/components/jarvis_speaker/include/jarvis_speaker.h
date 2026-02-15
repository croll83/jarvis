/**
 * =============================================================================
 * JARVIS AtomS3R - Speaker Module (ESP-IDF)
 * =============================================================================
 *
 * Gestisce l'output audio via I2S TX su Atomic SPK Base (NS4168):
 * - Inizializzazione I2S TX channel
 * - Playback di suoni PCM brevi (wake word feedback)
 * - Non-blocking playback via FreeRTOS task
 */

#ifndef JARVIS_SPEAKER_H
#define JARVIS_SPEAKER_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/**
 * @brief Initialize speaker (I2S TX for Atomic SPK Base NS4168)
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
 * Avvia un FreeRTOS task che riproduce il suono harmonic_rise.
 * Se un playback è già in corso, viene ignorato.
 */
void jarvis_speaker_play_wake_sound(void);

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

#endif // JARVIS_SPEAKER_H
