/**
 * =============================================================================
 * JARVIS AtomS3R - Codec Module (ES8311 + PI4IOE5V6408 + I2S)
 * =============================================================================
 *
 * Shared audio hardware layer for the Atomic Echo Base:
 *   - ES8311 audio codec (I2C addr 0x18): ADC (mic) + DAC (speaker)
 *   - PI4IOE5V6408 I/O expander (I2C addr 0x43): controls NS4150B amp enable
 *   - I2S full-duplex on I2S_NUM_1: RX (mic) + TX (speaker) via legacy I2S API
 *
 * This component owns all hardware init/deinit. Other components (jarvis_audio,
 * jarvis_speaker, jarvis_ws_audio) use this API to read/write audio.
 */

#ifndef JARVIS_CODEC_H
#define JARVIS_CODEC_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Initialize codec hardware: I2C bus, ES8311, PI4IOE5V6408 amp, I2S full-duplex.
 * Must be called before any other jarvis_codec_* function.
 * @return true on success
 */
bool jarvis_codec_init(void);

/**
 * Deinitialize codec hardware (power down ES8311, free I2S/I2C).
 */
void jarvis_codec_deinit(void);

/**
 * Read audio from microphone (I2S RX).
 * With I2S_CHANNEL_FMT_ALL_LEFT, data is already mono — direct read.
 *
 * @param buf       Output buffer for mono 16-bit PCM samples
 * @param num_samples Number of mono samples to read
 * @return Number of mono samples actually read, or -1 on error
 */
int jarvis_codec_read(int16_t *buf, size_t num_samples);

/**
 * Write audio to speaker (I2S TX).
 * With I2S_CHANNEL_FMT_ALL_LEFT, mono data is written directly.
 *
 * @param buf       Input buffer of mono 16-bit PCM samples
 * @param num_samples Number of mono samples to write
 * @return Number of mono samples actually written, or -1 on error
 */
int jarvis_codec_write(const int16_t *buf, size_t num_samples);

/**
 * Set ES8311 DAC output volume.
 * @param volume 0-100
 */
void jarvis_codec_set_volume(int volume);

/**
 * Set ES8311 ADC PGA gain register value.
 * Used for dual-gain approach:
 *   - 0x14 = MIC1P + 12dB (wake word detection)
 *   - 0x10 = MIC1P + 0dB  (clean streaming for transcription)
 *
 * @param gain_reg_val Raw register 0x14 value
 */
void jarvis_codec_set_mic_gain(uint8_t gain_reg_val);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_CODEC_H
