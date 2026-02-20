/**
 * =============================================================================
 * JARVIS AtomS3R - Codec Module Implementation
 * =============================================================================
 *
 * Initializes and manages all audio hardware on the Atomic Echo Base:
 *   - ES8311 audio codec via local ES8311 driver (new I2C master API)
 *   - PI4IOE5V6408 GPIO expander for NS4150B speaker amplifier enable
 *   - I2S full-duplex (TX+RX) on I2S_NUM_1 using legacy I2S API
 *
 * Uses new I2C master API (driver/i2c_master.h) — same as jarvis_display.
 * Uses legacy I2S API (driver/i2s.h) with I2S_CHANNEL_FMT_ALL_LEFT + APLL,
 * matching the proven OpenAI reference SDK configuration for AtomS3R.
 *
 * Pin mapping (Atomic Echo Base -> AtomS3R):
 *   I2C: SDA=GPIO38, SCL=GPIO39  (I2C_NUM_1, shared ES8311 + PI4IOE5V6408)
 *   I2S: BCLK=GPIO8, WS=GPIO6, DIN=GPIO7 (mic), DOUT=GPIO5 (speaker)
 *
 * Reference: OpenAI Realtime Embedded SDK media.cpp (M5_ATOMS3R config)
 */

#include "jarvis_codec.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_heap_caps.h"

// Legacy I2S API (proven working with ES8311 on AtomS3R)
#include "driver/i2s.h"

// New I2C master API (compatible with jarvis_display, available since IDF 5.0)
#include "driver/i2c_master.h"

// ES8311 driver (ported to new I2C API)
#include "es8311.h"

static const char *TAG = "CODEC";

// =============================================================================
// HARDWARE CONFIGURATION
// =============================================================================

// I2S pins (Atomic Echo Base -> AtomS3R)
#define I2S_BCLK_PIN        8
#define I2S_WS_PIN          6
#define I2S_DIN_PIN         7   // ES8311 ADC -> ESP32 (microphone)
#define I2S_DOUT_PIN        5   // ESP32 -> ES8311 DAC (speaker)

// I2S port (same as OpenAI reference SDK for AtomS3R)
#define I2S_PORT            I2S_NUM_1

// I2C bus (shared ES8311 + PI4IOE5V6408)
#define CODEC_I2C_SDA       38
#define CODEC_I2C_SCL       39
#define CODEC_I2C_FREQ_HZ   400000  // 400kHz (same as OpenAI reference)

// PI4IOE5V6408 I/O expander (controls NS4150B amplifier)
#define PI4IOE5V6408_ADDR       0x43
#define PI4IOE5V6408_REG_DIR    0x03   // I/O direction: 0=output, 1=input
#define PI4IOE5V6408_REG_OUT    0x05   // Output state

// Audio settings
#define MIC_SAMPLE_RATE     16000  // EchoBase requires 16kHz
#define BUFFER_SAMPLES      640    // 320 * 2 (same as OpenAI reference SDK)

// =============================================================================
// STATE
// =============================================================================

// I2C bus and device handles (new master API)
static i2c_master_bus_handle_t codec_i2c_bus = NULL;
static i2c_master_dev_handle_t es8311_i2c_dev = NULL;
static i2c_master_dev_handle_t amp_i2c_dev = NULL;

// ES8311 handle (from local es8311 driver)
static es8311_handle_t es8311_handle = NULL;

static bool initialized = false;

// =============================================================================
// I2C BUS INIT (New master API — compatible with jarvis_display)
// =============================================================================

static bool init_i2c(void) {
    ESP_LOGI(TAG, "Initializing I2C bus (new master API, SDA=%d, SCL=%d, %dHz)...",
             CODEC_I2C_SDA, CODEC_I2C_SCL, CODEC_I2C_FREQ_HZ);

    // Create I2C master bus on port 1 (port 0 used by jarvis_display for backlight)
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_1,
        .sda_io_num = CODEC_I2C_SDA,
        .scl_io_num = CODEC_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };

    esp_err_t ret = i2c_new_master_bus(&bus_cfg, &codec_i2c_bus);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C master bus create failed: %s", esp_err_to_name(ret));
        return false;
    }

    // Add ES8311 device (addr 0x18)
    i2c_device_config_t es_dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ES8311_ADDRESS_0,
        .scl_speed_hz = CODEC_I2C_FREQ_HZ,
    };
    ret = i2c_master_bus_add_device(codec_i2c_bus, &es_dev_cfg, &es8311_i2c_dev);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C add ES8311 device failed: %s", esp_err_to_name(ret));
        return false;
    }

    // Add PI4IOE5V6408 device (addr 0x43)
    i2c_device_config_t amp_dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = PI4IOE5V6408_ADDR,
        .scl_speed_hz = CODEC_I2C_FREQ_HZ,
    };
    ret = i2c_master_bus_add_device(codec_i2c_bus, &amp_dev_cfg, &amp_i2c_dev);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C add PI4IOE5V6408 device failed: %s", esp_err_to_name(ret));
        return false;
    }

    ESP_LOGI(TAG, "I2C bus initialized (new master API, port 1)");
    return true;
}

// =============================================================================
// ES8311 CODEC INIT (via local ES8311 driver, new I2C API)
// =============================================================================

static bool init_es8311(void) {
    ESP_LOGI(TAG, "Initializing ES8311 codec...");

    // Create ES8311 handle with I2C device handle
    es8311_handle = es8311_create(es8311_i2c_dev);
    if (es8311_handle == NULL) {
        ESP_LOGE(TAG, "Failed to create ES8311 handle");
        return false;
    }

    // Clock configuration — MCLK derived from BCLK (no external MCLK pin)
    es8311_clock_config_t clk_cfg = {
        .mclk_inverted = false,
        .sclk_inverted = false,
        .mclk_from_mclk_pin = false,  // MCLK from BCLK (slave clock)
        .sample_frequency = MIC_SAMPLE_RATE,
    };

    // Initialize codec with 32-bit resolution — MCLK requires 32-bit for clock dividers.
    // I2S is also set to 32-bit. jarvis_codec_read() extracts int16 from int32 samples.
    esp_err_t ret = es8311_init(es8311_handle, &clk_cfg,
                                 ES8311_RESOLUTION_32, ES8311_RESOLUTION_32);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "ES8311 init failed: %s", esp_err_to_name(ret));
        return false;
    }

    // Set DAC volume (0-100)
    ret = es8311_voice_volume_set(es8311_handle, 80, NULL);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ES8311 volume set failed: %s", esp_err_to_name(ret));
    }

    // Configure microphone (false = not digital, analog MIC1P)
    // Sets PGA=+30dB (max analog) and ADC volume=+16dB (digital)
    ret = es8311_microphone_config(es8311_handle, false);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ES8311 mic config failed: %s", esp_err_to_name(ret));
    }

    // Set ADC scale gain (REG16 bits[2:0]): 0=0dB, 1=6dB, ..., 7=42dB
    // M5Stack official example: ES8311_MIC_GAIN_6DB (+6dB) — too low, peak ~242/32767
    // XiaoZhi ESP32: ES8311_MIC_GAIN_30DB (+30dB) — good balance
    // ESPHome: ES8311_MIC_GAIN_42DB (+42dB) — aggressive
    // We use XiaoZhi level: +30dB. Total hardware chain:
    // PGA +30dB (analog) + ADC scale +30dB (digital) + ADC vol 0dB = +60dB
    ret = es8311_microphone_gain_set(es8311_handle, ES8311_MIC_GAIN_30DB);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ES8311 mic gain set failed: %s", esp_err_to_name(ret));
    }

    ESP_LOGI(TAG, "ES8311 codec initialized (16kHz, 32-bit I2S, volume=80%%, "
             "PGA=+30dB, ADC_vol=0dB, ADC_scale=+30dB)");
    return true;
}

// =============================================================================
// PI4IOE5V6408 AMPLIFIER ENABLE (New I2C master API)
// =============================================================================

static bool init_amplifier(void) {
    ESP_LOGI(TAG, "Enabling NS4150B amplifier via PI4IOE5V6408 (0x%02X)...",
             PI4IOE5V6408_ADDR);

    // All pins as output
    uint8_t dir_data[2] = {PI4IOE5V6408_REG_DIR, 0x00};
    esp_err_t ret = i2c_master_transmit(amp_i2c_dev, dir_data, sizeof(dir_data), pdMS_TO_TICKS(100));
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "PI4IOE5V6408 direction set failed: %s", esp_err_to_name(ret));
        return false;
    }

    // Enable amplifier (all outputs HIGH)
    uint8_t out_data[2] = {PI4IOE5V6408_REG_OUT, 0xFF};
    ret = i2c_master_transmit(amp_i2c_dev, out_data, sizeof(out_data), pdMS_TO_TICKS(100));
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "PI4IOE5V6408 output set failed: %s", esp_err_to_name(ret));
        return false;
    }

    ESP_LOGI(TAG, "NS4150B amplifier ENABLED");
    return true;
}

// =============================================================================
// I2S INIT (Legacy API — proven working on AtomS3R + Echo Base)
// =============================================================================

static bool init_i2s(void) {
    ESP_LOGI(TAG, "Initializing I2S (legacy API, BCLK=%d, WS=%d, DIN=%d, DOUT=%d)...",
             I2S_BCLK_PIN, I2S_WS_PIN, I2S_DIN_PIN, I2S_DOUT_PIN);

    // Legacy I2S config for ES8311 on AtomS3R
    // Stereo format: ES8311 sends L+R interleaved frames.
    // codec_read() de-interleaves to extract mono (left channel only).
    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX),
        .sample_rate = MIC_SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,  // Must match ES8311 32-bit resolution
        .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,  // Stereo: L+R interleaved
        .communication_format = I2S_COMM_FORMAT_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 8,
        .dma_buf_len = BUFFER_SAMPLES,
        .use_apll = 1,
        .tx_desc_auto_clear = true,
    };

    esp_err_t ret = i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S driver install failed: %s", esp_err_to_name(ret));
        return false;
    }

    i2s_pin_config_t pin_config = {
        .mck_io_num   = -1,            // no MCLK pin (derived from BCLK)
        .bck_io_num   = I2S_BCLK_PIN,  // GPIO8
        .ws_io_num    = I2S_WS_PIN,    // GPIO6
        .data_out_num = I2S_DOUT_PIN,  // GPIO5 (speaker)
        .data_in_num  = I2S_DIN_PIN,   // GPIO7 (microphone)
    };

    ret = i2s_set_pin(I2S_PORT, &pin_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S set pin failed: %s", esp_err_to_name(ret));
        return false;
    }

    i2s_zero_dma_buffer(I2S_PORT);

    ESP_LOGI(TAG, "I2S initialized (legacy API, 16kHz, 32-bit, STEREO, APLL, port %d)",
             I2S_PORT);
    return true;
}

// =============================================================================
// PUBLIC API
// =============================================================================

bool jarvis_codec_init(void) {
    if (initialized) {
        ESP_LOGW(TAG, "Codec already initialized");
        return true;
    }

    ESP_LOGI(TAG, "Initializing codec (ES8311 + PI4IOE5V6408 + I2S legacy)...");

    // 1. I2C bus (new master API, must be before ES8311 and amp)
    if (!init_i2c()) {
        ESP_LOGE(TAG, "I2C bus init failed");
        return false;
    }

    // 2. ES8311 codec (handles clock, power, volume)
    if (!init_es8311()) {
        ESP_LOGE(TAG, "ES8311 codec init failed");
        return false;
    }

    // 3. PI4IOE5V6408 amplifier enable (non-fatal)
    if (!init_amplifier()) {
        ESP_LOGW(TAG, "Amplifier enable failed - speaker may not work");
    }

    // 4. I2S full-duplex (legacy API with APLL + stereo RIGHT_LEFT)
    if (!init_i2s()) {
        ESP_LOGE(TAG, "I2S init failed");
        return false;
    }

    initialized = true;
    ESP_LOGI(TAG, "Codec initialized successfully");
    return true;
}

void jarvis_codec_deinit(void) {
    if (!initialized) return;

    // Uninstall I2S driver
    i2s_driver_uninstall(I2S_PORT);

    // Clean up ES8311
    if (es8311_handle) {
        es8311_delete(es8311_handle);
        es8311_handle = NULL;
    }

    // Clean up I2C devices
    if (es8311_i2c_dev) {
        i2c_master_bus_rm_device(es8311_i2c_dev);
        es8311_i2c_dev = NULL;
    }

    if (amp_i2c_dev) {
        i2c_master_bus_rm_device(amp_i2c_dev);
        amp_i2c_dev = NULL;
    }

    if (codec_i2c_bus) {
        i2c_del_master_bus(codec_i2c_bus);
        codec_i2c_bus = NULL;
    }

    initialized = false;
    ESP_LOGI(TAG, "Codec deinitialized");
}

// Persistent heap buffers for I2S read/write (avoids stack overflow in small tasks)
// Separate buffers for read vs write to allow concurrent access from different tasks
static int32_t *i2s_read_buf = NULL;
static size_t i2s_read_buf_size = 0;   // in int32 elements
static int32_t *i2s_write_buf = NULL;
static size_t i2s_write_buf_size = 0;  // in int32 elements

static int32_t *ensure_buf(int32_t **pbuf, size_t *psize, size_t stereo_samples_32) {
    if (*pbuf && *psize >= stereo_samples_32) {
        return *pbuf;
    }
    // Allocate (or grow) on PSRAM heap — never on stack
    if (*pbuf) free(*pbuf);
    *pbuf = (int32_t *)heap_caps_malloc(stereo_samples_32 * sizeof(int32_t),
                                         MALLOC_CAP_SPIRAM);
    if (!*pbuf) {
        // Fallback to internal RAM
        *pbuf = (int32_t *)malloc(stereo_samples_32 * sizeof(int32_t));
    }
    *psize = *pbuf ? stereo_samples_32 : 0;
    return *pbuf;
}

// Stereo I2S (RIGHT_LEFT) at 32-bit: read int32 samples, extract int16 from MSBs
int jarvis_codec_read(int16_t *buf, size_t num_samples) {
    if (!initialized || !buf || num_samples == 0) return -1;

    // I2S is 32-bit stereo: each frame = L(32bit) + R(32bit) = 8 bytes
    // We need num_samples mono frames → num_samples * 2 stereo int32 samples
    size_t stereo_samples_32 = num_samples * 2;
    size_t bytes_to_read = stereo_samples_32 * sizeof(int32_t);

    int32_t *stereo_buf = ensure_buf(&i2s_read_buf, &i2s_read_buf_size, stereo_samples_32);
    if (!stereo_buf) return -1;

    size_t bytes_read = 0;
    esp_err_t ret = i2s_read(I2S_PORT, stereo_buf, bytes_to_read, &bytes_read, portMAX_DELAY);
    if (ret != ESP_OK || bytes_read == 0) {
        return -1;
    }

    // De-interleave: take left channel (even indices), extract int16 from int32
    // ES8311 32-bit format: audio data is in the upper 16 bits (MSBs)
    size_t total_int32_samples = bytes_read / sizeof(int32_t);
    size_t mono_count = total_int32_samples / 2;

    for (size_t i = 0; i < mono_count; i++) {
        int32_t raw = stereo_buf[i * 2];  // Left channel (even index)
        buf[i] = (int16_t)(raw >> 16);    // Extract upper 16 bits
    }

    return (int)mono_count;
}

// Write mono int16 samples as stereo int32 (duplicate L=R, shift to upper bits) for ES8311
int jarvis_codec_write(const int16_t *buf, size_t num_samples) {
    if (!initialized || !buf || num_samples == 0) return -1;

    // I2S is 32-bit stereo: each frame = L(32bit) + R(32bit)
    size_t stereo_samples_32 = num_samples * 2;
    size_t bytes_to_write = stereo_samples_32 * sizeof(int32_t);

    int32_t *stereo_buf = ensure_buf(&i2s_write_buf, &i2s_write_buf_size, stereo_samples_32);
    if (!stereo_buf) return -1;

    for (size_t i = 0; i < num_samples; i++) {
        int32_t sample_32 = ((int32_t)buf[i]) << 16;  // int16 → upper 16 bits of int32
        stereo_buf[i * 2] = sample_32;      // Left
        stereo_buf[i * 2 + 1] = sample_32;  // Right = same
    }

    size_t bytes_written = 0;
    esp_err_t ret = i2s_write(I2S_PORT, stereo_buf, bytes_to_write, &bytes_written, pdMS_TO_TICKS(1000));

    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S write error: %s", esp_err_to_name(ret));
        return -1;
    }

    return (int)(bytes_written / sizeof(int32_t) / 2);  // return mono sample count
}

void jarvis_codec_set_volume(int volume) {
    if (!es8311_handle) return;
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;

    esp_err_t ret = es8311_voice_volume_set(es8311_handle, volume, NULL);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "DAC volume set to %d%%", volume);
    } else {
        ESP_LOGE(TAG, "Failed to set volume: %s", esp_err_to_name(ret));
    }
}

void jarvis_codec_set_mic_gain(uint8_t gain_reg_val) {
    if (!es8311_i2c_dev) return;

    // Direct register write for PGA gain (ES8311 reg 0x14 = SYSTEM_REG14)
    // The es8311 driver doesn't expose a direct PGA gain API,
    // so we write the register directly via I2C
    uint8_t data[2] = {0x14, gain_reg_val};

    esp_err_t ret = i2c_master_transmit(es8311_i2c_dev, data, sizeof(data), pdMS_TO_TICKS(100));

    if (ret == ESP_OK) {
        int gain_db = (gain_reg_val & 0x0F) * 3;
        ESP_LOGI(TAG, "PGA gain set to +%ddB (reg 0x14=0x%02X)", gain_db, gain_reg_val);
    } else {
        ESP_LOGE(TAG, "Failed to set PGA gain: %s", esp_err_to_name(ret));
    }
}
