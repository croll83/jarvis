/**
 * =============================================================================
 * JARVIS AtomS3R - WebSocket Audio Module Implementation
 * =============================================================================
 *
 * Streams Opus-encoded audio to JARVIS server via WebSocket.
 *
 * Audio pipeline:
 *   MIC -> jarvis_audio ring buffer -> Opus encoder -> WS binary frame -> server
 *   server -> WS binary frame -> Opus decoder -> jarvis_codec speaker (future)
 *
 * Replaces the WebRTC stack (libpeer + DTLS-SRTP + ICE + SDP signaling)
 * with a simple WebSocket connection. Setup time < 500ms vs 5-10s with WebRTC.
 */

#include "jarvis_ws_audio.h"
#include "jarvis_audio.h"
#include "jarvis_codec.h"

#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_event.h"
#include "esp_websocket_client.h"
#include "cJSON.h"
#include <opus.h>

static const char *TAG = "WS_AUDIO";

// =============================================================================
// CONFIGURATION
// =============================================================================

#define SAMPLE_RATE             16000
#define OPUS_FRAME_SAMPLES      320     // 20ms @ 16kHz
#define BUFFER_SAMPLES          OPUS_FRAME_SAMPLES        // Read exactly one Opus frame (320 samples)
#define OPUS_MAX_PACKET_SIZE    1276
#define OPUS_BITRATE            30000
#define OPUS_COMPLEXITY         0

#define TICK_INTERVAL_MS        15      // Audio read/send interval
#define SESSION_TIMEOUT_MS      30000   // 30 seconds max session
#define WS_CONNECT_TIMEOUT_MS   5000    // 5 seconds to connect + get "ready"
#define WS_TASK_STACK_SIZE      32768   // 32KB in SPIRAM (opus_encode needs deep stack)

// =============================================================================
// INTERNAL STATE MACHINE
// =============================================================================

typedef enum {
    WS_STATE_IDLE = 0,
    WS_STATE_CONNECTING,    // WS TCP+upgrade in progress
    WS_STATE_WAIT_READY,    // WS connected, waiting for {"type":"ready"}
    WS_STATE_STREAMING,     // Got "ready", sending Opus frames
    WS_STATE_DONE,          // Server sent "speech_end" or error
} ws_state_t;

// =============================================================================
// STATE
// =============================================================================

// Opus codec
static OpusEncoder *opus_encoder = NULL;
static OpusDecoder *opus_decoder = NULL;
static int16_t *enc_input_buffer = NULL;
static uint8_t *enc_output_buffer = NULL;
static int16_t *dec_output_buffer = NULL;

// Session state
static volatile ws_state_t ws_state = WS_STATE_IDLE;
static volatile bool session_active = false;
static ws_audio_session_done_callback_t session_done_cb = NULL;
static esp_websocket_client_handle_t ws_client = NULL;

// Task
static TaskHandle_t ws_task_handle = NULL;
static StaticTask_t ws_task_buffer;
static StackType_t *ws_task_stack = NULL;

// URL storage
static char ws_url[384] = {0};

// =============================================================================
// OPUS INIT
// =============================================================================

static bool init_opus_encoder(void) {
    int err;
    opus_encoder = opus_encoder_create(SAMPLE_RATE, 1, OPUS_APPLICATION_VOIP, &err);
    if (err != OPUS_OK || !opus_encoder) {
        ESP_LOGE(TAG, "Opus encoder create failed: %d", err);
        return false;
    }

    opus_encoder_ctl(opus_encoder, OPUS_SET_BITRATE(OPUS_BITRATE));
    opus_encoder_ctl(opus_encoder, OPUS_SET_COMPLEXITY(OPUS_COMPLEXITY));
    opus_encoder_ctl(opus_encoder, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));

    enc_input_buffer = heap_caps_malloc(BUFFER_SAMPLES * sizeof(int16_t),
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    enc_output_buffer = heap_caps_malloc(OPUS_MAX_PACKET_SIZE,
                                          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!enc_input_buffer || !enc_output_buffer) {
        ESP_LOGE(TAG, "Opus encoder buffer alloc failed");
        return false;
    }

    ESP_LOGI(TAG, "Opus encoder: %dHz mono, bitrate=%d, complexity=%d",
             SAMPLE_RATE, OPUS_BITRATE, OPUS_COMPLEXITY);
    return true;
}

static bool init_opus_decoder(void) {
    int err;
    opus_decoder = opus_decoder_create(SAMPLE_RATE, 1, &err);
    if (err != OPUS_OK || !opus_decoder) {
        ESP_LOGE(TAG, "Opus decoder create failed: %d", err);
        return false;
    }

    dec_output_buffer = heap_caps_malloc(BUFFER_SAMPLES * sizeof(int16_t),
                                          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!dec_output_buffer) {
        ESP_LOGE(TAG, "Opus decoder buffer alloc failed");
        return false;
    }

    ESP_LOGI(TAG, "Opus decoder: %dHz mono", SAMPLE_RATE);
    return true;
}

// =============================================================================
// WEBSOCKET EVENT HANDLER
// =============================================================================

static void ws_event_handler(void *handler_args, esp_event_base_t base,
                              int32_t event_id, void *event_data) {
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;

    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI(TAG, "WebSocket connected — starting audio immediately");
            // Skip waiting for "ready" JSON — start streaming right away.
            // The server sends {"type":"ready"} but esp_websocket_client
            // may not deliver DATA events reliably right after handshake.
            ws_state = WS_STATE_STREAMING;
            break;

        case WEBSOCKET_EVENT_DATA:
            ESP_LOGI(TAG, "WS DATA: op=0x%02x len=%d payload_len=%d payload_offset=%d",
                     data->op_code, data->data_len, data->payload_len, data->payload_offset);
            if (data->op_code == 0x01) {
                // Text frame: parse JSON control message
                if (data->data_ptr && data->data_len > 0) {
                    // Null-terminate for cJSON
                    char *json_buf = malloc(data->data_len + 1);
                    if (json_buf) {
                        memcpy(json_buf, data->data_ptr, data->data_len);
                        json_buf[data->data_len] = '\0';

                        cJSON *msg = cJSON_Parse(json_buf);
                        if (msg) {
                            const char *type = cJSON_GetStringValue(
                                cJSON_GetObjectItem(msg, "type"));
                            if (type) {
                                if (strcmp(type, "ready") == 0) {
                                    cJSON *sid = cJSON_GetObjectItem(msg, "session_id");
                                    ESP_LOGI(TAG, "Server ready confirmed (session_id=%s)",
                                             sid && cJSON_IsString(sid) ? sid->valuestring : "?");
                                    // State already STREAMING from CONNECTED event
                                } else if (strcmp(type, "speech_end") == 0) {
                                    ESP_LOGI(TAG, "Server detected speech end");
                                    ws_state = WS_STATE_DONE;
                                } else if (strcmp(type, "error") == 0) {
                                    cJSON *err_msg = cJSON_GetObjectItem(msg, "msg");
                                    ESP_LOGE(TAG, "Server error: %s",
                                             err_msg && cJSON_IsString(err_msg) ?
                                             err_msg->valuestring : "unknown");
                                    ws_state = WS_STATE_DONE;
                                }
                            }
                            cJSON_Delete(msg);
                        }
                        free(json_buf);
                    }
                }
            } else if (data->op_code == 0x02) {
                // Binary frame: future TTS Opus playback from server
                if (opus_decoder && dec_output_buffer && data->data_ptr && data->data_len > 0) {
                    int decoded_samples = opus_decode(opus_decoder,
                        (const unsigned char *)data->data_ptr, data->data_len,
                        dec_output_buffer, BUFFER_SAMPLES, 0);
                    if (decoded_samples > 0) {
                        jarvis_codec_write(dec_output_buffer, decoded_samples);
                    }
                }
            }
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGI(TAG, "WebSocket disconnected");
            if (ws_state != WS_STATE_DONE) {
                // Unexpected disconnect — treat as done
                ws_state = WS_STATE_DONE;
            }
            session_active = false;
            break;

        case WEBSOCKET_EVENT_ERROR:
            ESP_LOGE(TAG, "WebSocket error");
            ws_state = WS_STATE_DONE;
            session_active = false;
            break;

        default:
            break;
    }
}

// =============================================================================
// WS AUDIO SESSION TASK
// =============================================================================

static void ws_audio_task(void *arg) {
    ESP_LOGI(TAG, "WS audio task started: %s", ws_url);

    bool success = false;
    uint32_t packets_sent = 0;
    uint32_t read_failures = 0;
    uint32_t encode_failures = 0;
    uint32_t send_failures = 0;

    // --- 1. Create and start WebSocket client ---
    esp_websocket_client_config_t ws_cfg = {
        .uri = ws_url,
        .buffer_size = 2048,
        .task_stack = 4096,     // Internal WS client task stack
        .task_prio = 5,         // Below our audio task (6)
    };

    ws_client = esp_websocket_client_init(&ws_cfg);
    if (!ws_client) {
        ESP_LOGE(TAG, "Failed to init WebSocket client");
        goto done;
    }

    esp_websocket_register_events(ws_client, WEBSOCKET_EVENT_ANY,
                                   ws_event_handler, NULL);

    ws_state = WS_STATE_CONNECTING;
    esp_err_t err = esp_websocket_client_start(ws_client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start WebSocket client: %s", esp_err_to_name(err));
        goto done;
    }

    // --- 2. Wait for WS_STATE_STREAMING (connected + got "ready") ---
    int64_t connect_start = esp_timer_get_time() / 1000;
    while (session_active && ws_state < WS_STATE_STREAMING) {
        int64_t elapsed = (esp_timer_get_time() / 1000) - connect_start;
        if (elapsed > WS_CONNECT_TIMEOUT_MS) {
            ESP_LOGE(TAG, "Timeout waiting for WS ready (%lldms)", elapsed);
            goto done;
        }
        if (ws_state == WS_STATE_DONE) {
            // Error or disconnect during connect
            goto done;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    if (!session_active || ws_state != WS_STATE_STREAMING) {
        goto done;
    }

    int64_t ready_time = (esp_timer_get_time() / 1000) - connect_start;
    ESP_LOGI(TAG, "Server ready in %lldms, starting audio stream", ready_time);

    // --- 3. Drain stale audio from ring buffer (wake sound echo) ---
    {
        size_t drained = 0;
        size_t drain_samples;
        while ((drain_samples = jarvis_audio_read_raw(enc_input_buffer,
                    BUFFER_SAMPLES, 0)) > 0) {
            drained += drain_samples;
        }
        if (drained > 0) {
            ESP_LOGI(TAG, "Drained %zu stale samples from ring buffer", drained);
        }
    }

    // --- 4. Audio send loop ---
    int64_t session_start = esp_timer_get_time() / 1000;

    while (session_active && ws_state == WS_STATE_STREAMING) {
        // Check session timeout
        int64_t now = esp_timer_get_time() / 1000;
        if ((now - session_start) > SESSION_TIMEOUT_MS) {
            ESP_LOGW(TAG, "Session timeout (%dms)", SESSION_TIMEOUT_MS);
            break;
        }

        // Read raw mono audio from ring buffer
        size_t samples_read = jarvis_audio_read_raw(enc_input_buffer,
                                                      BUFFER_SAMPLES,
                                                      TICK_INTERVAL_MS);
        if (samples_read >= (size_t)OPUS_FRAME_SAMPLES) {
            // Encode to Opus
            int encoded_size = opus_encode(opus_encoder,
                                            enc_input_buffer,
                                            OPUS_FRAME_SAMPLES,
                                            enc_output_buffer,
                                            OPUS_MAX_PACKET_SIZE);
            if (encoded_size > 0) {
                // Send as binary WebSocket frame
                int sent = esp_websocket_client_send_bin(ws_client,
                    (const char *)enc_output_buffer, encoded_size,
                    pdMS_TO_TICKS(1000));
                if (sent >= 0) {
                    packets_sent++;
                } else {
                    send_failures++;
                    if (send_failures == 1 || send_failures % 100 == 0) {
                        ESP_LOGW(TAG, "WS send failed (total=%lu)", (unsigned long)send_failures);
                    }
                }
            } else {
                encode_failures++;
            }
        } else {
            read_failures++;
        }

        // Log stats every 500 iterations (~7.5 seconds)
        if ((packets_sent + read_failures) % 500 == 0 && (packets_sent + read_failures) > 0) {
            ESP_LOGI(TAG, "Audio stats: sent=%lu read_fail=%lu enc_fail=%lu send_fail=%lu",
                     (unsigned long)packets_sent, (unsigned long)read_failures,
                     (unsigned long)encode_failures, (unsigned long)send_failures);
        }

        vTaskDelay(pdMS_TO_TICKS(TICK_INTERVAL_MS));
    }

    // Check if we got a clean speech_end
    success = (ws_state == WS_STATE_DONE);

    ESP_LOGI(TAG, "Audio loop ended: sent=%lu, state=%d, success=%d",
             (unsigned long)packets_sent, (int)ws_state, success);

done:
    // --- 4. Cleanup ---
    if (ws_client) {
        // Give a moment for any pending data
        vTaskDelay(pdMS_TO_TICKS(50));

        esp_websocket_client_stop(ws_client);
        esp_websocket_client_destroy(ws_client);
        ws_client = NULL;
    }

    ws_state = WS_STATE_IDLE;
    session_active = false;

    ESP_LOGI(TAG, "WS audio session ended, heap free: %lu",
             (unsigned long)esp_get_free_heap_size());

    // Notify caller
    if (session_done_cb) {
        session_done_cb(success);
    }

    ws_task_handle = NULL;
    vTaskDelete(NULL);
}

// =============================================================================
// PUBLIC API
// =============================================================================

bool jarvis_ws_audio_init(void) {
    ESP_LOGI(TAG, "Initializing WS Audio module (Opus only)...");

    if (!init_opus_encoder()) {
        ESP_LOGE(TAG, "Opus encoder init failed");
        return false;
    }

    if (!init_opus_decoder()) {
        ESP_LOGE(TAG, "Opus decoder init failed");
        return false;
    }

    ESP_LOGI(TAG, "WS Audio module initialized");
    return true;
}

bool jarvis_ws_audio_start_session(const char *url,
                                    ws_audio_session_done_callback_t done_cb) {
    if (session_active) {
        ESP_LOGW(TAG, "Session already active");
        return false;
    }

    if (!url || !url[0]) {
        ESP_LOGE(TAG, "Empty URL");
        return false;
    }

    // Save parameters
    strncpy(ws_url, url, sizeof(ws_url) - 1);
    ws_url[sizeof(ws_url) - 1] = '\0';
    session_done_cb = done_cb;
    session_active = true;
    ws_state = WS_STATE_IDLE;

    // Allocate SPIRAM stack (16KB — no DTLS/ECDSA overhead)
    if (!ws_task_stack) {
        ws_task_stack = heap_caps_malloc(WS_TASK_STACK_SIZE * sizeof(StackType_t),
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!ws_task_stack) {
            ESP_LOGE(TAG, "Failed to allocate SPIRAM stack for WS task");
            session_active = false;
            return false;
        }
    }

    ws_task_handle = xTaskCreateStaticPinnedToCore(
        ws_audio_task,
        "ws_audio",
        WS_TASK_STACK_SIZE,
        NULL,
        6,              // Priority 6 (same as old webrtc_session_task)
        ws_task_stack,
        &ws_task_buffer,
        0               // Core 0
    );

    if (!ws_task_handle) {
        ESP_LOGE(TAG, "Failed to create WS audio task");
        session_active = false;
        return false;
    }

    ESP_LOGI(TAG, "WS audio session starting: %s", ws_url);
    return true;
}

void jarvis_ws_audio_stop_session(void) {
    if (!session_active) return;

    ESP_LOGI(TAG, "Stopping WS audio session");
    session_active = false;

    // Wait for task to finish
    if (ws_task_handle) {
        int timeout = 100;  // 1s max
        while (ws_task_handle && timeout-- > 0) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        if (ws_task_handle) {
            ESP_LOGW(TAG, "WS task did not finish in time");
        }
    }
}

bool jarvis_ws_audio_is_active(void) {
    return session_active;
}
