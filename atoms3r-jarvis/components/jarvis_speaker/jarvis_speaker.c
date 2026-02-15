/**
 * =============================================================================
 * JARVIS AtomS3R - Speaker Module Implementation
 * =============================================================================
 *
 * Output audio via jarvis_codec (shared I2S TX bus).
 * Hardware init (I2S, ES8311, PI4IOE5V6408 amp) is handled by jarvis_codec.
 * This module only handles PCM playback logic.
 *
 * The wake sound (harmonic_rise) is embedded as const array in flash.
 * Playback is non-blocking (FreeRTOS task).
 */

#include "jarvis_speaker.h"
#include "jarvis_codec.h"
#include "wake_sound_data.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

static const char *TAG = "SPEAKER";

// =============================================================================
// STATE
// =============================================================================

static bool speaker_initialized = false;
static volatile bool playing = false;
static volatile bool stop_requested = false;

static TaskHandle_t playback_task_handle = NULL;

// =============================================================================
// INITIALIZATION
// =============================================================================

bool jarvis_speaker_init(void) {
    ESP_LOGI(TAG, "Initializing speaker (using jarvis_codec for I2S TX)...");

    // jarvis_codec handles all hardware: I2S, ES8311, PI4IOE5V6408 amp.
    // Nothing to init here — just verify codec is available.
    speaker_initialized = true;
    ESP_LOGI(TAG, "Speaker initialized");
    return true;
}

void jarvis_speaker_deinit(void) {
    jarvis_speaker_stop();
    speaker_initialized = false;
    ESP_LOGI(TAG, "Speaker deinitialized");
}

// =============================================================================
// PLAYBACK
// =============================================================================

void jarvis_speaker_play_pcm(const int16_t* pcm_data, size_t num_samples) {
    if (!speaker_initialized) {
        ESP_LOGW(TAG, "Speaker not initialized");
        return;
    }

    playing = true;
    stop_requested = false;

    // Write in blocks via jarvis_codec_write (handles mono→stereo interleave)
    const size_t BLOCK_SIZE = 256;  // mono samples per block
    size_t sample_offset = 0;

    while (sample_offset < num_samples && !stop_requested) {
        size_t remaining = num_samples - sample_offset;
        size_t block = remaining < BLOCK_SIZE ? remaining : BLOCK_SIZE;

        int written = jarvis_codec_write(&pcm_data[sample_offset], block);
        if (written < 0) {
            ESP_LOGE(TAG, "Codec write error");
            break;
        }

        sample_offset += block;
    }

    // Flush: write silence to push DMA buffers
    int16_t silence[256] = {0};
    jarvis_codec_write(silence, 256);

    playing = false;
}

// FreeRTOS task for non-blocking playback
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

    BaseType_t ret = xTaskCreatePinnedToCore(
        wake_sound_task,
        "wake_sound",
        4096,
        NULL,
        4,
        &playback_task_handle,
        1  // Core 1
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

    if (playback_task_handle) {
        int timeout = 50;  // 500ms max
        while (playing && timeout-- > 0) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}

bool jarvis_speaker_wait_done(uint32_t timeout_ms) {
    uint32_t elapsed = 0;
    const uint32_t step = 10;

    while (playing && elapsed < timeout_ms) {
        vTaskDelay(pdMS_TO_TICKS(step));
        elapsed += step;
    }

    return !playing;
}
