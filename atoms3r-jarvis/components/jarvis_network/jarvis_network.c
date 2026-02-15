/**
 * =============================================================================
 * JARVIS AtomS3R - Network Module Implementation (ESP-IDF)
 * =============================================================================
 *
 * NOTA CRITICA: esp_http_client_perform() consuma TUTTO il response body
 * internamente. NON si può usare esp_http_client_read() dopo perform().
 * Per leggere il body bisogna usare un event_handler con HTTP_EVENT_ON_DATA.
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
#include "esp_tls.h"
#include "esp_crt_bundle.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "cJSON.h"

static const char *TAG = "NETWORK";

// =============================================================================
// CONFIGURATION (should match jarvis_config.h)
// =============================================================================

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

#if JARVIS_SERVER_PORT == 443
#define JARVIS_URL_SCHEME   "https"
#define JARVIS_USE_TLS      1
#else
#define JARVIS_URL_SCHEME   "http"
#define JARVIS_USE_TLS      0
#endif

#if JARVIS_USE_TLS
#define JARVIS_TLS_CONFIG  .crt_bundle_attach = esp_crt_bundle_attach,
#else
#define JARVIS_TLS_CONFIG
#endif

#ifdef CONFIG_JARVIS_API_TOKEN
#define JARVIS_API_TOKEN    CONFIG_JARVIS_API_TOKEN
#else
#define JARVIS_API_TOKEN    ""
#endif

#define JARVIS_HAS_TOKEN    (JARVIS_API_TOKEN[0] != '\0')

#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1

// Streaming buffer
#define ACCUMULATE_SAMPLES  1600  // ~100ms @ 16kHz

// =============================================================================
// HTTP RESPONSE BUFFER (per event handler)
// =============================================================================
// Struttura passata come user_data all'event handler HTTP.
// perform() chiama l'handler con HTTP_EVENT_ON_DATA per ogni chunk del body.

typedef struct {
    char *buffer;       // Buffer allocato dal chiamante
    int buf_size;       // Dimensione del buffer
    int offset;         // Bytes scritti finora
} http_response_buf_t;

// Event handler generico: accumula il body nel buffer
static esp_err_t http_event_handler(esp_http_client_event_t *evt) {
    http_response_buf_t *resp = (http_response_buf_t *)evt->user_data;
    if (!resp) return ESP_OK;

    switch (evt->event_id) {
        case HTTP_EVENT_ON_DATA:
            if (resp->buffer && evt->data_len > 0) {
                int space = resp->buf_size - resp->offset - 1;  // -1 per null terminator
                int to_copy = evt->data_len < space ? evt->data_len : space;
                if (to_copy > 0) {
                    memcpy(resp->buffer + resp->offset, evt->data, to_copy);
                    resp->offset += to_copy;
                    resp->buffer[resp->offset] = '\0';
                }
            }
            break;
        default:
            break;
    }
    return ESP_OK;
}

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
static char current_room[32] = "";
static size_t total_bytes_sent = 0;
static int64_t stream_start_time = 0;

// Accumulate buffer
static int16_t* accumulate_buffer = NULL;
static size_t accumulated_samples = 0;

// =============================================================================
// AUTH HELPER
// =============================================================================

static void set_auth_header(esp_http_client_handle_t client) {
    if (JARVIS_HAS_TOKEN) {
        char auth_header[128];
        snprintf(auth_header, sizeof(auth_header), "Bearer %s", JARVIS_API_TOKEN);
        esp_http_client_set_header(client, "Authorization", auth_header);
    }
}

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

    wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                    &wifi_event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                    &wifi_event_handler, NULL, &instance_got_ip));

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

    EventBits_t bits = xEventGroupWaitBits(wifi_event_group,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(30000));

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi connected!");

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
    snprintf(url, sizeof(url), JARVIS_URL_SCHEME "://%s:%d/device_config?device_id=%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, device_id);

    ESP_LOGI(TAG, "Fetching config from: %s", url);

    // Buffer per catturare il response body via event handler
    char body_buf[512] = {0};
    http_response_buf_t resp_buf = {
        .buffer = body_buf,
        .buf_size = sizeof(body_buf),
        .offset = 0,
    };

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 10000,
        .disable_auto_redirect = false,
        .event_handler = http_event_handler,
        .user_data = &resp_buf,
        JARVIS_TLS_CONFIG
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        ESP_LOGE(TAG, "HTTP client init failed");
        return false;
    }
    set_auth_header(client);

    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Fetch config failed: %s (url=%s)", esp_err_to_name(err), url);
        esp_http_client_cleanup(client);
        return false;
    }

    int status = esp_http_client_get_status_code(client);
    ESP_LOGI(TAG, "Fetch config response: HTTP %d, body_len=%d", status, resp_buf.offset);
    esp_http_client_cleanup(client);

    if (status != 200) {
        if (resp_buf.offset > 0) {
            ESP_LOGW(TAG, "Fetch config error body: %s", body_buf);
        }
        return false;
    }

    if (resp_buf.offset <= 0) {
        ESP_LOGW(TAG, "Fetch config: empty response body");
        return false;
    }

    ESP_LOGI(TAG, "Fetch config body: %s", body_buf);

    cJSON* json = cJSON_Parse(body_buf);
    if (!json) {
        ESP_LOGE(TAG, "Failed to parse config JSON");
        return false;
    }

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
    snprintf(url, sizeof(url), JARVIS_URL_SCHEME "://%s:%d/heartbeat",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT);

    cJSON* json = cJSON_CreateObject();
    cJSON_AddStringToObject(json, "device_id", device_id);
    if (firmware_version) {
        cJSON_AddStringToObject(json, "firmware_version", firmware_version);
    }

    char* payload = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (!payload) return false;

    ESP_LOGI(TAG, "Sending heartbeat to: %s", url);

    // Buffer per catturare il response body
    char body_buf[512] = {0};
    http_response_buf_t resp_buf = {
        .buffer = body_buf,
        .buf_size = sizeof(body_buf),
        .offset = 0,
    };

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 10000,
        .disable_auto_redirect = false,
        .event_handler = http_event_handler,
        .user_data = &resp_buf,
        JARVIS_TLS_CONFIG
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        free(payload);
        return false;
    }
    set_auth_header(client);

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, payload, strlen(payload));

    esp_err_t err = esp_http_client_perform(client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Heartbeat failed: %s (url=%s)", esp_err_to_name(err), url);
        esp_http_client_cleanup(client);
        free(payload);
        return false;
    }

    int status = esp_http_client_get_status_code(client);
    ESP_LOGI(TAG, "Heartbeat response: HTTP %d, body_len=%d", status, resp_buf.offset);
    bool success = (status == 200);

    // Parse response per eventuali aggiornamenti config
    if (success && out_config && resp_buf.offset > 0) {
        cJSON* resp_json = cJSON_Parse(body_buf);
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
    }

    esp_http_client_cleanup(client);
    free(payload);

    return success;
}

void jarvis_network_set_config_callback(config_update_callback_t config_cb) {
    config_callback = config_cb;
}

// =============================================================================
// STREAMING (usa esp_tls per supporto HTTPS trasparente)
// =============================================================================

static esp_tls_t *stream_tls = NULL;

static int tls_write_all(esp_tls_t *tls, const char *data, size_t len) {
    size_t written = 0;
    while (written < len) {
        int ret = esp_tls_conn_write(tls, data + written, len - written);
        if (ret < 0) {
            ESP_LOGE(TAG, "TLS write error: %d", ret);
            return -1;
        }
        written += ret;
    }
    return (int)written;
}

static bool send_accumulated_chunk(void) {
    if (accumulated_samples == 0 || !stream_tls) return true;

    size_t data_size = accumulated_samples * sizeof(int16_t);

    char chunk_header[16];
    snprintf(chunk_header, sizeof(chunk_header), "%zx\r\n", data_size);

    if (tls_write_all(stream_tls, chunk_header, strlen(chunk_header)) < 0) {
        return false;
    }
    if (tls_write_all(stream_tls, (const char*)accumulate_buffer, data_size) < 0) {
        return false;
    }
    if (tls_write_all(stream_tls, "\r\n", 2) < 0) {
        return false;
    }

    total_bytes_sent += data_size;
    accumulated_samples = 0;
    return true;
}

bool jarvis_network_start_stream(const char* device_id) {
    if (!wifi_connected) {
        ESP_LOGE(TAG, "Cannot start stream: no WiFi");
        return false;
    }

    if (stream_state != STREAM_NET_IDLE) {
        ESP_LOGW(TAG, "Stream already in progress");
        return false;
    }

    strncpy(current_room, device_id, sizeof(current_room) - 1);
    total_bytes_sent = 0;
    accumulated_samples = 0;
    stream_start_time = esp_timer_get_time() / 1000;
    stream_state = STREAM_NET_CONNECTING;

    esp_tls_cfg_t tls_cfg = {
        .timeout_ms = 10000,
#if JARVIS_USE_TLS
        .crt_bundle_attach = esp_crt_bundle_attach,
#else
        .is_plain_tcp = true,
#endif
    };

    stream_tls = esp_tls_init();
    if (!stream_tls) {
        ESP_LOGE(TAG, "TLS init failed");
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    int ret = esp_tls_conn_new_sync(JARVIS_SERVER_HOST, strlen(JARVIS_SERVER_HOST),
                                     JARVIS_SERVER_PORT, &tls_cfg, stream_tls);
    if (ret != 1) {
        ESP_LOGE(TAG, "TLS connection failed: %d", ret);
        esp_tls_conn_destroy(stream_tls);
        stream_tls = NULL;
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    const char* boundary = "----JarvisAudioStream";
    char header[640];
    int header_len;
    if (JARVIS_HAS_TOKEN) {
        header_len = snprintf(header, sizeof(header),
            "POST /voice_stream HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Content-Type: multipart/form-data; boundary=%s\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Authorization: Bearer %s\r\n"
            "Connection: keep-alive\r\n"
            "\r\n",
            JARVIS_SERVER_HOST, boundary, JARVIS_API_TOKEN);
    } else {
        header_len = snprintf(header, sizeof(header),
            "POST /voice_stream HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Content-Type: multipart/form-data; boundary=%s\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: keep-alive\r\n"
            "\r\n",
            JARVIS_SERVER_HOST, boundary);
    }

    if (tls_write_all(stream_tls, header, header_len) < 0) {
        ESP_LOGE(TAG, "Failed to send header");
        esp_tls_conn_destroy(stream_tls);
        stream_tls = NULL;
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    char meta_part[512];
    int meta_len = snprintf(meta_part, sizeof(meta_part),
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"device_id\"\r\n\r\n"
        "%s\r\n"
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"audio\"; filename=\"audio.raw\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n",
        boundary, device_id, boundary);

    char chunk_header[16];
    snprintf(chunk_header, sizeof(chunk_header), "%x\r\n", meta_len);
    tls_write_all(stream_tls, chunk_header, strlen(chunk_header));
    tls_write_all(stream_tls, meta_part, meta_len);
    tls_write_all(stream_tls, "\r\n", 2);

    stream_state = STREAM_NET_SENDING;
    ESP_LOGI(TAG, "Stream started to %s:%d (%s)",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT,
             JARVIS_USE_TLS ? "HTTPS" : "HTTP");
    return true;
}

bool jarvis_network_send_chunk(int16_t* chunk, size_t samples) {
    if (stream_state != STREAM_NET_SENDING) return false;
    if (!stream_tls) {
        stream_state = STREAM_NET_ERROR;
        return false;
    }

    size_t to_add = samples;
    if (accumulated_samples + to_add > ACCUMULATE_SAMPLES) {
        to_add = ACCUMULATE_SAMPLES - accumulated_samples;
    }

    memcpy(&accumulate_buffer[accumulated_samples], chunk, to_add * sizeof(int16_t));
    accumulated_samples += to_add;

    if (accumulated_samples >= ACCUMULATE_SAMPLES) {
        if (!send_accumulated_chunk()) {
            stream_state = STREAM_NET_ERROR;
            return false;
        }
    }

    if (to_add < samples) {
        size_t remaining = samples - to_add;
        memcpy(accumulate_buffer, &chunk[to_add], remaining * sizeof(int16_t));
        accumulated_samples = remaining;
    }

    return true;
}

bool jarvis_network_end_stream(bool* use_local_speaker) {
    if (use_local_speaker) {
        *use_local_speaker = false;
    }

    if (stream_state != STREAM_NET_SENDING) {
        stream_state = STREAM_NET_IDLE;
        return false;
    }

    stream_state = STREAM_NET_FINISHING;

    send_accumulated_chunk();

    const char* boundary = "----JarvisAudioStream";
    char end_part[64];
    int end_len = snprintf(end_part, sizeof(end_part), "\r\n--%s--\r\n", boundary);

    char chunk_header[16];
    snprintf(chunk_header, sizeof(chunk_header), "%x\r\n", end_len);
    tls_write_all(stream_tls, chunk_header, strlen(chunk_header));
    tls_write_all(stream_tls, end_part, end_len);
    tls_write_all(stream_tls, "\r\n", 2);

    // Send final empty chunk
    tls_write_all(stream_tls, "0\r\n\r\n", 5);

    // Read response
    char response[512];
    bool success = false;

    int received = 0;
    int64_t read_start = esp_timer_get_time() / 1000;
    while (received == 0) {
        int ret = esp_tls_conn_read(stream_tls, response, sizeof(response) - 1);
        if (ret > 0) {
            received = ret;
        } else if (ret == 0) {
            break;
        } else if (ret == ESP_TLS_ERR_SSL_WANT_READ || ret == ESP_TLS_ERR_SSL_WANT_WRITE) {
            vTaskDelay(pdMS_TO_TICKS(10));
            if ((esp_timer_get_time() / 1000) - read_start > 10000) {
                ESP_LOGW(TAG, "Stream response timeout");
                break;
            }
        } else {
            ESP_LOGE(TAG, "TLS read error: %d", ret);
            break;
        }
    }

    if (received > 0) {
        response[received] = '\0';

        if (strstr(response, "200") != NULL) {
            success = true;
        }

        char* json_start = strchr(response, '{');
        if (json_start) {
            cJSON* json = cJSON_Parse(json_start);
            if (json) {
                cJSON* status = cJSON_GetObjectItem(json, "status");
                cJSON* speaker = cJSON_GetObjectItem(json, "speaker");

                ESP_LOGI(TAG, "Stream response: status=%s, speaker=%s",
                         status ? status->valuestring : "unknown",
                         speaker ? speaker->valuestring : "");

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

    esp_tls_conn_destroy(stream_tls);
    stream_tls = NULL;
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

    // Converti room in lowercase per matching entity_id HA (sensor.temperatura_soggiorno)
    char room_lower[64];
    int i;
    for (i = 0; room[i] && i < (int)sizeof(room_lower) - 1; i++) {
        room_lower[i] = (room[i] >= 'A' && room[i] <= 'Z') ? room[i] + 32 : room[i];
    }
    room_lower[i] = '\0';

    char url[192];
    snprintf(url, sizeof(url), JARVIS_URL_SCHEME "://%s:%d/room_temperature/%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, room_lower);

    ESP_LOGI(TAG, "Fetching temperature from: %s", url);

    char body_buf[256] = {0};
    http_response_buf_t resp_buf = {
        .buffer = body_buf,
        .buf_size = sizeof(body_buf),
        .offset = 0,
    };

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 5000,
        .event_handler = http_event_handler,
        .user_data = &resp_buf,
        JARVIS_TLS_CONFIG
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return false;
    set_auth_header(client);

    esp_err_t err = esp_http_client_perform(client);
    int status = (err == ESP_OK) ? esp_http_client_get_status_code(client) : 0;
    ESP_LOGI(TAG, "Temperature response: HTTP %d, body_len=%d", status, resp_buf.offset);
    esp_http_client_cleanup(client);

    if (err != ESP_OK || status != 200 || resp_buf.offset <= 0) {
        if (resp_buf.offset > 0) {
            ESP_LOGW(TAG, "Temperature error body: %s", body_buf);
        }
        return false;
    }

    ESP_LOGD(TAG, "Temperature body: %s", body_buf);

    cJSON* json = cJSON_Parse(body_buf);
    if (!json) {
        ESP_LOGW(TAG, "Temperature: failed to parse JSON");
        return false;
    }

    cJSON* temp_obj = cJSON_GetObjectItem(json, "temperature");
    if (temp_obj && cJSON_IsNumber(temp_obj)) {
        *temp = (float)temp_obj->valuedouble;
        ESP_LOGI(TAG, "Temperature parsed: %.1f", *temp);
        cJSON_Delete(json);
        return true;
    }

    ESP_LOGW(TAG, "Temperature: no 'temperature' field in JSON");
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
    snprintf(url, sizeof(url), JARVIS_URL_SCHEME "://%s:%d/device_status",
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
        JARVIS_TLS_CONFIG
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client) {
        set_auth_header(client);
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
    snprintf(url, sizeof(url), JARVIS_URL_SCHEME "://%s:%d/device_status?device_id=%s",
             JARVIS_SERVER_HOST, JARVIS_SERVER_PORT, device_id);

    char body_buf[512] = {0};
    http_response_buf_t resp_buf = {
        .buffer = body_buf,
        .buf_size = sizeof(body_buf),
        .offset = 0,
    };

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 2000,
        .event_handler = http_event_handler,
        .user_data = &resp_buf,
        JARVIS_TLS_CONFIG
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return;
    set_auth_header(client);

    esp_err_t err = esp_http_client_perform(client);
    int status = (err == ESP_OK) ? esp_http_client_get_status_code(client) : 0;
    esp_http_client_cleanup(client);

    if (err != ESP_OK || status != 200 || resp_buf.offset <= 0) {
        return;
    }

    cJSON* json = cJSON_Parse(body_buf);
    if (!json) return;

    cJSON* speaking = cJSON_GetObjectItem(json, "speaking");
    bool busy = (speaking && cJSON_IsTrue(speaking));

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
    snprintf(url, sizeof(url), JARVIS_URL_SCHEME "://%s:%d/speaker/suppress",
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
        .timeout_ms = 3000,
        JARVIS_TLS_CONFIG
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client) {
        set_auth_header(client);
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
