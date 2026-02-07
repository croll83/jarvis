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

#define WIFI_SSID           CONFIG_WIFI_SSID
#define WIFI_PASSWORD       CONFIG_WIFI_PASSWORD
#define JARVIS_SERVER_HOST  CONFIG_JARVIS_SERVER_HOST
#define JARVIS_SERVER_PORT  CONFIG_JARVIS_SERVER_PORT
#define DEVICE_ROOM         CONFIG_DEVICE_ROOM
#define DEVICE_ID           CONFIG_DEVICE_ID

#ifndef CONFIG_WIFI_SSID
#define WIFI_SSID           "YOUR_WIFI_SSID"
#endif
#ifndef CONFIG_WIFI_PASSWORD
#define WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"
#endif
#ifndef CONFIG_JARVIS_SERVER_HOST
#define JARVIS_SERVER_HOST  "jarvis.local"
#endif
#ifndef CONFIG_JARVIS_SERVER_PORT
#define JARVIS_SERVER_PORT  5000
#endif
#ifndef CONFIG_DEVICE_ROOM
#define DEVICE_ROOM         "salotto"
#endif
#ifndef CONFIG_DEVICE_ID
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

bool jarvis_network_end_stream(void) {
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

bool jarvis_network_fetch_temperature(float* temp) {
    if (!wifi_connected) return false;

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/room_temperature/%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, DEVICE_ROOM);

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

void jarvis_network_notify_dnd(bool enabled) {
    if (!wifi_connected) return;

    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/device_status",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT);

    cJSON* json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(json, "room", DEVICE_ROOM);
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

void jarvis_network_poll_state(void) {
    if (!wifi_connected) return;
    if (stream_state != STREAM_NET_IDLE) return;

    char url[256];
    snprintf(url, sizeof(url), "http://%s:%d/device_status?device_id=%s&room=%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, DEVICE_ID, DEVICE_ROOM);

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
