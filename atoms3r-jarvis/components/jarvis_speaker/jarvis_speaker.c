/**
 * =============================================================================
 * JARVIS AtomS3R - Speaker Module Implementation (ESP-IDF)
 * =============================================================================
 *
 * Output audio via I2S TX su Atomic SPK Base (NS4168 amplifier).
 * Pin I2S: BCLK=GPIO5, DATA=GPIO38, LRCK=GPIO39
 *
 * Il suono wake word (harmonic_rise) è embedded come array const in flash.
 * Il playback è non-bloccante (FreeRTOS task separato).
 */

#include "jarvis_speaker.h"
#include "wake_sound_data.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "driver/i2s_std.h"

static const char *TAG = "SPEAKER";

// =============================================================================
// CONFIGURATION - Atomic SPK Base (NS4168)
// =============================================================================

#define SPK_BCLK_PIN    5
#define SPK_DATA_PIN    38
#define SPK_LRCK_PIN    39

#define SPK_SAMPLE_RATE 16000
#define SPK_DMA_DESC    8
#define SPK_DMA_FRAME   256

// =============================================================================
// STATE
// =============================================================================

static i2s_chan_handle_t tx_chan = NULL;
static bool speaker_initialized = false;
static bool playing = false;
static volatile bool stop_requested = false;

static TaskHandle_t playback_task_handle = NULL;

// =============================================================================
// INITIALIZATION
// =============================================================================

bool jarvis_speaker_init(void) {
    ESP_LOGI(TAG, "Initializing speaker (NS4168 via I2S)...");

    // Create I2S TX channel on I2S_NUM_1 (I2S_NUM_0 is used by PDM mic RX)
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    chan_cfg.dma_desc_num = SPK_DMA_DESC;
    chan_cfg.dma_frame_num = SPK_DMA_FRAME;

    esp_err_t ret = i2s_new_channel(&chan_cfg, &tx_chan, NULL);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create I2S TX channel: %s", esp_err_to_name(ret));
        return false;
    }

    // Configure standard I2S mode for NS4168
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SPK_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = SPK_BCLK_PIN,
            .ws = SPK_LRCK_PIN,
            .dout = SPK_DATA_PIN,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    ret = i2s_channel_init_std_mode(tx_chan, &std_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to init I2S STD TX: %s", esp_err_to_name(ret));
        i2s_del_channel(tx_chan);
        tx_chan = NULL;
        return false;
    }

    ret = i2s_channel_enable(tx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enable I2S TX: %s", esp_err_to_name(ret));
        i2s_del_channel(tx_chan);
        tx_chan = NULL;
        return false;
    }

    speaker_initialized = true;
    ESP_LOGI(TAG, "Speaker initialized (BCLK=%d, DATA=%d, LRCK=%d, %dHz)",
             SPK_BCLK_PIN, SPK_DATA_PIN, SPK_LRCK_PIN, SPK_SAMPLE_RATE);
    return true;
}

void jarvis_speaker_deinit(void) {
    jarvis_speaker_stop();

    if (tx_chan) {
        i2s_channel_disable(tx_chan);
        i2s_del_channel(tx_chan);
        tx_chan = NULL;
    }
    speaker_initialized = false;
    ESP_LOGI(TAG, "Speaker deinitialized");
}

// =============================================================================
// PLAYBACK
// =============================================================================

void jarvis_speaker_play_pcm(const int16_t* pcm_data, size_t num_samples) {
    if (!speaker_initialized || !tx_chan) {
        ESP_LOGW(TAG, "Speaker not initialized");
        return;
    }

    playing = true;
    stop_requested = false;

    size_t bytes_written = 0;
    size_t total_bytes = num_samples * sizeof(int16_t);
    size_t offset = 0;

    // Scrivi in blocchi per permettere stop
    const size_t block_size = 512 * sizeof(int16_t);  // 512 samples per blocco

    while (offset < total_bytes && !stop_requested) {
        size_t remaining = total_bytes - offset;
        size_t to_write = remaining < block_size ? remaining : block_size;

        esp_err_t ret = i2s_channel_write(tx_chan,
                                           (const uint8_t*)pcm_data + offset,
                                           to_write,
                                           &bytes_written,
                                           pdMS_TO_TICKS(1000));

        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "I2S write error: %s", esp_err_to_name(ret));
            break;
        }

        offset += bytes_written;
    }

    // Silenzio finale per flush DMA
    int16_t silence[128] = {0};
    i2s_channel_write(tx_chan, silence, sizeof(silence), &bytes_written, pdMS_TO_TICKS(100));

    playing = false;
}

// FreeRTOS task per playback non-bloccante
static void wake_sound_task(void* arg) {
    ESP_LOGI(TAG, "Playing wake sound (%d samples, %dms)",
             WAKE_SOUND_SAMPLES, WAKE_SOUND_DURATION_MS);

    jarvis_speaker_play_pcm(wake_sound_pcm, WAKE_SOUND_SAMPLES);

    ESP_LOGI(TAG, "Wake sound playback complete");
    playback_task_handle = NULL;
    vTaskDelete(NULL);
}

void jarvis_speaker_play_wake_sound(void) {
    if (!speaker_initialized) {
        ESP_LOGW(TAG, "Speaker not initialized, cannot play wake sound");
        return;
    }

    if (playing) {
        ESP_LOGD(TAG, "Already playing, skip wake sound");
        return;
    }

    // Crea un task separato per il playback non-bloccante
    BaseType_t ret = xTaskCreatePinnedToCore(
        wake_sound_task,
        "wake_sound",
        4096,
        NULL,
        4,      // Priorità medio-alta
        &playback_task_handle,
        1       // Core 1
    );

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create wake sound task");
    }
}

bool jarvis_speaker_is_playing(void) {
    return playing;
}

void jarvis_speaker_stop(void) {
    stop_requested = true;

    // Aspetta che il task finisca
    if (playback_task_handle) {
        int timeout = 50;  // 500ms max
        while (playing && timeout-- > 0) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}
