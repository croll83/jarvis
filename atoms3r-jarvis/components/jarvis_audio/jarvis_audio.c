/**
 * =============================================================================
 * JARVIS AtomS3R - Audio Module Implementation (ESP-IDF + ESP-SR)
 * =============================================================================
 *
 * Carica il modello WakeNet dalla partizione SPIFFS "model" invece che embedded.
 * I file del modello (wn9_jarvis.bin, ecc.) devono essere nella partizione.
 */

#include "jarvis_audio.h"

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/i2s_pdm.h"

// ESP-SR includes
#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_mn_speech_commands.h"
#include "model_path.h"

static const char *TAG = "AUDIO";

// =============================================================================
// CONFIGURATION
// =============================================================================

#define MIC_SAMPLE_RATE     16000
#define MIC_CLK_PIN         1
#define MIC_DATA_PIN        2

#define AUDIO_CHUNK_SIZE    512
#define STREAM_CHUNK_SAMPLES 512

// VAD configuration
#define VAD_SILENCE_CHUNKS      30      // ~1 second of silence
#define STREAM_MAX_DURATION_MS  60000   // 60s safety timeout
#define STREAM_MIN_AUDIO_MS     500     // Minimum audio before VAD check

// Model partition name
#define MODEL_PARTITION_LABEL   "model"

// Streaming states
typedef enum {
    STREAM_IDLE,
    STREAM_STARTING,
    STREAM_ACTIVE,
    STREAM_ENDING
} stream_state_t;

// =============================================================================
// STATE
// =============================================================================

static bool listening = false;
static stream_state_t stream_state = STREAM_IDLE;
static int64_t stream_start_time = 0;
static int silence_chunk_count = 0;
static int voice_chunk_count = 0;

static float audio_level = 0.0f;
static bool voice_active = false;

// Callbacks
static wake_word_callback_t wake_callback = NULL;
static stream_chunk_callback_t chunk_callback = NULL;
static stream_end_callback_t end_callback = NULL;

// I2S
static i2s_chan_handle_t rx_chan = NULL;
static bool i2s_initialized = false;

// ESP-SR
static esp_afe_sr_iface_t *afe_handle = NULL;
static esp_afe_sr_data_t *afe_data = NULL;
static srmodel_list_t *sr_models = NULL;
static bool wakenet_initialized = false;

// Audio buffers
static int16_t *chunk_buffer = NULL;

// AFE feed task
static TaskHandle_t afe_task_handle = NULL;

// =============================================================================
// HELPER FUNCTIONS
// =============================================================================

static float calculate_rms(int16_t* samples, size_t count) {
    if (count == 0) return 0;

    int64_t sum = 0;
    for (size_t i = 0; i < count; i++) {
        sum += (int64_t)samples[i] * samples[i];
    }

    float rms = sqrtf((float)sum / count);
    float normalized = rms / 10000.0f;
    return normalized > 1.0f ? 1.0f : normalized;
}

// =============================================================================
// MODEL CLEANUP
// =============================================================================

static void deinit_models(void) {
    if (sr_models != NULL) {
        esp_srmodel_deinit(sr_models);
        sr_models = NULL;
        ESP_LOGI(TAG, "SR models deinitialized");
    }
}

// =============================================================================
// AFE FEED TASK
// =============================================================================

static void afe_feed_task(void* arg) {
    size_t bytes_read = 0;
    int16_t* i2s_buff = heap_caps_malloc(AUDIO_CHUNK_SIZE * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);

    if (!i2s_buff) {
        i2s_buff = malloc(AUDIO_CHUNK_SIZE * sizeof(int16_t));
    }

    if (!i2s_buff) {
        ESP_LOGE(TAG, "Failed to allocate I2S buffer");
        vTaskDelete(NULL);
        return;
    }

    while (1) {
        if (rx_chan && afe_data) {
            esp_err_t ret = i2s_channel_read(rx_chan, i2s_buff, AUDIO_CHUNK_SIZE * sizeof(int16_t), &bytes_read, portMAX_DELAY);

            if (ret == ESP_OK && bytes_read > 0 && afe_handle) {
                afe_handle->feed(afe_data, i2s_buff);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }

    free(i2s_buff);
    vTaskDelete(NULL);
}

// =============================================================================
// INITIALIZATION
// =============================================================================

static bool init_i2s(void) {
    ESP_LOGI(TAG, "Initializing I2S PDM...");

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;

    esp_err_t ret = i2s_new_channel(&chan_cfg, NULL, &rx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create I2S channel: %s", esp_err_to_name(ret));
        return false;
    }

    i2s_pdm_rx_config_t pdm_rx_cfg = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .clk = MIC_CLK_PIN,
            .din = MIC_DATA_PIN,
            .invert_flags = {
                .clk_inv = false,
            },
        },
    };

    ret = i2s_channel_init_pdm_rx_mode(rx_chan, &pdm_rx_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to init PDM RX: %s", esp_err_to_name(ret));
        return false;
    }

    ret = i2s_channel_enable(rx_chan);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enable I2S channel: %s", esp_err_to_name(ret));
        return false;
    }

    ESP_LOGI(TAG, "I2S PDM initialized");
    return true;
}

static bool init_wakenet(void) {
    ESP_LOGI(TAG, "Initializing ESP-SR WakeNet...");

    // Initialize model list from SPIFFS partition
    // ESP-SR will mount SPIFFS and load models from the specified partition label
    sr_models = esp_srmodel_init(MODEL_PARTITION_LABEL);
    if (sr_models == NULL) {
        ESP_LOGE(TAG, "Failed to load models from partition '%s'", MODEL_PARTITION_LABEL);
        return false;
    }
    ESP_LOGI(TAG, "Loaded %d models from SPIFFS", sr_models->num);

    // Configure AFE
    afe_config_t afe_config = AFE_CONFIG_DEFAULT();

    // Microphone configuration
    afe_config.pcm_config.total_ch_num = 1;
    afe_config.pcm_config.mic_num = 1;
    afe_config.pcm_config.ref_num = 0;
    afe_config.pcm_config.sample_rate = MIC_SAMPLE_RATE;

    // Enable WakeNet with "jarvis" model from SPIFFS
    afe_config.wakenet_init = true;
    afe_config.wakenet_model_name = "wn9_jarvis";
    afe_config.wakenet_mode = DET_MODE_95;  // Medium sensitivity

    // Enable VAD
    afe_config.vad_init = true;

    // Disable AEC (no reference speaker)
    afe_config.aec_init = false;

    // Speech enhancement
    afe_config.se_init = true;

    // Low-cost mode for AtomS3R
    afe_config.afe_mode = SR_MODE_LOW_COST;

    // Use PSRAM
    afe_config.memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;

    // Core and priority
    afe_config.afe_perferred_core = 0;
    afe_config.afe_perferred_priority = 5;
    afe_config.afe_ringbuf_size = 50;

    // AGC
    afe_config.agc_mode = AFE_MN_PEAK_AGC_MODE_2;

    // Create AFE handle (cast to remove const warning)
    afe_handle = (esp_afe_sr_iface_t *)&ESP_AFE_SR_HANDLE;
    afe_data = afe_handle->create_from_config(&afe_config);

    if (!afe_data) {
        ESP_LOGE(TAG, "Failed to create AFE - model may not be found in partition");
        deinit_models();
        return false;
    }

    // Start AFE feed task
    xTaskCreatePinnedToCore(
        afe_feed_task,
        "afe_feed",
        4096,
        NULL,
        5,
        &afe_task_handle,
        1  // Core 1
    );

    ESP_LOGI(TAG, "WakeNet initialized: model=wn9_jarvis (from SPIFFS), VAD=ON");
    return true;
}

bool jarvis_audio_init(void) {
    ESP_LOGI(TAG, "Initializing audio module...");

    // Allocate chunk buffer
    chunk_buffer = heap_caps_malloc(STREAM_CHUNK_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!chunk_buffer) {
        chunk_buffer = malloc(STREAM_CHUNK_SAMPLES * sizeof(int16_t));
    }
    if (!chunk_buffer) {
        ESP_LOGE(TAG, "Failed to allocate chunk buffer");
        return false;
    }

    // Initialize I2S
    if (!init_i2s()) {
        return false;
    }
    i2s_initialized = true;

    // Initialize WakeNet
    if (!init_wakenet()) {
        ESP_LOGW(TAG, "WakeNet init failed - continuing without wake word");
        ESP_LOGW(TAG, "Make sure to flash the model files to 'model' partition");
    } else {
        wakenet_initialized = true;
    }

    ESP_LOGI(TAG, "Audio module initialized");
    return true;
}

void jarvis_audio_deinit(void) {
    if (afe_task_handle) {
        vTaskDelete(afe_task_handle);
        afe_task_handle = NULL;
    }

    if (afe_data && afe_handle) {
        afe_handle->destroy(afe_data);
        afe_data = NULL;
    }
    wakenet_initialized = false;

    // Deinit SR models (this also unmounts SPIFFS)
    deinit_models();

    if (rx_chan) {
        i2s_channel_disable(rx_chan);
        i2s_del_channel(rx_chan);
        rx_chan = NULL;
    }
    i2s_initialized = false;

    if (chunk_buffer) {
        free(chunk_buffer);
        chunk_buffer = NULL;
    }
}

// =============================================================================
// LISTENING CONTROL
// =============================================================================

void jarvis_audio_start_listening(void) {
    listening = true;
    ESP_LOGI(TAG, "Listening started");
}

void jarvis_audio_stop_listening(void) {
    listening = false;
    ESP_LOGI(TAG, "Listening stopped");
}

bool jarvis_audio_is_listening(void) {
    return listening;
}

// =============================================================================
// STREAMING CONTROL
// =============================================================================

void jarvis_audio_start_streaming(void) {
    stream_state = STREAM_STARTING;
    stream_start_time = esp_timer_get_time() / 1000;
    silence_chunk_count = 0;
    voice_chunk_count = 0;
    ESP_LOGI(TAG, "Streaming started (VAD mode)");
}

void jarvis_audio_stop_streaming(void) {
    if (stream_state != STREAM_IDLE) {
        stream_state = STREAM_IDLE;
        ESP_LOGI(TAG, "Streaming stopped: voice chunks=%d", voice_chunk_count);

        if (end_callback) {
            end_callback();
        }
    }
}

bool jarvis_audio_is_streaming(void) {
    return stream_state != STREAM_IDLE;
}

// =============================================================================
// AUDIO PROCESSING
// =============================================================================

static bool check_vad(void) {
    if (!afe_data || !afe_handle) {
        // Fallback: use energy threshold
        return audio_level > 0.15f;
    }

    afe_fetch_result_t* result = afe_handle->fetch(afe_data);
    if (result == NULL) {
        return audio_level > 0.15f;
    }

    return result->vad_state == 1;
}

static bool detect_wake_word(void) {
    if (!afe_data || !afe_handle) return false;

    afe_fetch_result_t* result = afe_handle->fetch(afe_data);
    if (result == NULL) return false;

    if (result->wakeup_state == WAKENET_DETECTED) {
        ESP_LOGI(TAG, "WakeNet detected: word_index=%d, channel=%d",
                 result->wake_word_index, result->trigger_channel_id);
        return true;
    }

    return false;
}

void jarvis_audio_process(void) {
    if (!i2s_initialized) return;

    // Read audio chunk
    size_t bytes_read = 0;
    esp_err_t ret = i2s_channel_read(rx_chan, chunk_buffer, STREAM_CHUNK_SAMPLES * sizeof(int16_t), &bytes_read, 10);
    if (ret != ESP_OK || bytes_read == 0) return;

    size_t samples_read = bytes_read / sizeof(int16_t);

    // Calculate audio level
    audio_level = calculate_rms(chunk_buffer, samples_read);

    // Check VAD
    voice_active = check_vad();

    // === STREAMING MODE ===
    if (stream_state != STREAM_IDLE) {
        int64_t elapsed = (esp_timer_get_time() / 1000) - stream_start_time;

        // Safety timeout
        if (elapsed > STREAM_MAX_DURATION_MS) {
            ESP_LOGW(TAG, "Streaming safety timeout");
            jarvis_audio_stop_streaming();
            return;
        }

        switch (stream_state) {
            case STREAM_STARTING:
                if (voice_active) {
                    voice_chunk_count++;
                }

                if (chunk_callback) {
                    if (!chunk_callback(chunk_buffer, samples_read)) {
                        jarvis_audio_stop_streaming();
                        return;
                    }
                }

                if (elapsed >= STREAM_MIN_AUDIO_MS) {
                    stream_state = STREAM_ACTIVE;
                    ESP_LOGI(TAG, "Streaming active (VAD monitoring)");
                }
                break;

            case STREAM_ACTIVE:
                if (chunk_callback) {
                    if (!chunk_callback(chunk_buffer, samples_read)) {
                        jarvis_audio_stop_streaming();
                        return;
                    }
                }

                if (voice_active) {
                    voice_chunk_count++;
                    silence_chunk_count = 0;
                } else {
                    silence_chunk_count++;

                    if (silence_chunk_count >= VAD_SILENCE_CHUNKS) {
                        ESP_LOGI(TAG, "VAD: silence detected (%d chunks)", silence_chunk_count);
                        stream_state = STREAM_ENDING;
                    }
                }
                break;

            case STREAM_ENDING:
                if (chunk_callback) {
                    chunk_callback(chunk_buffer, samples_read);
                }
                jarvis_audio_stop_streaming();
                break;

            default:
                break;
        }

        return;  // Don't process wake word while streaming
    }

    // === LISTENING MODE ===
    if (listening && wakenet_initialized) {
        if (detect_wake_word()) {
            ESP_LOGI(TAG, ">>> WAKE WORD 'JARVIS' DETECTED! <<<");
            if (wake_callback) {
                wake_callback();
            }
        }
    }
}

// =============================================================================
// GETTERS
// =============================================================================

float jarvis_audio_get_level(void) {
    return audio_level;
}

bool jarvis_audio_is_voice_active(void) {
    return voice_active;
}

void jarvis_audio_set_callbacks(
    wake_word_callback_t wake_cb,
    stream_chunk_callback_t chunk_cb,
    stream_end_callback_t end_cb
) {
    wake_callback = wake_cb;
    chunk_callback = chunk_cb;
    end_callback = end_cb;
}
