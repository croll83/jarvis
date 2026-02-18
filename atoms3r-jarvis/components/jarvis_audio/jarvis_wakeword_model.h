/**
 * =============================================================================
 * JARVIS AtomS3R - Wake Word TFLite Model Data
 * =============================================================================
 *
 * This header embeds the jarvis_it.tflite model (60KB) as a C array.
 * The model was trained with microWakeWord using:
 *   - 1350 Italian TTS voices (male/female/child)
 *   - 100 real WAV recordings from AtomS3R
 *   - Negative samples with ambient noise
 *
 * To regenerate from the .tflite model:
 *   cd model_data/
 *   xxd -i jarvis_it.tflite | sed 's/jarvis_it_tflite/jarvis_wakeword_model/g'
 *   Then add 'const' qualifier and save as jarvis_wakeword_model.c
 */

#ifndef JARVIS_WAKEWORD_MODEL_H
#define JARVIS_WAKEWORD_MODEL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Embedded jarvis_it.tflite model data (60968 bytes) */
extern const unsigned char jarvis_wakeword_model[];
extern const unsigned int jarvis_wakeword_model_len;

#ifdef __cplusplus
}
#endif

#endif // JARVIS_WAKEWORD_MODEL_H
