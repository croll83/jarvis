/**
 * =============================================================================
 * JARVIS AtomS3R - Network Module Implementation (ESP-IDF)
 * =============================================================================
 */

#include "jarvis_network.h"

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "cJSON.h"

static const char *TAG = "NETWORK";

// =============================================================================
// CONFIGURATION (should match jarvis_config.h)
// =============================================================================

// WiFi credentials (from menuconfig or fallback defaults)
#ifdef CONFIG_WIFI_SSID
#define WIFI_SSID           CONFIG_WIFI_SSID
#else
#define WIFI_SSID           "YOUR_WIFI_SSID"
#endif

#ifdef CONFIG_WIFI_PASSWORD
#define WIFI_PASSWORD       CONFIG_WIFI_PASSWORD
#else
#define WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"
#endif

#ifdef CONFIG_JARVIS_SERVER_HOST
#define JARVIS_SERVER_HOST  CONFIG_JARVIS_SERVER_HOST
#else
#define JARVIS_SERVER_HOST  "jarvis.local"
#endif

#ifdef CONFIG_JARVIS_SERVER_PORT
#define JARVIS_SERVER_PORT  CONFIG_JARVIS_SERVER_PORT
#else
#define JARVIS_SERVER_PORT  5000
#endif

// Device room/id (legacy, kept for backwards compatibility)
#ifdef CONFIG_DEVICE_ROOM
#define DEVICE_ROOM         CONFIG_DEVICE_ROOM
#else
#define DEVICE_ROOM         "salotto"
#endif

#ifdef CONFIG_DEVICE_ID
#define DEVICE_ID           CONFIG_DEVICE_ID
#else
#define DEVICE_ID           "atoms3r_salotto"
#endif

#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1

// Streaming buffer
#define ACCUMULATE_SAMPLES  1600  // ~100ms @ 16kHz

// =============================================================================
// STATE
// =============================================================================

static EventGroupHandle_t wifi_event_group = NULL;
static bool wifi_connected = false;
static int retry_count = 0;
#define MAX_RETRY 10

// Callbacks
static server_response_callback_t response_callback = NULL;
static busy_state_callback_t busy_callback = NULL;
static config_update_callback_t config_callback = NULL;

// Streaming state
typedef enum {
    STREAM_NET_IDLE,
    STREAM_NET_CONNECTING,
    STREAM_NET_SENDING,
    STREAM_NET_FINISHING,
    STREAM_NET_ERROR
} stream_net_state_t;

static stream_net_state_t stream_state = STREAM_NET_IDLE;
static int stream_socket = -1;
static char current_room[32] = "";
static size_t total_bytes_sent = 0;
static int64_t stream_start_time = 0;

// Accumulate buffer
static int16_t* accumulate_buffer = NULL;
static size_t accumulated_samples = 0;

// =============================================================================
// WIFI EVENT HANDLER
// =============================================================================

static void wifi_event_handler(void* arg, esp_event_base_t event_base,
                               int32_t event_id, void* event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_connected = false;
        if (retry_count < MAX_RETRY) {
            esp_wifi_connect();
            retry_count++;
            ESP_LOGI(TAG, "Retry connection (%d/%d)", retry_count, MAX_RETRY);
        } else {
            xEventGroupSetBits(wifi_event_group, WIFI_FAIL_BIT);
            ESP_LOGE(TAG, "WiFi connection failed");
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        retry_count = 0;
        wifi_connected = true;
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

// =============================================================================
// INITIALIZATION
// =============================================================================

bool jarvis_network_init(void) {
    ESP_LOGI(TAG, "Initializing network...");

    // Create event group
    wifi_event_group = xEventGroupCreate();

    // Initialize TCP/IP stack
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    // WiFi configuration
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    // Register event handlers
    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                    &wifi_event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                    &wifi_event_handler, NULL, &instance_got_ip));

    // Set WiFi config
    wifi_config_t wifi_config = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    strncpy((char*)wifi_config.sta.ssid, WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strncpy((char*)wifi_config.sta.password, WIFI_PASSWORD, sizeof(wifi_config.sta.password));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Connecting to WiFi: %s", WIFI_SSID);

    // Wait for connection
    EventBits_t bits = xEventGroupWaitBits(wifi_event_group,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(30000));

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi connected!");

        // Allocate accumulate buffer
        accumulate_buffer = heap_caps_malloc(ACCUMULATE_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!accumulate_buffer) {
            accumulate_buffer = malloc(ACCUMULATE_SAMPLES * sizeof(int16_t));
        }

        return true;
    }

    ESP_LOGE(TAG, "WiFi connection failed");
    return false;
}

void jarvis_network_deinit(void) {
    esp_wifi_disconnect();
    esp_wifi_stop();
    esp_wifi_deinit();

    if (accumulate_buffer) {
        free(accumulate_buffer);
        accumulate_buffer = NULL;
    }
}

bool jarvis_network_is_connected(void) {
    return wifi_connected;
}

// =============================================================================
// DEVICE ID & CONFIGURATION
// =============================================================================

bool jarvis_network_get_device_id(char* out_device_id) {
    if (!out_device_id) return false;

    uint8_t mac[6];
    esp_err_t err = esp_wifi_get_mac(WIFI_IF_STA, mac);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to get MAC: %s", esp_err_to_name(err));
        return false;
    }

    snprintf(out_device_id, 13, "%02X%02X%02X%02X%02X%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    ESP_LOGI(TAG, "Device ID (MAC): %s", out_device_id);
    return true;
}

bool jarvis_network_fetch_config(const char* device_id, device_config_t* out_config) {
    if (!wifi_connected || !device_id || !out_config) return false;

    char url[192];
    snprintf(url, sizeof(url), "http://%s:%d/device_config?device_id=%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, device_id);

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return false;

    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Fetch config failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return false;
    }

    int status = esp_http_client_get_status_code(client);
    if (status != 200) {
        ESP_LOGW(TAG, "Fetch config: HTTP %d", status);
        esp_http_client_cleanup(client);
        return false;
    }

    int content_length = esp_http_client_get_content_length(client);
    if (content_length <= 0 || content_length > 512) {
        esp_http_client_cleanup(client);
        return false;
    }

    char* buffer = malloc(content_length + 1);
    if (!buffer) {
        esp_http_client_cleanup(client);
        return false;
    }

    int read_len = esp_http_client_read(client, buffer, content_length);
    buffer[read_len] = '\0';
    esp_http_client_cleanup(client);

    cJSON* json = cJSON_Parse(buffer);
    free(buffer);
    if (!json) return false;

    // Copia device_id nella config
    strncpy(out_config->device_id, device_id, sizeof(out_config->device_id) - 1);

    cJSON* friendly = cJSON_GetObjectItem(json, "friendly_name");
    cJSON* location = cJSON_GetObjectItem(json, "location_id");

    if (friendly && cJSON_IsString(friendly) && strlen(friendly->valuestring) > 0) {
        strncpy(out_config->friendly_name, friendly->valuestring, sizeof(out_config->friendly_name) - 1);
        out_config->is_configured = true;
    } else {
        out_config->is_configured = false;
    }

    if (location && cJSON_IsString(location)) {
        strncpy(out_config->location_id, location->valuestring, sizeof(out_config->location_id) - 1);
    }

    ESP_LOGI(TAG, "Config fetched: name=%s, location=%s, configured=%d",
             out_config->friendly_name, out_config->location_id, out_config->is_configured);

    cJSON_Delete(json);
    return true;
}

bool jarvis_network_send_heartbeat(const char* device_id, const char* firmware_version, device_config_t* out_config) {
    if (!wifi_connected || !device_id) return false;

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/heartbeat",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT);

    cJSON* json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "device_id", device_id);
    if (firmware_version) {
        cJSON_AddStringToObject(json, "firmware_version", firmware_version);
    }

    char* payload = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (!payload) return false;

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        free(payload);
        return false;
    }

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, payload, strlen(payload));

    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Heartbeat failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        free(payload);
        return false;
    }

    int status = esp_http_client_get_status_code(client);
    bool success = (status == 200);

    // Parse response per eventuali aggiornamenti config
    if (success && out_config) {
        int content_length = esp_http_client_get_content_length(client);
        if (content_length > 0 && content_length < 512) {
            char* resp_buf = malloc(content_length + 1);
            if (resp_buf) {
                int read_len = esp_http_client_read(client, resp_buf, content_length);
                resp_buf[read_len] = '\0';

                cJSON* resp_json = cJSON_Parse(resp_buf);
                if (resp_json) {
                    cJSON* friendly = cJSON_GetObjectItem(resp_json, "friendly_name");
                    cJSON* location = cJSON_GetObjectItem(resp_json, "location_id");

                    if (friendly && cJSON_IsString(friendly)) {
                        strncpy(out_config->friendly_name, friendly->valuestring,
                                sizeof(out_config->friendly_name) - 1);
                        out_config->is_configured = true;
                    }
                    if (location && cJSON_IsString(location)) {
                        strncpy(out_config->location_id, location->valuestring,
                                sizeof(out_config->location_id) - 1);
                    }

                    cJSON_Delete(resp_json);
                }
                free(resp_buf);
            }
        }
    }

    esp_http_client_cleanup(client);
    free(payload);

    ESP_LOGD(TAG, "Heartbeat: HTTP %d", status);
    return success;
}

void jarvis_network_set_config_callback(config_update_callback_t config_cb) {
    config_callback = config_cb;
}

// =============================================================================
// STREAMING
// =============================================================================

static bool send_accumulated_chunk(void) {
    if (accumulated_samples == 0 || stream_socket < 0) return true;

    size_t data_size = accumulated_samples * sizeof(int16_t);

    // Send as HTTP chunk
    char chunk_header[16];
    snprintf(chunk_header, sizeof(chunk_header), "%zx\r\n", data_size);

    if (send(stream_socket, chunk_header, strlen(chunk_header), 0) < 0) {
        return false;
    }
    if (send(stream_socket, accumulate_buffer, data_size, 0) < 0) {
        return false;
    }
    if (send(stream_socket, "\r\n", 2, 0) < 0) {
        return false;
    }

    total_bytes_sent += data_size;
    accumulated_samples = 0;
    return true;
}

bool jarvis_network_start_stream(const char* room) {
    if (!wifi_connected) {
        ESP_LOGE(TAG, "Cannot start stream: no WiFi");
        return false;
    }

    if (stream_state != STREAM_NET_IDLE) {
        ESP_LOGW(TAG, "Stream already in progress");
        return false;
    }

    strncpy(current_room, room, sizeof(current_room) - 1);
    total_bytes_sent = 0;
    accumulated_samples = 0;
    stream_start_time = esp_timer_get_time() / 1000;
    stream_state = STREAM_NET_CONNECTING;

    // Resolve host
    struct addrinfo hints = {
        .ai_family = AF_INET,
        .ai_socktype = SOCK_STREAM,
    };
    struct addrinfo *res;
    char port_str[8];
    snprintf(port_str, sizeof(port_str), "%d", JARVIS_SERVER_PORT);

    int err = getaddrinfo(JARVIS_SERVER_HOST, port_str, &hints, &res);
    if (err != 0 || res == NULL) {
        ESP_LOGE(TAG, "DNS lookup failed: %d", err);
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    // Create socket
    stream_socket = socket(res->ai_family, res->ai_socktype, 0);
    if (stream_socket < 0) {
        ESP_LOGE(TAG, "Socket creation failed");
        freeaddrinfo(res);
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    // Connect
    if (connect(stream_socket, res->ai_addr, res->ai_addrlen) != 0) {
        ESP_LOGE(TAG, "Socket connect failed");
        close(stream_socket);
        stream_socket = -1;
        freeaddrinfo(res);
        stream_state = STREAM_NET_ERROR;
        return false;
    }
    freeaddrinfo(res);

    // Send HTTP header with chunked transfer
    const char* boundary = "----JarvisAudioStream";
    char header[512];
    int header_len = snprintf(header, sizeof(header),
        "POST /voice_stream HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Content-Type: multipart/form-data; boundary=%s\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: keep-alive\r\n"
        "\r\n",
        JARVIS_SERVER_HOST, boundary);

    if (send(stream_socket, header, header_len, 0) < 0) {
        ESP_LOGE(TAG, "Failed to send header");
        close(stream_socket);
        stream_socket = -1;
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    // Send metadata part
    char meta_part[512];
    int meta_len = snprintf(meta_part, sizeof(meta_part),
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"room\"\r\n\r\n"
        "%s\r\n"
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"mic_id\"\r\n\r\n"
        "%s\r\n"
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"audio\"; filename=\"audio.raw\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n",
        boundary, current_room, boundary, DEVICE_ID, boundary);

    char chunk_header[16];
    snprintf(chunk_header, sizeof(chunk_header), "%x\r\n", meta_len);
    send(stream_socket, chunk_header, strlen(chunk_header), 0);
    send(stream_socket, meta_part, meta_len, 0);
    send(stream_socket, "\r\n", 2, 0);

    stream_state = STREAM_NET_SENDING;
    ESP_LOGI(TAG, "Stream started to %s:%d", JARVIS_SERVER_HOST, JARVIS_SERVER_PORT);
    return true;
}

bool jarvis_network_send_chunk(int16_t* chunk, size_t samples) {
    if (stream_state != STREAM_NET_SENDING) return false;
    if (stream_socket < 0) {
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    // Accumulate samples
    size_t to_add = samples;
    if (accumulated_samples + to_add > ACCUMULATE_SAMPLES) {
        to_add = ACCUMULATE_SAMPLES - accumulated_samples;
    }

    memcpy(&accumulate_buffer[accumulated_samples], chunk, to_add * sizeof(int16_t));
    accumulated_samples += to_add;

    // Send if buffer full
    if (accumulated_samples >= ACCUMULATE_SAMPLES) {
        if (!send_accumulated_chunk()) {
            stream_state = STREAM_NET_ERROR;
            return false;
        }
    }

    // Handle remaining samples
    if (to_add < samples) {
        size_t remaining = samples - to_add;
        memcpy(accumulate_buffer, &chunk[to_add], remaining * sizeof(int16_t));
        accumulated_samples = remaining;
    }

    return true;
}

bool jarvis_network_end_stream(bool* use_local_speaker) {
    if (use_local_speaker) {
        *use_local_speaker = false;  // Default: non usare speaker locale
    }

    if (stream_state != STREAM_NET_SENDING) {
        stream_state = STREAM_NET_IDLE;
        return false;
    }

    stream_state = STREAM_NET_FINISHING;

    // Send remaining samples
    send_accumulated_chunk();

    // Send boundary end
    const char* boundary = "----JarvisAudioStream";
    char end_part[64];
    int end_len = snprintf(end_part, sizeof(end_part), "\r\n--%s--\r\n", boundary);

    char chunk_header[16];
    snprintf(chunk_header, sizeof(chunk_header), "%x\r\n", end_len);
    send(stream_socket, chunk_header, strlen(chunk_header), 0);
    send(stream_socket, end_part, end_len, 0);
    send(stream_socket, "\r\n", 2, 0);

    // Send final empty chunk
    send(stream_socket, "0\r\n\r\n", 5, 0);

    // Read response
    char response[512];
    bool success = false;

    struct timeval tv = { .tv_sec = 10, .tv_usec = 0 };
    setsockopt(stream_socket, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    int received = recv(stream_socket, response, sizeof(response) - 1, 0);
    if (received > 0) {
        response[received] = '\0';

        if (strstr(response, "200") != NULL) {
            success = true;
        }

        // Parse JSON response
        char* json_start = strchr(response, '{');
        if (json_start) {
            cJSON* json = cJSON_Parse(json_start);
            if (json) {
                cJSON* status = cJSON_GetObjectItem(json, "status");
                cJSON* speaker = cJSON_GetObjectItem(json, "speaker");

                ESP_LOGI(TAG, "Stream response: status=%s, speaker=%s",
                         status ? status->valuestring : "unknown",
                         speaker ? speaker->valuestring : "");

                // Controlla se il server richiede playback locale
                if (use_local_speaker && speaker && cJSON_IsString(speaker)) {
                    if (strcmp(speaker->valuestring, "local") == 0) {
                        *use_local_speaker = true;
                    }
                }

                if (response_callback) {
                    response_callback(success, status ? status->valuestring : "unknown");
                }

                cJSON_Delete(json);
            }
        }
    }

    close(stream_socket);
    stream_socket = -1;
    stream_state = STREAM_NET_IDLE;

    int64_t duration = (esp_timer_get_time() / 1000) - stream_start_time;
    ESP_LOGI(TAG, "Stream ended: %zu bytes in %lld ms", total_bytes_sent, duration);

    return success;
}

bool jarvis_network_is_streaming(void) {
    return stream_state != STREAM_NET_IDLE;
}

// =============================================================================
// TEMPERATURE
// =============================================================================

bool jarvis_network_fetch_temperature(const char* room, float* temp) {
    if (!wifi_connected) return false;
    if (!room || room[0] == '\0') return false;

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/room_temperature/%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, room);

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return false;

    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        esp_http_client_cleanup(client);
        return false;
    }

    int status = esp_http_client_get_status_code(client);
    if (status != 200) {
        esp_http_client_cleanup(client);
        return false;
    }

    int content_length = esp_http_client_get_content_length(client);
    if (content_length <= 0 || content_length > 256) {
        esp_http_client_cleanup(client);
        return false;
    }

    char* buffer = malloc(content_length + 1);
    if (!buffer) {
        esp_http_client_cleanup(client);
        return false;
    }

    int read_len = esp_http_client_read(client, buffer, content_length);
    buffer[read_len] = '\0';
    esp_http_client_cleanup(client);

    cJSON* json = cJSON_Parse(buffer);
    free(buffer);

    if (!json) return false;

    cJSON* temp_obj = cJSON_GetObjectItem(json, "temperature");
    if (temp_obj && cJSON_IsNumber(temp_obj)) {
        *temp = (float)temp_obj->valuedouble;
        cJSON_Delete(json);
        return true;
    }

    cJSON_Delete(json);
    return false;
}

// =============================================================================
// DND & STATE POLLING
// =============================================================================

void jarvis_network_notify_dnd(const char* device_id, bool enabled) {
    if (!wifi_connected) return;
    if (!device_id || device_id[0] == '\0') return;

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/device_status",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT);

    cJSON* json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "device_id", device_id);
    cJSON_AddBoolToObject(json, "dnd", enabled);

    char* payload = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client) {
        esp_http_client_set_header(client, "Content-Type", "application/json");
        esp_http_client_set_post_field(client, payload, strlen(payload));
        esp_http_client_perform(client);
        esp_http_client_cleanup(client);
    }

    free(payload);
}

void jarvis_network_poll_state(const char* device_id) {
    if (!wifi_connected) return;
    if (stream_state != STREAM_NET_IDLE) return;
    if (!device_id || device_id[0] == '\0') return;

    char url[256];
    snprintf(url, sizeof(url), "http://%s:%d/device_status?device_id=%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, device_id);

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 2000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return;

    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK || esp_http_client_get_status_code(client) != 200) {
        esp_http_client_cleanup(client);
        return;
    }

    int content_length = esp_http_client_get_content_length(client);
    if (content_length <= 0 || content_length > 512) {
        esp_http_client_cleanup(client);
        return;
    }

    char* buffer = malloc(content_length + 1);
    if (!buffer) {
        esp_http_client_cleanup(client);
        return;
    }

    int read_len = esp_http_client_read(client, buffer, content_length);
    buffer[read_len] = '\0';
    esp_http_client_cleanup(client);

    cJSON* json = cJSON_Parse(buffer);
    free(buffer);

    if (!json) return;

    cJSON* speaking = cJSON_GetObjectItem(json, "speaking");
    cJSON* target_room = cJSON_GetObjectItem(json, "target_room");

    bool busy = false;
    if (speaking && cJSON_IsTrue(speaking)) {
        bool is_our_room = true;
        if (target_room && cJSON_IsString(target_room) && strlen(target_room->valuestring) > 0) {
            is_our_room = (strcmp(target_room->valuestring, DEVICE_ROOM) == 0);
        }
        busy = is_our_room;
    }

    if (busy_callback) {
        busy_callback(busy);
    }

    cJSON_Delete(json);
}

void jarvis_network_set_callbacks(
    server_response_callback_t response_cb,
    busy_state_callback_t busy_cb
) {
    response_callback = response_cb;
    busy_callback = busy_cb;
}

// =============================================================================
// SPEAKER SUPPRESS (fire-and-forget)
// =============================================================================

void jarvis_network_suppress_speaker(const char* device_id) {
    if (!wifi_connected) {
        ESP_LOGW(TAG, "Cannot suppress speaker: no WiFi");
        return;
    }

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/speaker/suppress",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT);

    cJSON* json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "device_id", device_id);

    char* payload = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);

    if (!payload) {
        ESP_LOGE(TAG, "Failed to create suppress payload");
        return;
    }

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 3000,  // Timeout breve: fire-and-forget
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client) {
        esp_http_client_set_header(client, "Content-Type", "application/json");
        esp_http_client_set_post_field(client, payload, strlen(payload));

        esp_err_t err = esp_http_client_perform(client);
        if (err == ESP_OK) {
            int status = esp_http_client_get_status_code(client);
            ESP_LOGI(TAG, "Speaker suppress sent (HTTP %d)", status);
        } else {
            ESP_LOGW(TAG, "Speaker suppress failed: %s", esp_err_to_name(err));
        }

        esp_http_client_cleanup(client);
    }

    free(payload);
}
