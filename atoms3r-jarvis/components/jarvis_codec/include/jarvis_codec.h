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
 * Same as jarvis_codec_write(), but with its own scratch buffer, so a
 * streaming task (duplex live playback) can write while other writers
 * (wake sound, TTS in the WS handler) use jarvis_codec_write().
 */
int jarvis_codec_write_stream(const int16_t *buf, size_t num_samples);

/**
 * Mute/unmute the ES8311 DAC output (does not touch the volume setting).
 * Used on barge-in to silence audio already queued in the I2S TX DMA ring.
 * @param mute true = mute, false = unmute
 */
void jarvis_codec_set_mute(bool mute);

/** True while the DAC is muted: the loopback may still carry audio the speaker does not play. */
bool jarvis_codec_is_muted(void);

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

/**
 * Digital DAC loopback on the right I2S RX slot (ES8311 REG44 "ADC + DACR").
 * On: every RX frame carries the mic (left) and, sample-aligned, the PCM the
 * DAC received (right): the echo canceller reference for duplex sessions.
 * Off: back to the reset default (both slots = mic).
 * @return true if the register reads back as requested
 */
bool jarvis_codec_set_aec_loopback(bool on);

/**
 * Read both I2S RX slots: even index -> mic, odd index -> ref. With the loopback
 * on that is mic and DAC input from the same LRCK frame, if the ESP32 maps the
 * left slot to the even index (the server checks which slot is the loopback).
 * @return Number of frames read, or -1 on error
 */
int jarvis_codec_read_pair(int16_t *mic, int16_t *ref, size_t num_samples);

/**
 * Set the ES8311 ADC digital scale (REG16), 0-42 dB in 6 dB steps.
 * The firmware default is JARVIS_CODEC_MIC_SCALE_DB.
 */
#define JARVIS_CODEC_MIC_SCALE_DB 30
void jarvis_codec_set_mic_scale_db(int db);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_CODEC_H
