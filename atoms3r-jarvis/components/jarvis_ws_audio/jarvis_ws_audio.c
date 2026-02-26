/**
 * =============================================================================
 * JARVIS AtomS3R - Persistent WebSocket Audio Module Implementation
 * =============================================================================
 *
 * Persistent WebSocket connection to JARVIS server.
 * Handles both control (JSON) and audio (Opus binary) on a single channel.
 *
 * Audio pipeline (during active session):
 *   MIC -> jarvis_audio ring buffer -> Opus encoder -> WS binary frame -> server
 *   server -> WS binary frame -> Opus decoder -> jarvis_codec speaker (future TTS)
 *
 * Control channel (always active when connected):
 *   Server -> trigger_listen, ping, speech_end, ready, welcome
 *   Device -> hello, audio_start, audio_end, state, pong
 *
 * Connection management:
 *   - Auto-reconnect with exponential backoff (1s -> 2s -> 4s -> ... -> 30s max)
 *   - Ping keepalive every 30s during idle
 *   - Reset backoff on successful connection
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
#define BUFFER_SAMPLES          OPUS_FRAME_SAMPLES
#define OPUS_MAX_PACKET_SIZE    1276
#define OPUS_BITRATE_VAL        30000
#define OPUS_COMPLEXITY_VAL     0

#define TICK_INTERVAL_MS        15      // Audio read/send interval
#define SESSION_TIMEOUT_MS      30000   // 30 seconds max audio session
#define WS_TASK_STACK_SIZE      32768   // 32KB in SPIRAM

// Reconnect backoff
#define RECONNECT_MIN_MS        1000
#define RECONNECT_MAX_MS        30000

// Keepalive
#define PING_INTERVAL_MS        30000   // Ping every 30s if idle
#define CONNECT_TIMEOUT_MS      10000   // 10s to establish connection

// =============================================================================
// CONNECTION STATE MACHINE
// =============================================================================

typedef enum {
    CONN_STATE_DISCONNECTED = 0,  // Not connected, waiting for reconnect
    CONN_STATE_CONNECTING,        // TCP+WS handshake in progress
    CONN_STATE_CONNECTED,         // Connected, idle (no audio session)
    CONN_STATE_AUDIO_STARTING,    // Sent audio_start, waiting for ready
    CONN_STATE_STREAMING,         // Audio session active, streaming Opus
} conn_state_t;

// =============================================================================
// STATE
// =============================================================================

// Opus codec
static OpusEncoder *opus_encoder = NULL;
static OpusDecoder *opus_decoder = NULL;
static int16_t *enc_input_buffer = NULL;
static uint8_t *enc_output_buffer = NULL;
static int16_t *dec_output_buffer = NULL;

// Connection state
static volatile conn_state_t conn_state = CONN_STATE_DISCONNECTED;
static volatile bool task_should_run = false;
static volatile bool audio_session_requested = false;  // Flag: main_task requests audio start
static volatile bool audio_session_active = false;      // Audio is streaming
volatile bool live_session_active = false;               // Live session mode (no session timeout)

// Callbacks
static ws_audio_session_done_callback_t session_done_cb = NULL;
static ws_trigger_listen_callback_t trigger_listen_cb = NULL;
static ws_tts_done_callback_t tts_done_cb = NULL;
static ws_config_update_callback_t config_update_cb = NULL;
static ws_wake_detected_callback_t wake_detected_cb = NULL;

// WebSocket client
static esp_websocket_client_handle_t ws_client = NULL;

// Task
static TaskHandle_t ws_task_handle = NULL;
static StackType_t *ws_task_stack = NULL;
static StaticTask_t ws_task_buffer;

// URL storage
static char ws_url[384] = {0};
static char device_id_buf[16] = {0};

// Reconnect state
static int reconnect_delay_ms = RECONNECT_MIN_MS;

// Session timing
static int64_t audio_session_start_ms = 0;

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

    opus_encoder_ctl(opus_encoder, OPUS_SET_BITRATE(OPUS_BITRATE_VAL));
    opus_encoder_ctl(opus_encoder, OPUS_SET_COMPLEXITY(OPUS_COMPLEXITY_VAL));
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
             SAMPLE_RATE, OPUS_BITRATE_VAL, OPUS_COMPLEXITY_VAL);
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
// JSON HELPERS
// =============================================================================

static bool send_json(const char *type, const char *extra_key, const char *extra_val) {
    if (!ws_client || conn_state < CONN_STATE_CONNECTED) return false;

    cJSON *json = cJSON_CreateObject();
    if (!json) return false;

    cJSON_AddStringToObject(json, "type", type);
    if (extra_key && extra_val) {
        cJSON_AddStringToObject(json, extra_key, extra_val);
    }

    char *str = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (!str) return false;

    int ret = esp_websocket_client_send_text(ws_client, str, strlen(str), pdMS_TO_TICKS(1000));
    free(str);

    return (ret >= 0);
}

static bool send_json_hello(const char *device_id, const char *fw_version) {
    if (!ws_client || conn_state < CONN_STATE_CONNECTED) return false;

    cJSON *json = cJSON_CreateObject();
    if (!json) return false;

    cJSON_AddStringToObject(json, "type", "hello");
    cJSON_AddStringToObject(json, "device_id", device_id);
    cJSON_AddStringToObject(json, "fw", fw_version);

    char *str = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (!str) return false;

    int ret = esp_websocket_client_send_text(ws_client, str, strlen(str), pdMS_TO_TICKS(1000));
    free(str);

    return (ret >= 0);
}

// =============================================================================
// WEBSOCKET EVENT HANDLER
// =============================================================================

static void ws_event_handler(void *handler_args, esp_event_base_t base,
                              int32_t event_id, void *event_data) {
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;

    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI(TAG, "WebSocket connected to server");
            conn_state = CONN_STATE_CONNECTED;
            reconnect_delay_ms = RECONNECT_MIN_MS;  // Reset backoff on success
            break;

        case WEBSOCKET_EVENT_DATA:
            if (data->op_code == 0x01) {
                // Text frame: parse JSON control message
                if (data->data_ptr && data->data_len > 0) {
                    char *json_buf = malloc(data->data_len + 1);
                    if (json_buf) {
                        memcpy(json_buf, data->data_ptr, data->data_len);
                        json_buf[data->data_len] = '\0';

                        cJSON *msg = cJSON_Parse(json_buf);
                        if (msg) {
                            const char *type = cJSON_GetStringValue(
                                cJSON_GetObjectItem(msg, "type"));
                            if (type) {
                                if (strcmp(type, "welcome") == 0) {
                                    ESP_LOGI(TAG, "Server welcome received");

                                } else if (strcmp(type, "ready") == 0) {
                                    cJSON *sid = cJSON_GetObjectItem(msg, "session_id");
                                    ESP_LOGI(TAG, "Audio session ready (session_id=%s)",
                                             sid && cJSON_IsString(sid) ? sid->valuestring : "?");
                                    if (conn_state == CONN_STATE_AUDIO_STARTING) {
                                        conn_state = CONN_STATE_STREAMING;
                                    }

                                } else if (strcmp(type, "speech_end") == 0) {
                                    ESP_LOGI(TAG, "Server detected speech end");
                                    if (conn_state == CONN_STATE_STREAMING) {
                                        // Signal audio loop to stop
                                        audio_session_active = false;
                                    }

                                } else if (strcmp(type, "trigger_listen") == 0) {
                                    cJSON *silent_obj = cJSON_GetObjectItem(msg, "silent");
                                    bool silent = true;
                                    if (silent_obj && cJSON_IsBool(silent_obj)) {
                                        silent = cJSON_IsTrue(silent_obj);
                                    }
                                    ESP_LOGI(TAG, "Server trigger_listen (silent=%d)", silent);
                                    if (trigger_listen_cb) {
                                        trigger_listen_cb(silent);
                                    }

                                } else if (strcmp(type, "tts_done") == 0) {
                                    ESP_LOGI(TAG, "Server: TTS playback complete");
                                    if (tts_done_cb) {
                                        tts_done_cb();
                                    }

                                } else if (strcmp(type, "config_update") == 0) {
                                    ESP_LOGI(TAG, "Server: config_update received");
                                    if (config_update_cb) {
                                        cJSON *sens = cJSON_GetObjectItem(msg, "wake_word_sensitivity");
                                        float sensitivity = -1.0f;
                                        if (sens && cJSON_IsNumber(sens)) {
                                            sensitivity = (float)sens->valuedouble;
                                        }
                                        config_update_cb(sensitivity);
                                    }

                                } else if (strcmp(type, "wake_detected") == 0) {
                                    ESP_LOGI(TAG, "Server: wake_detected (server-side wake word)");
                                    if (wake_detected_cb) {
                                        wake_detected_cb();
                                    }

                                } else if (strcmp(type, "live_session_start") == 0) {
                                    ESP_LOGI(TAG, "Server: live_session_start");
                                    live_session_active = true;

                                } else if (strcmp(type, "live_session_end") == 0) {
                                    ESP_LOGI(TAG, "Server: live_session_end");
                                    live_session_active = false;

                                } else if (strcmp(type, "ping") == 0) {
                                    ESP_LOGD(TAG, "Server ping — sending pong");
                                    send_json("pong", NULL, NULL);

                                } else if (strcmp(type, "error") == 0) {
                                    cJSON *err_msg = cJSON_GetObjectItem(msg, "msg");
                                    ESP_LOGE(TAG, "Server error: %s",
                                             err_msg && cJSON_IsString(err_msg) ?
                                             err_msg->valuestring : "unknown");
                                    if (conn_state == CONN_STATE_STREAMING ||
                                        conn_state == CONN_STATE_AUDIO_STARTING) {
                                        audio_session_active = false;
                                    }
                                }
                            }
                            cJSON_Delete(msg);
                        }
                        free(json_buf);
                    }
                }
            } else if (data->op_code == 0x02) {
                // Binary frame: TTS Opus playback from server
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
            conn_state = CONN_STATE_DISCONNECTED;
            audio_session_active = false;
            break;

        case WEBSOCKET_EVENT_ERROR:
            ESP_LOGE(TAG, "WebSocket error");
            conn_state = CONN_STATE_DISCONNECTED;
            audio_session_active = false;
            break;

        default:
            break;
    }
}

// =============================================================================
// AUDIO STREAMING LOOP (runs within the persistent task when session active)
// =============================================================================

static bool run_audio_session(void) {
    ESP_LOGI(TAG, "Starting audio session...");

    // Send audio_start to server
    conn_state = CONN_STATE_AUDIO_STARTING;
    audio_session_active = true;
    audio_session_start_ms = esp_timer_get_time() / 1000;

    if (!send_json("audio_start", NULL, NULL)) {
        ESP_LOGE(TAG, "Failed to send audio_start");
        audio_session_active = false;
        conn_state = CONN_STATE_CONNECTED;
        return false;
    }

    // Wait for "ready" from server (transitions to STREAMING via event handler)
    int64_t wait_start = esp_timer_get_time() / 1000;
    while (audio_session_active && conn_state == CONN_STATE_AUDIO_STARTING) {
        int64_t elapsed = (esp_timer_get_time() / 1000) - wait_start;
        if (elapsed > 5000) {  // 5s timeout for ready
            ESP_LOGE(TAG, "Timeout waiting for audio ready");
            audio_session_active = false;
            conn_state = CONN_STATE_CONNECTED;
            return false;
        }
        if (conn_state == CONN_STATE_DISCONNECTED) {
            audio_session_active = false;
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    if (!audio_session_active || conn_state != CONN_STATE_STREAMING) {
        conn_state = (conn_state >= CONN_STATE_CONNECTED) ? CONN_STATE_CONNECTED : conn_state;
        return false;
    }

    int64_t ready_time = (esp_timer_get_time() / 1000) - wait_start;
    ESP_LOGI(TAG, "Server ready in %lldms, starting audio stream", ready_time);

    // Drain stale audio from ring buffer (wake sound echo)
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

    // Audio send loop
    uint32_t packets_sent = 0;
    uint32_t read_failures = 0;
    uint32_t send_failures = 0;
    int64_t session_start = esp_timer_get_time() / 1000;
    bool success = false;

    while (audio_session_active && conn_state == CONN_STATE_STREAMING) {
        // Check session timeout
        int64_t now = esp_timer_get_time() / 1000;
        if ((now - session_start) > SESSION_TIMEOUT_MS) {
            ESP_LOGW(TAG, "Audio session timeout (%dms)", SESSION_TIMEOUT_MS);
            break;
        }

        // Check connection still alive
        if (conn_state != CONN_STATE_STREAMING) {
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
            }
        } else {
            read_failures++;
        }

        // Log stats every 500 iterations (~7.5 seconds)
        if ((packets_sent + read_failures) % 500 == 0 && (packets_sent + read_failures) > 0) {
            ESP_LOGI(TAG, "Audio stats: sent=%lu read_fail=%lu send_fail=%lu",
                     (unsigned long)packets_sent, (unsigned long)read_failures,
                     (unsigned long)send_failures);
        }

        vTaskDelay(pdMS_TO_TICKS(TICK_INTERVAL_MS));
    }

    // Check if we got a clean speech_end (audio_session_active was set false by event handler)
    success = (!audio_session_active && conn_state >= CONN_STATE_CONNECTED);

    ESP_LOGI(TAG, "Audio session ended: sent=%lu, state=%d, success=%d",
             (unsigned long)packets_sent, (int)conn_state, success);

    // Clean up audio session state
    audio_session_active = false;
    if (conn_state == CONN_STATE_STREAMING) {
        conn_state = CONN_STATE_CONNECTED;  // Back to idle
    }

    return success;
}

// =============================================================================
// PERSISTENT WS TASK (runs for the lifetime of the device)
// =============================================================================

static void ws_persistent_task(void *arg) {
    ESP_LOGI(TAG, "Persistent WS task started");

    extern const char* jarvis_network_get_api_token(void);

    while (task_should_run) {
        // === Phase 1: Connect ===
        ESP_LOGI(TAG, "Connecting to %s ...", ws_url);
        conn_state = CONN_STATE_CONNECTING;

        esp_websocket_client_config_t ws_cfg = {
            .uri = ws_url,
            .buffer_size = 2048,
            .task_stack = 6144,  // 6KB: event handler runs opus_decode + codec_write
            .task_prio = 5,
        };

        ws_client = esp_websocket_client_init(&ws_cfg);
        if (!ws_client) {
            ESP_LOGE(TAG, "Failed to init WebSocket client");
            conn_state = CONN_STATE_DISCONNECTED;
            goto reconnect;
        }

        esp_websocket_register_events(ws_client, WEBSOCKET_EVENT_ANY,
                                       ws_event_handler, NULL);

        esp_err_t err = esp_websocket_client_start(ws_client);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to start WebSocket client: %s", esp_err_to_name(err));
            esp_websocket_client_destroy(ws_client);
            ws_client = NULL;
            conn_state = CONN_STATE_DISCONNECTED;
            goto reconnect;
        }

        // Wait for connection (event handler sets CONN_STATE_CONNECTED)
        {
            int64_t connect_start = esp_timer_get_time() / 1000;
            while (task_should_run && conn_state == CONN_STATE_CONNECTING) {
                int64_t elapsed = (esp_timer_get_time() / 1000) - connect_start;
                if (elapsed > CONNECT_TIMEOUT_MS) {
                    ESP_LOGE(TAG, "Connection timeout (%dms)", CONNECT_TIMEOUT_MS);
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(50));
            }
        }

        if (conn_state != CONN_STATE_CONNECTED) {
            ESP_LOGW(TAG, "Connection failed (state=%d)", conn_state);
            esp_websocket_client_stop(ws_client);
            esp_websocket_client_destroy(ws_client);
            ws_client = NULL;
            conn_state = CONN_STATE_DISCONNECTED;
            goto reconnect;
        }

        // === Phase 2: Connected! Send hello ===
        ESP_LOGI(TAG, "Connected! Sending hello...");
        send_json_hello(device_id_buf, "5.0.0-ws");

        // === Phase 3: Main loop (idle + audio sessions) ===
        int64_t last_ping_time = esp_timer_get_time() / 1000;

        while (task_should_run && conn_state >= CONN_STATE_CONNECTED) {
            int64_t now = esp_timer_get_time() / 1000;

            // Continuous Opus stream to server (for server-side wake word detection)
            // Only when idle — during active sessions, run_audio_session handles streaming
            #ifndef USE_LOCAL_WAKEWORD
            if (conn_state == CONN_STATE_CONNECTED && !audio_session_requested) {
                size_t samples = jarvis_audio_read_raw(enc_input_buffer,
                                                        BUFFER_SAMPLES,
                                                        TICK_INTERVAL_MS);
                if (samples >= (size_t)OPUS_FRAME_SAMPLES) {
                    int enc_size = opus_encode(opus_encoder,
                                                enc_input_buffer,
                                                OPUS_FRAME_SAMPLES,
                                                enc_output_buffer,
                                                OPUS_MAX_PACKET_SIZE);
                    if (enc_size > 0) {
                        esp_websocket_client_send_bin(ws_client,
                            (const char *)enc_output_buffer, enc_size,
                            pdMS_TO_TICKS(100));
                    }
                }
            }
            #endif

            // Check if audio session was requested by main_task
            if (audio_session_requested && conn_state == CONN_STATE_CONNECTED) {
                audio_session_requested = false;

                bool session_success = run_audio_session();

                // Notify main_task via callback
                if (session_done_cb) {
                    session_done_cb(session_success);
                }

                last_ping_time = esp_timer_get_time() / 1000;  // Reset ping timer
                continue;
            }

            // Keepalive ping
            if ((now - last_ping_time) > PING_INTERVAL_MS) {
                if (!send_json("pong", NULL, NULL)) {
                    // Send failed — connection likely dead
                    ESP_LOGW(TAG, "Keepalive send failed — disconnecting");
                    break;
                }
                last_ping_time = now;
            }

            #ifndef USE_LOCAL_WAKEWORD
            vTaskDelay(pdMS_TO_TICKS(5));   // 5ms tick for continuous Opus stream
            #else
            vTaskDelay(pdMS_TO_TICKS(50));  // 50ms idle loop (local wake word)
            #endif
        }

        // === Phase 4: Cleanup connection ===
        ESP_LOGI(TAG, "Connection loop ended (state=%d, should_run=%d)",
                 conn_state, task_should_run);

        if (ws_client) {
            vTaskDelay(pdMS_TO_TICKS(50));
            esp_websocket_client_stop(ws_client);
            esp_websocket_client_destroy(ws_client);
            ws_client = NULL;
        }
        conn_state = CONN_STATE_DISCONNECTED;

reconnect:
        if (!task_should_run) break;

        // Exponential backoff
        ESP_LOGI(TAG, "Reconnecting in %dms...", reconnect_delay_ms);
        vTaskDelay(pdMS_TO_TICKS(reconnect_delay_ms));

        // Double backoff (capped at max)
        reconnect_delay_ms *= 2;
        if (reconnect_delay_ms > RECONNECT_MAX_MS) {
            reconnect_delay_ms = RECONNECT_MAX_MS;
        }
    }

    ESP_LOGI(TAG, "Persistent WS task exiting");

    if (ws_client) {
        esp_websocket_client_stop(ws_client);
        esp_websocket_client_destroy(ws_client);
        ws_client = NULL;
    }

    ws_task_handle = NULL;
    vTaskDelete(NULL);
}

// =============================================================================
// PUBLIC API
// =============================================================================

bool jarvis_ws_audio_init(void) {
    ESP_LOGI(TAG, "Initializing WS Audio module (Opus)...");

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

bool jarvis_ws_audio_connect(const char *device_id,
                              ws_audio_session_done_callback_t done_cb,
                              ws_trigger_listen_callback_t trigger_cb) {
    if (task_should_run) {
        ESP_LOGW(TAG, "Already connecting/connected");
        return false;
    }

    if (!device_id || !device_id[0]) {
        ESP_LOGE(TAG, "Empty device_id");
        return false;
    }

    // Save parameters
    strncpy(device_id_buf, device_id, sizeof(device_id_buf) - 1);
    device_id_buf[sizeof(device_id_buf) - 1] = '\0';
    session_done_cb = done_cb;
    trigger_listen_cb = trigger_cb;

    // Build URL
    extern void jarvis_network_get_ws_audio_url(const char *, char *, size_t);
    jarvis_network_get_ws_audio_url(device_id, ws_url, sizeof(ws_url));
    ESP_LOGI(TAG, "WS URL: %s", ws_url);

    // Allocate SPIRAM stack (once)
    if (!ws_task_stack) {
        ws_task_stack = heap_caps_malloc(WS_TASK_STACK_SIZE * sizeof(StackType_t),
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!ws_task_stack) {
            ESP_LOGE(TAG, "Failed to allocate SPIRAM stack for WS task");
            return false;
        }
    }

    // Start persistent task
    task_should_run = true;
    reconnect_delay_ms = RECONNECT_MIN_MS;

    ws_task_handle = xTaskCreateStaticPinnedToCore(
        ws_persistent_task,
        "ws_persist",
        WS_TASK_STACK_SIZE,
        NULL,
        6,              // Priority 6
        ws_task_stack,
        &ws_task_buffer,
        0               // Core 0
    );

    if (!ws_task_handle) {
        ESP_LOGE(TAG, "Failed to create persistent WS task");
        task_should_run = false;
        return false;
    }

    ESP_LOGI(TAG, "Persistent WS task started for device %s", device_id);
    return true;
}

bool jarvis_ws_audio_start_session(void) {
    if (conn_state != CONN_STATE_CONNECTED) {
        ESP_LOGW(TAG, "Cannot start session: not connected (state=%d)", conn_state);
        return false;
    }
    if (audio_session_active) {
        ESP_LOGW(TAG, "Audio session already active");
        return false;
    }

    // Set flag for persistent task to pick up
    audio_session_requested = true;
    ESP_LOGI(TAG, "Audio session requested");
    return true;
}

void jarvis_ws_audio_stop_session(void) {
    if (!audio_session_active) return;

    ESP_LOGI(TAG, "Stopping audio session");
    audio_session_active = false;

    // Wait a bit for the streaming loop to exit
    int timeout = 50;  // 500ms max
    while (audio_session_active && timeout-- > 0) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

bool jarvis_ws_audio_is_active(void) {
    return audio_session_active;
}

bool jarvis_ws_audio_is_connected(void) {
    return (conn_state >= CONN_STATE_CONNECTED);
}

bool jarvis_ws_audio_is_live_session(void) {
    return live_session_active;
}

void jarvis_ws_audio_send_state(const char *state_str) {
    if (!state_str) return;
    send_json("state", "state", state_str);
}

void jarvis_ws_audio_send_speaker_stop(void) {
    ESP_LOGI(TAG, "Sending speaker_stop to server");
    send_json("speaker_stop", NULL, NULL);
}

void jarvis_ws_audio_disconnect(void) {
    ESP_LOGI(TAG, "Disconnecting...");
    task_should_run = false;
    audio_session_active = false;

    // Wait for task to finish
    if (ws_task_handle) {
        int timeout = 200;  // 2s max
        while (ws_task_handle && timeout-- > 0) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        if (ws_task_handle) {
            ESP_LOGW(TAG, "WS task did not finish in time");
        }
    }
}

void jarvis_ws_audio_set_tts_done_callback(ws_tts_done_callback_t cb) {
    tts_done_cb = cb;
}

void jarvis_ws_audio_set_config_update_callback(ws_config_update_callback_t cb) {
    config_update_cb = cb;
}

void jarvis_ws_audio_set_wake_detected_callback(ws_wake_detected_callback_t cb) {
    wake_detected_cb = cb;
}
