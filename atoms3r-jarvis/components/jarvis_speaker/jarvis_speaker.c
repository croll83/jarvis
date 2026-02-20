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
#include <math.h>
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

// =============================================================================
// REJECT BUZZ (short low-frequency tone for wake reject feedback)
// =============================================================================

#define BUZZ_FREQ_HZ    400
#define BUZZ_DURATION_MS 80
#define BUZZ_SAMPLE_RATE 16000
#define BUZZ_SAMPLES     (BUZZ_SAMPLE_RATE * BUZZ_DURATION_MS / 1000)  // 1280
#define BUZZ_AMPLITUDE   4000  // quiet buzz, not startling

static void buzz_task(void* arg) {
    // Generate 400Hz sine wave, 80ms, low amplitude
    int16_t buzz_buf[BUZZ_SAMPLES];
    for (int i = 0; i < BUZZ_SAMPLES; i++) {
        float t = (float)i / BUZZ_SAMPLE_RATE;
        // Apply quick fade-in/fade-out (first/last 10ms) to avoid click
        float env = 1.0f;
        int fade_samples = BUZZ_SAMPLE_RATE / 100;  // 10ms = 160 samples
        if (i < fade_samples) {
            env = (float)i / fade_samples;
        } else if (i > BUZZ_SAMPLES - fade_samples) {
            env = (float)(BUZZ_SAMPLES - i) / fade_samples;
        }
        buzz_buf[i] = (int16_t)(sinf(2.0f * 3.14159265f * BUZZ_FREQ_HZ * t) * BUZZ_AMPLITUDE * env);
    }

    jarvis_speaker_play_pcm(buzz_buf, BUZZ_SAMPLES);
    playback_task_handle = NULL;
    vTaskDelete(NULL);
}

void jarvis_speaker_play_buzz(void) {
    if (!speaker_initialized) return;
    if (playing) return;

    xTaskCreatePinnedToCore(
        buzz_task, "buzz", 4096 + 2560,  // extra stack for 1280 int16 on stack
        NULL, 4, &playback_task_handle, 1
    );
}

// =============================================================================
// LISTENING BEEP (short high-pitched beep for multi-turn "speak now" feedback)
// =============================================================================

#define BEEP_FREQ_HZ     880
#define BEEP_DURATION_MS  100
#define BEEP_SAMPLE_RATE  16000
#define BEEP_SAMPLES      (BEEP_SAMPLE_RATE * BEEP_DURATION_MS / 1000)  // 1600
#define BEEP_AMPLITUDE    6000  // clear but not loud

static void listening_beep_task(void* arg) {
    // Generate 880Hz sine wave, 100ms, moderate amplitude
    int16_t beep_buf[BEEP_SAMPLES];
    for (int i = 0; i < BEEP_SAMPLES; i++) {
        float t = (float)i / BEEP_SAMPLE_RATE;
        // Fade-in/fade-out (first/last 10ms) to avoid click
        float env = 1.0f;
        int fade_samples = BEEP_SAMPLE_RATE / 100;  // 10ms = 160 samples
        if (i < fade_samples) {
            env = (float)i / fade_samples;
        } else if (i > BEEP_SAMPLES - fade_samples) {
            env = (float)(BEEP_SAMPLES - i) / fade_samples;
        }
        beep_buf[i] = (int16_t)(sinf(2.0f * 3.14159265f * BEEP_FREQ_HZ * t) * BEEP_AMPLITUDE * env);
    }

    jarvis_speaker_play_pcm(beep_buf, BEEP_SAMPLES);
    playback_task_handle = NULL;
    vTaskDelete(NULL);
}

void jarvis_speaker_play_listening_beep(void) {
    if (!speaker_initialized) return;
    if (playing) return;

    xTaskCreatePinnedToCore(
        listening_beep_task, "beep", 4096 + 3200,  // extra stack for 1600 int16 on stack
        NULL, 4, &playback_task_handle, 1
    );
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
