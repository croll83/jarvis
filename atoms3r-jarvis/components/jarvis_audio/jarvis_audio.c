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

#include "jarvis_audio.h"
#include "jarvis_codec.h"

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"

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

#define MIC_SAMPLE_RATE         16000

// Raw ring buffer for WebRTC streaming (1 second @ 16kHz mono 16-bit = 32KB)
#define RAW_RINGBUF_SIZE        (16000 * 2)

// ES8311 PGA gain: left at default from es8311_microphone_config() = 0x1A (+30dB)
// No override needed — 30dB provides good sensitivity without clipping at normal distances.

// Model partition name
#define MODEL_PARTITION_LABEL   "model"

// =============================================================================
// STATE
// =============================================================================

static bool listening = false;
static float audio_level = 0.0f;
static bool voice_active = false;

// Wake word callback
static wake_word_callback_t wake_callback = NULL;

// ESP-SR
static esp_afe_sr_iface_t *afe_handle = NULL;
static esp_afe_sr_data_t *afe_data = NULL;
static srmodel_list_t *sr_models = NULL;
static bool wakenet_initialized = false;

// AFE tasks
static TaskHandle_t afe_feed_task_handle = NULL;
static TaskHandle_t afe_detect_task_handle = NULL;

// Raw audio ring buffer for WebRTC
static RingbufHandle_t raw_ringbuf = NULL;
static volatile bool streaming_to_ringbuf = false;

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
// AFE FEED TASK (Dual-Path: raw ring buffer + AFE)
// =============================================================================

static void afe_feed_task(void* arg) {
    // AFE requires exactly get_feed_chunksize() MONO samples per call
    int feed_chunksize = afe_handle->get_feed_chunksize(afe_data);
    ESP_LOGI(TAG, "AFE feed chunksize: %d mono samples", feed_chunksize);

    // Allocate mono buffer (jarvis_codec_read returns mono directly with ALL_LEFT)
    int16_t* mono_buff = heap_caps_malloc(feed_chunksize * sizeof(int16_t),
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!mono_buff) {
        mono_buff = malloc(feed_chunksize * sizeof(int16_t));
    }
    if (!mono_buff) {
        ESP_LOGE(TAG, "Failed to allocate mono buffer");
        vTaskDelete(NULL);
        return;
    }

    int feed_count = 0;
    while (1) {
        if (afe_data && afe_handle) {
            // Read mono audio from codec (legacy I2S with ALL_LEFT = already mono)
            int samples = jarvis_codec_read(mono_buff, feed_chunksize);

            if (samples > 0) {
                feed_count++;
                if (feed_count <= 10 || feed_count % 500 == 0) {
                    float rms = calculate_rms(mono_buff, samples);
                    ESP_LOGI(TAG, "Feed #%d: %d samples, RMS=%.4f",
                             feed_count, samples, rms);
                }

                // PATH 1: Raw ring buffer (when streaming is active)
                if (streaming_to_ringbuf && raw_ringbuf) {
                    xRingbufferSend(raw_ringbuf, mono_buff,
                                    samples * sizeof(int16_t), 0);
                }

                // PATH 2: AFE feed (always — keeps WakeNet running)
                afe_handle->feed(afe_data, mono_buff);
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }

    free(mono_buff);
    vTaskDelete(NULL);
}

// =============================================================================
// AFE DETECT TASK (dedicated fetch — wake word detection)
// =============================================================================

static void afe_detect_task(void* arg) {
    ESP_LOGI(TAG, "AFE detect task started (dedicated fetch loop)");

    while (1) {
        if (!wakenet_initialized || !afe_data || !afe_handle) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        afe_fetch_result_t *result = afe_handle->fetch(afe_data);
        if (result == NULL) {
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        // Update audio level from AFE processed data
        if (result->data && result->data_size > 0) {
            size_t samples = result->data_size / sizeof(int16_t);
            audio_level = calculate_rms(result->data, samples);
        }

        // Update VAD state from AFE
        voice_active = (result->vad_state == AFE_VAD_SPEECH);

        // Wake word detection (only while listening)
        if (listening && result->wakeup_state == WAKENET_DETECTED) {
            ESP_LOGI(TAG, ">>> WAKE WORD 'JARVIS' DETECTED! <<< (level=%.4f)",
                     audio_level);
            ESP_LOGI(TAG, "WakeNet: word_index=%d, channel=%d",
                     result->wake_word_index, result->trigger_channel_id);
            if (wake_callback) {
                wake_callback();
            }
        }
    }

    vTaskDelete(NULL);
}

// =============================================================================
// INITIALIZATION
// =============================================================================

static bool init_wakenet(void) {
    ESP_LOGI(TAG, "Initializing ESP-SR WakeNet...");

    // Initialize model list from SPIFFS partition
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

    // Enable WakeNet — DET_MODE_90 is the most sensitive
    afe_config.wakenet_init = true;
    afe_config.wakenet_model_name = esp_srmodel_filter(sr_models, ESP_WN_PREFIX, NULL);
    afe_config.wakenet_mode = DET_MODE_90;

    // Enable VAD
    afe_config.vad_init = true;

    // Disable AEC (no reference speaker)
    afe_config.aec_init = false;

    // Disable Speech Enhancement — SE suppresses weak/distant speech
    afe_config.se_init = false;

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

    // Create AFE handle
    afe_handle = (esp_afe_sr_iface_t *)&ESP_AFE_SR_HANDLE;
    afe_data = afe_handle->create_from_config(&afe_config);

    if (!afe_data) {
        ESP_LOGE(TAG, "Failed to create AFE - model may not be found in partition");
        deinit_models();
        return false;
    }

    // Start AFE feed task (Core 1, priority 5 — continuous I2S read + AFE feed)
    xTaskCreatePinnedToCore(
        afe_feed_task,
        "afe_feed",
        8192,
        NULL,
        5,
        &afe_feed_task_handle,
        1  // Core 1
    );

    // Start AFE detect task (Core 0, priority 6 — continuous fetch, higher than main_task)
    // This MUST run at higher priority than main_task (5) to ensure fetch() is called
    // continuously even when main_task is blocked on HTTP calls.
    xTaskCreatePinnedToCore(
        afe_detect_task,
        "afe_detect",
        8192,
        NULL,
        6,
        &afe_detect_task_handle,
        0  // Core 0 (same as main_task, but higher priority)
    );

    int fetch_chunksize = afe_handle->get_fetch_chunksize(afe_data);
    int feed_chunksize_log = afe_handle->get_feed_chunksize(afe_data);
    ESP_LOGI(TAG, "WakeNet initialized: model=%s (from SPIFFS), VAD=ON",
             afe_config.wakenet_model_name ? afe_config.wakenet_model_name : "unknown");
    ESP_LOGI(TAG, "AFE: feed_chunksize=%d, fetch_chunksize=%d (%.1fms per fetch)",
             feed_chunksize_log, fetch_chunksize, (float)fetch_chunksize / MIC_SAMPLE_RATE * 1000.0f);
    ESP_LOGI(TAG, "AFE detect task: Core 0, priority 6 (dedicated fetch loop)");
    return true;
}

bool jarvis_audio_init(void) {
    ESP_LOGI(TAG, "Initializing audio module (ESP-SR + ring buffer)...");

    // jarvis_codec_init() must be called before this!
    // We don't init I2C/I2S/ES8311 here — that's jarvis_codec's job.

    // Create raw audio ring buffer in PSRAM
    raw_ringbuf = xRingbufferCreateWithCaps(RAW_RINGBUF_SIZE,
                                             RINGBUF_TYPE_BYTEBUF,
                                             MALLOC_CAP_SPIRAM);
    if (!raw_ringbuf) {
        // Fallback to internal RAM
        raw_ringbuf = xRingbufferCreate(RAW_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    }
    if (!raw_ringbuf) {
        ESP_LOGE(TAG, "Failed to create raw audio ring buffer");
        return false;
    }
    ESP_LOGI(TAG, "Raw audio ring buffer created (%d bytes)", RAW_RINGBUF_SIZE);

    // Initialize WakeNet
    if (!init_wakenet()) {
        ESP_LOGW(TAG, "WakeNet init failed - continuing without wake word");
        ESP_LOGW(TAG, "Make sure to flash the model files to 'model' partition");
    } else {
        wakenet_initialized = true;
    }

    ESP_LOGI(TAG, "Audio module initialized (ESP-SR + dual-path ring buffer)");
    return true;
}

void jarvis_audio_deinit(void) {
    if (afe_detect_task_handle) {
        vTaskDelete(afe_detect_task_handle);
        afe_detect_task_handle = NULL;
    }

    if (afe_feed_task_handle) {
        vTaskDelete(afe_feed_task_handle);
        afe_feed_task_handle = NULL;
    }

    if (afe_data && afe_handle) {
        afe_handle->destroy(afe_data);
        afe_data = NULL;
    }
    wakenet_initialized = false;

    deinit_models();

    if (raw_ringbuf) {
        vRingbufferDelete(raw_ringbuf);
        raw_ringbuf = NULL;
    }

    streaming_to_ringbuf = false;
}

// =============================================================================
// LISTENING CONTROL
// =============================================================================

void jarvis_audio_start_listening(void) {
    listening = true;

    if (afe_handle && afe_data) {
        afe_handle->reset_buffer(afe_data);
        afe_handle->enable_wakenet(afe_data);
    }
    ESP_LOGI(TAG, "Listening started (WakeNet enabled)");
}

void jarvis_audio_stop_listening(void) {
    listening = false;
    if (afe_handle && afe_data) {
        afe_handle->disable_wakenet(afe_data);
    }
    ESP_LOGI(TAG, "Listening stopped (WakeNet disabled)");
}

bool jarvis_audio_is_listening(void) {
    return listening;
}

// =============================================================================
// STREAMING RING BUFFER CONTROL
// =============================================================================

void jarvis_audio_set_streaming(bool enable) {
    if (enable) {
        // Clear any stale data in ring buffer
        if (raw_ringbuf) {
            size_t item_size;
            void *item;
            while ((item = xRingbufferReceive(raw_ringbuf, &item_size, 0)) != NULL) {
                vRingbufferReturnItem(raw_ringbuf, item);
            }
        }

        streaming_to_ringbuf = true;
        ESP_LOGI(TAG, "Streaming enabled (ring buffer active)");
    } else {
        streaming_to_ringbuf = false;
        ESP_LOGI(TAG, "Streaming disabled (ring buffer inactive)");
    }
}

size_t jarvis_audio_read_raw(int16_t *buf, size_t num_samples, uint32_t timeout_ms) {
    if (!raw_ringbuf || !buf || num_samples == 0) return 0;

    size_t bytes_needed = num_samples * sizeof(int16_t);
    size_t item_size = 0;

    void *data = xRingbufferReceiveUpTo(raw_ringbuf, &item_size,
                                         pdMS_TO_TICKS(timeout_ms),
                                         bytes_needed);
    if (data && item_size > 0) {
        memcpy(buf, data, item_size);
        vRingbufferReturnItem(raw_ringbuf, data);
        return item_size / sizeof(int16_t);
    }

    return 0;
}

// =============================================================================
// AUDIO PROCESSING (legacy — now handled by afe_detect_task)
// =============================================================================

void jarvis_audio_process(void) {
    // No-op: wake word detection is now handled by the dedicated afe_detect_task
    // which runs at priority 6 on Core 0, ensuring fetch() is called continuously
    // even when main_task is blocked on HTTP calls.
    // This function is kept for API compatibility.
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

void jarvis_audio_set_wake_callback(wake_word_callback_t cb) {
    wake_callback = cb;
}
