/**
 * =============================================================================
 * JARVIS AtomS3R - Codec Module Implementation
 * =============================================================================
 *
 * Initializes and manages all audio hardware on the Atomic Echo Base:
 *   - ES8311 audio codec via local ES8311 driver (new I2C master API)
 *   - PI4IOE5V6408 GPIO expander for NS4150B speaker amplifier enable
 *   - I2S full-duplex (TX+RX) on I2S_NUM_0
 *
 * Uses new I2C master API (driver/i2c_master.h) — must match jarvis_display
 * to avoid ESP-IDF v6.x "CONFLICT! driver_ng" abort.
 * Uses new I2S API (driver/i2s_std.h) for full-duplex.
 *
 * Pin mapping (Atomic Echo Base → AtomS3R):
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

// I2S new API (full-duplex)
#include "driver/i2s_std.h"

// New I2C master API (must match jarvis_display to avoid driver conflict)
#include "driver/i2c_master.h"

// ES8311 driver (ported to new I2C API)
#include "es8311.h"

static const char *TAG = "CODEC";

// =============================================================================
// HARDWARE CONFIGURATION
// =============================================================================

// I2S pins (Atomic Echo Base → AtomS3R)
#define I2S_BCLK_PIN        8
#define I2S_WS_PIN          6
#define I2S_DIN_PIN         7   // ES8311 ADC → ESP32 (microphone)
#define I2S_DOUT_PIN        5   // ESP32 → ES8311 DAC (speaker)

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

// =============================================================================
// STATE
// =============================================================================

// I2S handles (full-duplex: TX + RX on same port)
static i2s_chan_handle_t rx_chan = NULL;
static i2s_chan_handle_t tx_chan = NULL;

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

    // Initialize codec with 32-bit resolution (same as OpenAI reference)
    // The driver handles: clock dividers, power sequencing, codec mode
    esp_err_t ret = es8311_init(es8311_handle, &clk_cfg,
                                 ES8311_RESOLUTION_32, ES8311_RESOLUTION_32);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "ES8311 init failed: %s", esp_err_to_name(ret));
        return false;
    }

    // Set DAC volume (0-100, driver handles proper register mapping)
    ret = es8311_voice_volume_set(es8311_handle, 80, NULL);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ES8311 volume set failed: %s", esp_err_to_name(ret));
    }

    // Configure microphone (false = not differential, single-ended MIC1P)
    ret = es8311_microphone_config(es8311_handle, false);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "ES8311 mic config failed: %s", esp_err_to_name(ret));
    }

    ESP_LOGI(TAG, "ES8311 codec initialized (16kHz, 32-bit res, volume=80%%)");
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
// I2S FULL-DUPLEX INIT (New API)
// =============================================================================

static bool init_i2s(void) {
    ESP_LOGI(TAG, "Initializing I2S full-duplex (BCLK=%d, WS=%d, DIN=%d, DOUT=%d)...",
             I2S_BCLK_PIN, I2S_WS_PIN, I2S_DIN_PIN, I2S_DOUT_PIN);

    // Create TX + RX channels on same port (full-duplex)
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;

    esp_err_t ret = i2s_new_channel(&chan_cfg, &tx_chan, &rx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S channel create failed: %s", esp_err_to_name(ret));
        return false;
    }

    // I2S standard mode config
    // EchoBase ES8311 operates with data on LEFT channel only.
    // Using MONO slot mode to match (equivalent to I2S_CHANNEL_FMT_ALL_LEFT in legacy API).
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = I2S_BCLK_PIN,
            .ws = I2S_WS_PIN,
            .dout = I2S_DOUT_PIN,
            .din = I2S_DIN_PIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    std_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    // Init both channels with same config (shared bus)
    ret = i2s_channel_init_std_mode(tx_chan, &std_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S TX init failed: %s", esp_err_to_name(ret));
        return false;
    }

    ret = i2s_channel_init_std_mode(rx_chan, &std_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S RX init failed: %s", esp_err_to_name(ret));
        return false;
    }

    ret = i2s_channel_enable(tx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S TX enable failed: %s", esp_err_to_name(ret));
        return false;
    }

    ret = i2s_channel_enable(rx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S RX enable failed: %s", esp_err_to_name(ret));
        return false;
    }

    ESP_LOGI(TAG, "I2S full-duplex initialized (16kHz, 16-bit, MONO/LEFT)");
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

    ESP_LOGI(TAG, "Initializing codec (ES8311 component + PI4IOE5V6408 + I2S)...");

    // 1. I2C bus (legacy API, must be before ES8311 and amp)
    if (!init_i2c()) {
        ESP_LOGE(TAG, "I2C bus init failed");
        return false;
    }

    // 2. ES8311 codec via component (handles clock, power, volume)
    if (!init_es8311()) {
        ESP_LOGE(TAG, "ES8311 codec init failed");
        return false;
    }

    // 3. PI4IOE5V6408 amplifier enable (non-fatal)
    if (!init_amplifier()) {
        ESP_LOGW(TAG, "Amplifier enable failed - speaker may not work");
    }

    // 4. I2S full-duplex
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

    if (rx_chan) {
        i2s_channel_disable(rx_chan);
        i2s_del_channel(rx_chan);
        rx_chan = NULL;
    }

    if (tx_chan) {
        i2s_channel_disable(tx_chan);
        i2s_del_channel(tx_chan);
        tx_chan = NULL;
    }

    if (es8311_handle) {
        es8311_delete(es8311_handle);
        es8311_handle = NULL;
    }

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

int jarvis_codec_read(int16_t *buf, size_t num_samples) {
    if (!initialized || !rx_chan || !buf || num_samples == 0) return -1;

    // I2S MONO mode → read directly, no de-interleave needed
    size_t bytes_needed = num_samples * sizeof(int16_t);
    size_t bytes_read = 0;

    esp_err_t ret = i2s_channel_read(rx_chan, buf, bytes_needed, &bytes_read, portMAX_DELAY);

    if (ret != ESP_OK || bytes_read == 0) {
        return -1;
    }

    return (int)(bytes_read / sizeof(int16_t));
}

int jarvis_codec_write(const int16_t *buf, size_t num_samples) {
    if (!initialized || !tx_chan || !buf || num_samples == 0) return -1;

    // I2S MONO mode → write directly, no stereo interleave needed
    size_t bytes_needed = num_samples * sizeof(int16_t);
    size_t bytes_written = 0;

    esp_err_t ret = i2s_channel_write(tx_chan, buf, bytes_needed,
                                       &bytes_written, pdMS_TO_TICKS(1000));
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S write error: %s", esp_err_to_name(ret));
        return -1;
    }

    return (int)(bytes_written / sizeof(int16_t));
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
