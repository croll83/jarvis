/**
 * =============================================================================
 * JARVIS AtomS3R - WebRTC Module Implementation
 * =============================================================================
 *
 * WebRTC peer connection with JARVIS orchestrator server.
 * Uses libpeer for WebRTC, Opus for audio codec.
 *
 * Audio pipeline:
 *   MIC → jarvis_audio ring buffer → Opus encoder → PeerConnection → server
 *   server → PeerConnection → Opus decoder → jarvis_codec speaker → speaker
 *
 * Signaling:
 *   SDP offer → HTTP POST to /webrtc/offer → SDP answer
 */

#include "jarvis_webrtc.h"
#include "jarvis_audio.h"
#include "jarvis_codec.h"

#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "cJSON.h"

#include "peer.h"
#include "peer_connection.h"
#include <opus.h>

static const char *TAG = "WEBRTC";

// =============================================================================
// CONFIGURATION
// =============================================================================

#define SAMPLE_RATE             16000
#define OPUS_FRAME_SAMPLES      320     // 20ms @ 16kHz
#define BUFFER_SAMPLES          (OPUS_FRAME_SAMPLES * 2)  // 640 samples for read
#define OPUS_MAX_PACKET_SIZE    1276
#define OPUS_BITRATE            30000
#define OPUS_COMPLEXITY         0

#define TICK_INTERVAL_MS        15
#define SESSION_TIMEOUT_MS      120000  // 2 minutes
#define MAX_SDP_BUFFER_SIZE     4096

// =============================================================================
// STATE
// =============================================================================

// Opus codec
static OpusEncoder *opus_encoder = NULL;
static OpusDecoder *opus_decoder = NULL;
static int16_t *enc_input_buffer = NULL;
static uint8_t *enc_output_buffer = NULL;
static int16_t *dec_output_buffer = NULL;

// PeerConnection
static PeerConnection *pc = NULL;
static volatile bool session_active = false;
static webrtc_session_done_callback_t session_done_cb = NULL;

// Tasks
static TaskHandle_t webrtc_task_handle = NULL;
static TaskHandle_t audio_send_task_handle = NULL;
static StaticTask_t audio_send_task_buffer;
static StackType_t *audio_send_task_stack = NULL;

// Signaling info (copied at session start)
static char sig_url[256] = {0};
static char sig_device_id[32] = {0};
static char sig_api_token[128] = {0};

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
// SDP SIGNALING (HTTP POST)
// =============================================================================

static int sdp_response_offset = 0;

static esp_err_t http_event_handler(esp_http_client_event_t *evt) {
    switch (evt->event_id) {
        case HTTP_EVENT_ON_DATA:
            if (evt->user_data) {
                int copy_len = evt->data_len;
                if (sdp_response_offset + copy_len > MAX_SDP_BUFFER_SIZE - 1) {
                    copy_len = MAX_SDP_BUFFER_SIZE - 1 - sdp_response_offset;
                }
                if (copy_len > 0) {
                    memcpy((char *)evt->user_data + sdp_response_offset,
                           evt->data, copy_len);
                    sdp_response_offset += copy_len;
                }
            }
            break;
        case HTTP_EVENT_ON_FINISH:
            sdp_response_offset = 0;
            break;
        case HTTP_EVENT_DISCONNECTED:
            sdp_response_offset = 0;
            break;
        default:
            break;
    }
    return ESP_OK;
}

static bool send_sdp_offer(const char *offer, char *answer, size_t answer_size) {
    ESP_LOGI(TAG, "Sending SDP offer to %s", sig_url);

    // Build JSON body: {"sdp": "<offer>", "type": "offer"}
    cJSON *json_body = cJSON_CreateObject();
    if (!json_body) {
        ESP_LOGE(TAG, "Failed to create JSON body");
        return false;
    }
    cJSON_AddStringToObject(json_body, "sdp", offer);
    cJSON_AddStringToObject(json_body, "type", "offer");

    char *json_str = cJSON_PrintUnformatted(json_body);
    cJSON_Delete(json_body);

    if (!json_str) {
        ESP_LOGE(TAG, "Failed to serialize JSON body");
        return false;
    }

    ESP_LOGI(TAG, "JSON body length: %d bytes", (int)strlen(json_str));

    // Response buffer — accumulates raw HTTP response body
    // answer buffer is reused for raw response, then we extract SDP from JSON
    char *response_buf = heap_caps_malloc(MAX_SDP_BUFFER_SIZE,
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!response_buf) {
        ESP_LOGE(TAG, "Failed to alloc response buffer");
        free(json_str);
        return false;
    }

    esp_http_client_config_t config = {0};
    config.url = sig_url;
    config.event_handler = http_event_handler;
    config.user_data = response_buf;
    config.timeout_ms = 15000;
    config.buffer_size = 4096;
    config.buffer_size_tx = 4096;

    memset(response_buf, 0, MAX_SDP_BUFFER_SIZE);
    memset(answer, 0, answer_size);
    sdp_response_offset = 0;

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        ESP_LOGE(TAG, "HTTP client init failed");
        free(json_str);
        free(response_buf);
        return false;
    }

    esp_http_client_set_method(client, HTTP_METHOD_POST);
    esp_http_client_set_header(client, "Content-Type", "application/json");

    // Set device ID header (uppercase as server expects)
    if (sig_device_id[0]) {
        esp_http_client_set_header(client, "X-Device-ID", sig_device_id);
    }

    // Set Authorization header if token available
    if (sig_api_token[0]) {
        char auth_header[160];
        snprintf(auth_header, sizeof(auth_header), "Bearer %s", sig_api_token);
        esp_http_client_set_header(client, "Authorization", auth_header);
    }

    esp_http_client_set_post_field(client, json_str, strlen(json_str));

    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    free(json_str);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "HTTP request failed: %s", esp_err_to_name(err));
        free(response_buf);
        return false;
    }

    if (status != 200 && status != 201) {
        ESP_LOGE(TAG, "SDP signaling failed: HTTP %d, body: %.200s", status, response_buf);
        free(response_buf);
        return false;
    }

    ESP_LOGI(TAG, "Server response received (%d bytes)", (int)strlen(response_buf));

    // Parse JSON response: {"sdp": "...", "type": "answer", "session_id": "..."}
    cJSON *resp_json = cJSON_Parse(response_buf);
    free(response_buf);

    if (!resp_json) {
        ESP_LOGE(TAG, "Failed to parse server JSON response");
        return false;
    }

    cJSON *sdp_item = cJSON_GetObjectItem(resp_json, "sdp");
    if (!sdp_item || !cJSON_IsString(sdp_item) || !sdp_item->valuestring) {
        ESP_LOGE(TAG, "No 'sdp' field in server response");
        cJSON_Delete(resp_json);
        return false;
    }

    // Copy SDP answer to output buffer
    strncpy(answer, sdp_item->valuestring, answer_size - 1);
    answer[answer_size - 1] = '\0';

    cJSON *session_item = cJSON_GetObjectItem(resp_json, "session_id");
    if (session_item && cJSON_IsString(session_item)) {
        ESP_LOGI(TAG, "WebRTC session_id: %s", session_item->valuestring);
    }

    cJSON_Delete(resp_json);

    ESP_LOGI(TAG, "SDP answer extracted (%d bytes)", (int)strlen(answer));
    return true;
}

// =============================================================================
// AUDIO RECEIVE (server → speaker)
// =============================================================================

static void on_audio_track(uint8_t *data, size_t size, void *userdata) {
    if (!opus_decoder || !dec_output_buffer || !session_active) return;

    int decoded_samples = opus_decode(opus_decoder, data, size,
                                       dec_output_buffer, BUFFER_SAMPLES, 0);
    if (decoded_samples > 0) {
        jarvis_codec_write(dec_output_buffer, decoded_samples);
    }
}

// =============================================================================
// AUDIO SEND TASK (mic → server)
// =============================================================================

static void audio_send_task(void *arg) {
    ESP_LOGI(TAG, "Audio send task started");

    while (session_active) {
        // Read raw mono audio from ring buffer
        size_t samples_read = jarvis_audio_read_raw(enc_input_buffer,
                                                      BUFFER_SAMPLES,
                                                      TICK_INTERVAL_MS);
        if (samples_read >= (size_t)OPUS_FRAME_SAMPLES) {
            // Encode: input is BUFFER_SAMPLES, but opus_encode frame_size is half
            int encoded_size = opus_encode(opus_encoder,
                                            enc_input_buffer,
                                            OPUS_FRAME_SAMPLES,
                                            enc_output_buffer,
                                            OPUS_MAX_PACKET_SIZE);
            if (encoded_size > 0 && pc) {
                peer_connection_send_audio(pc, enc_output_buffer, encoded_size);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(TICK_INTERVAL_MS));
    }

    ESP_LOGI(TAG, "Audio send task ended");
    audio_send_task_handle = NULL;
    vTaskDelete(NULL);
}

// =============================================================================
// PEER CONNECTION CALLBACKS
// =============================================================================

static void on_connection_state_change(PeerConnectionState state, void *user_data) {
    ESP_LOGI(TAG, "PeerConnectionState: %s",
             peer_connection_state_to_string(state));

    if (state == PEER_CONNECTION_CONNECTED) {
        ESP_LOGI(TAG, "WebRTC connected! Starting audio send task");

        // Create audio send task with SPIRAM stack
        if (!audio_send_task_stack) {
            audio_send_task_stack = heap_caps_malloc(
                40000 * sizeof(StackType_t), MALLOC_CAP_SPIRAM);
        }
        if (audio_send_task_stack) {
            audio_send_task_handle = xTaskCreateStaticPinnedToCore(
                audio_send_task, "webrtc_audio", 40000,
                NULL, 7, audio_send_task_stack, &audio_send_task_buffer, 0);
        } else {
            ESP_LOGE(TAG, "Failed to allocate audio send task stack");
        }
    } else if (state == PEER_CONNECTION_DISCONNECTED ||
               state == PEER_CONNECTION_CLOSED) {
        ESP_LOGI(TAG, "WebRTC disconnected/closed");
        session_active = false;
        // done_callback will be called from the main webrtc loop task
    } else if (state == PEER_CONNECTION_FAILED) {
        ESP_LOGE(TAG, "WebRTC connection FAILED");
        session_active = false;
    }
}

static void on_ice_candidate(char *sdp_offer, void *user_data) {
    char *sdp_answer = heap_caps_malloc(MAX_SDP_BUFFER_SIZE,
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!sdp_answer) {
        ESP_LOGE(TAG, "Failed to alloc SDP answer buffer");
        session_active = false;
        return;
    }

    bool ok = send_sdp_offer(sdp_offer, sdp_answer, MAX_SDP_BUFFER_SIZE);
    if (ok && sdp_answer[0]) {
        peer_connection_set_remote_description(pc, sdp_answer, SDP_TYPE_ANSWER);
    } else {
        ESP_LOGE(TAG, "SDP signaling failed");
        session_active = false;
    }

    free(sdp_answer);
}

// =============================================================================
// WEBRTC SESSION TASK
// =============================================================================

static void webrtc_session_task(void *arg) {
    ESP_LOGI(TAG, "WebRTC session task started");

    // Create PeerConnection
    PeerConfiguration config = {
        .ice_servers = {},
        .audio_codec = CODEC_OPUS,
        .video_codec = CODEC_NONE,
        .datachannel = DATA_CHANNEL_NONE,
        .onaudiotrack = on_audio_track,
        .onvideotrack = NULL,
        .on_request_keyframe = NULL,
        .user_data = NULL,
    };

    pc = peer_connection_create(&config);
    if (!pc) {
        ESP_LOGE(TAG, "Failed to create PeerConnection");
        session_active = false;
        if (session_done_cb) session_done_cb(false);
        webrtc_task_handle = NULL;
        vTaskDelete(NULL);
        return;
    }

    peer_connection_oniceconnectionstatechange(pc, on_connection_state_change);
    peer_connection_onicecandidate(pc, on_ice_candidate);
    peer_connection_create_offer(pc);

    int64_t start_time = esp_timer_get_time() / 1000;
    bool timed_out = false;

    // Main loop: drive PeerConnection
    while (session_active) {
        peer_connection_loop(pc);
        vTaskDelay(pdMS_TO_TICKS(TICK_INTERVAL_MS));

        // Check timeout
        int64_t elapsed = (esp_timer_get_time() / 1000) - start_time;
        if (elapsed > SESSION_TIMEOUT_MS) {
            ESP_LOGW(TAG, "WebRTC session timeout (%lldms)", elapsed);
            timed_out = true;
            session_active = false;
        }
    }

    ESP_LOGI(TAG, "WebRTC session ended (timeout=%d)", timed_out);

    // Wait for audio send task to finish
    if (audio_send_task_handle) {
        int wait = 50;
        while (audio_send_task_handle && wait-- > 0) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }

    // Cleanup PeerConnection
    if (pc) {
        peer_connection_destroy(pc);
        pc = NULL;
    }

    // Notify caller
    bool success = !timed_out;
    if (session_done_cb) {
        session_done_cb(success);
    }

    webrtc_task_handle = NULL;
    vTaskDelete(NULL);
}

// =============================================================================
// PUBLIC API
// =============================================================================

bool jarvis_webrtc_init(void) {
    ESP_LOGI(TAG, "Initializing WebRTC module (Opus encoder/decoder)...");

    if (!init_opus_encoder()) {
        ESP_LOGE(TAG, "Opus encoder init failed");
        return false;
    }

    if (!init_opus_decoder()) {
        ESP_LOGE(TAG, "Opus decoder init failed");
        return false;
    }

    ESP_LOGI(TAG, "WebRTC module initialized");
    return true;
}

bool jarvis_webrtc_start_session(const char *signaling_url,
                                  const char *device_id,
                                  const char *api_token,
                                  webrtc_session_done_callback_t done_cb) {
    if (session_active) {
        ESP_LOGW(TAG, "Session already active");
        return false;
    }

    // Save signaling params
    strncpy(sig_url, signaling_url, sizeof(sig_url) - 1);
    strncpy(sig_device_id, device_id ? device_id : "", sizeof(sig_device_id) - 1);
    strncpy(sig_api_token, api_token ? api_token : "", sizeof(sig_api_token) - 1);
    session_done_cb = done_cb;
    session_active = true;

    // Create WebRTC session task on Core 0
    // Stack must be large enough for DTLS handshake + ECDSA key gen (~20KB)
    BaseType_t ret = xTaskCreatePinnedToCore(
        webrtc_session_task,
        "webrtc_sess",
        32768,
        NULL,
        6,
        &webrtc_task_handle,
        0  // Core 0
    );

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create WebRTC session task");
        session_active = false;
        return false;
    }

    ESP_LOGI(TAG, "WebRTC session starting (url=%s, device=%s)",
             sig_url, sig_device_id);
    return true;
}

void jarvis_webrtc_stop_session(void) {
    if (!session_active) return;

    ESP_LOGI(TAG, "Stopping WebRTC session");
    session_active = false;

    // Wait for task to finish
    if (webrtc_task_handle) {
        int timeout = 100;  // 1s max
        while (webrtc_task_handle && timeout-- > 0) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}

bool jarvis_webrtc_is_active(void) {
    return session_active;
}
