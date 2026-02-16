/**
 * =============================================================================
 * JARVIS AtomS3R - Audio Module (ESP-SR WakeNet + Dual-Path)
 * =============================================================================
 *
 * Manages:
 * - Wake word detection with ESP-SR WakeNet (model "jarvis")
 * - Dual-path audio feed: raw ring buffer (for WS audio) + AFE (for WakeNet)
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

// Raw ring buffer for WS audio streaming (1 second @ 16kHz mono 16-bit = 32KB)
#define RAW_RINGBUF_SIZE        (16000 * 2)

// ES8311 PGA gain: left at default from es8311_microphone_config() = 0x1A (+30dB)
// No override needed — 30dB provides good sensitivity without clipping at normal distances.

// Model partition name
#define MODEL_PARTITION_LABEL   "model"

// WakeNet detection threshold override.
// Default for wn9_jarvis_tts DET_MODE_90 is 0.627 — too strict for Italian pronunciation.
// Lower value = more false positives but better sensitivity to pronunciation variations.
// Range: 0.5 ~ 0.9999 (per ESP-SR docs). Start at 0.5 (most sensitive allowed).
#define WAKENET_DET_THRESHOLD   0.5f

// =============================================================================
// AFE INTERNAL WakeNet ACCESS (layout-independent pointer scan)
// =============================================================================
// The AFE doesn't expose set_det_threshold publicly. Instead of mirroring the
// internal struct (fragile, caused crash), we scan the AFE data blob for the
// known WakeNet interface pointer obtained from esp_wn_handle_from_name().
// The model_data pointer is at the next word position in the struct.
// This works regardless of struct layout or padding changes.

/**
 * @brief Find WakeNet interface and model_data pointers inside AFE data blob.
 *
 * Scans first 256 bytes (64 words) of afe_data looking for a pointer that
 * matches the known WakeNet interface. The next word is model_data.
 *
 * @param afe_data       Opaque AFE data pointer
 * @param known_wn_iface The WakeNet interface pointer from esp_wn_handle_from_name()
 * @param out_wn         Output: WakeNet interface pointer (or NULL)
 * @param out_model_data Output: model_iface_data_t pointer (or NULL)
 * @return true if found
 */
static bool find_wakenet_in_afe(esp_afe_sr_data_t *afe_data_ptr,
                                const esp_wn_iface_t *known_wn_iface,
                                esp_wn_iface_t **out_wn,
                                model_iface_data_t **out_model_data)
{
    *out_wn = NULL;
    *out_model_data = NULL;

    // Scan first 64 words (256 bytes) of AFE data blob
    // On ESP32-S3 (Xtensa, 32-bit), all struct fields are word-aligned
    uint32_t *words = (uint32_t *)afe_data_ptr;
    const int scan_words = 64;

    for (int i = 0; i < scan_words - 1; i++) {
        if (words[i] == (uint32_t)known_wn_iface) {
            ESP_LOGI(TAG, "WakeNet iface found at word[%d]=0x%08lx", i, (unsigned long)words[i]);

            // Dump next 8 words for debugging
            for (int d = 1; d <= 8 && (i + d) < scan_words; d++) {
                ESP_LOGI(TAG, "  word[%d]=0x%08lx", i + d, (unsigned long)words[i + d]);
            }

            // model_data may be at offset +1 or further (struct padding differs between
            // .ref source and compiled binary). Search next 4 words for a valid heap pointer.
            for (int offset = 1; offset <= 4 && (i + offset) < scan_words; offset++) {
                uint32_t md_addr = words[i + offset];
                // Valid heap pointer: non-zero, word-aligned, in DRAM or PSRAM range
                if (md_addr != 0 && (md_addr & 0x3) == 0 && md_addr >= 0x3C000000) {
                    *out_wn = (esp_wn_iface_t *)words[i];
                    *out_model_data = (model_iface_data_t *)md_addr;
                    ESP_LOGI(TAG, "model_data at word[%d]=0x%08lx (offset +%d from wakenet)",
                             i + offset, (unsigned long)md_addr, offset);
                    return true;
                }
            }

            ESP_LOGW(TAG, "WakeNet at word[%d] but no valid model_data in next 4 words", i);
        }
    }

    return false;
}

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

// Raw audio ring buffer for WS audio streaming
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

                // PATH 1: Raw ring buffer with digital gain (when streaming is active)
                // Apply 8x gain ONLY for WS streaming (STT needs louder signal)
                // WakeNet gets the original signal (its AGC handles low levels)
                if (streaming_to_ringbuf && raw_ringbuf) {
                    int16_t amplified[320];  // max feed_chunksize typically 256 or 160
                    int copy_len = (samples <= 320) ? samples : 320;
                    for (int j = 0; j < copy_len; j++) {
                        int32_t v = (int32_t)mono_buff[j] * 4;  // 4x = ~12dB (hardware at +64dB, extra boost for speaker ID)
                        if (v > 32767) v = 32767;
                        else if (v < -32768) v = -32768;
                        amplified[j] = (int16_t)v;
                    }
                    xRingbufferSend(raw_ringbuf, amplified,
                                    copy_len * sizeof(int16_t), 0);
                }

                // PATH 2: AFE feed (always — keeps WakeNet running, NO gain applied)
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

    // DISABLE Speech Enhancement for single-mic setup
    // BSS (Blind Source Separation) requires 2+ mics. With 1 mic, SE can
    // attenuate weak speech signals, making WakeNet detection worse.
    afe_config.se_init = false;

    // Low-cost mode for AtomS3R
    afe_config.afe_mode = SR_MODE_LOW_COST;

    // Use PSRAM
    afe_config.memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;

    // Core and priority
    afe_config.afe_perferred_core = 0;
    afe_config.afe_perferred_priority = 5;
    afe_config.afe_ringbuf_size = 50;

    // Linear gain: amplify weak mic signal BEFORE AFE processing.
    // ES8311 mic output is very weak (RMS ~0.007 at silence, ~0.02-0.04 speech).
    // 2x gain brings speech to ~0.04-0.08 while keeping noise manageable.
    // Higher values (4.0+) amplify background noise too much, reducing detection range.
    afe_config.afe_linear_gain = 2.0;

    // Most aggressive peak AGC (-3dB) for MultiNet path
    afe_config.agc_mode = AFE_MN_PEAK_AGC_MODE_3;

    // Create AFE handle
    afe_handle = (esp_afe_sr_iface_t *)&ESP_AFE_SR_HANDLE;
    afe_data = afe_handle->create_from_config(&afe_config);

    if (!afe_data) {
        ESP_LOGE(TAG, "Failed to create AFE - model may not be found in partition");
        deinit_models();
        return false;
    }

    // =========================================================================
    // LOWER WakeNet DETECTION THRESHOLD
    // =========================================================================
    // Scan AFE data blob to find WakeNet interface and model_data pointers.
    // This allows calling set_det_threshold() to make detection more sensitive
    // for Italian pronunciation of "Jarvis" (model trained for American English).
    {
        // Get the known WakeNet interface pointer for comparison
        const char *wn_model_name = afe_config.wakenet_model_name;
        const esp_wn_iface_t *known_wn = esp_wn_handle_from_name(wn_model_name);

        if (known_wn) {
            esp_wn_iface_t *wn = NULL;
            model_iface_data_t *md = NULL;

            if (find_wakenet_in_afe(afe_data, known_wn, &wn, &md)) {
                // Log current threshold before changing
                float old_threshold = wn->get_det_threshold(md, 1);
                ESP_LOGI(TAG, "WakeNet threshold BEFORE: %.4f (word_index=1)", old_threshold);

                // Set new lower threshold
                int ret = wn->set_det_threshold(md, WAKENET_DET_THRESHOLD, 1);
                float new_threshold = wn->get_det_threshold(md, 1);
                ESP_LOGI(TAG, "WakeNet threshold AFTER: %.4f (ret=%d, target=%.2f)",
                         new_threshold, ret, WAKENET_DET_THRESHOLD);

                // Log model info
                int word_num = wn->get_word_num(md);
                for (int i = 1; i <= word_num; i++) {
                    char *name = wn->get_word_name(md, i);
                    float thresh = wn->get_det_threshold(md, i);
                    ESP_LOGI(TAG, "  Word %d: '%s' threshold=%.4f",
                             i, name ? name : "?", thresh);
                }
            } else {
                ESP_LOGW(TAG, "Could not find WakeNet in AFE data (scanned 64 words)");
            }
        } else {
            ESP_LOGW(TAG, "esp_wn_handle_from_name('%s') returned NULL", wn_model_name);
        }
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
    ESP_LOGI(TAG, "WakeNet initialized: model=%s (from SPIFFS), VAD=ON, SE=OFF",
             afe_config.wakenet_model_name ? afe_config.wakenet_model_name : "unknown");
    ESP_LOGI(TAG, "AFE: feed=%d, fetch=%d (%.1fms), linear_gain=%.1f, AGC=MODE_3(-3dB)",
             feed_chunksize_log, fetch_chunksize, (float)fetch_chunksize / MIC_SAMPLE_RATE * 1000.0f,
             afe_config.afe_linear_gain);
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
