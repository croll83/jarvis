/**
 * =============================================================================
 * AtomS3R Audio Recorder - Firmware minimale per registrazione WAV via TCP
 * =============================================================================
 *
 * Flusso:
 *   1. Init WiFi + ES8311 codec (stessi gain del firmware JARVIS)
 *   2. Connessione TCP al server (Mac con record_from_atom.py)
 *   3. Attende comando "REC\n" → beep + stream PCM raw
 *   4. Attende comando "STOP\n" → ferma stream
 *   5. Loop fino a disconnessione
 *
 * Protocollo:
 *   Mac → Device:  "REC\n", "STOP\n"
 *   Device → Mac:   "STATUS:idle\n", "STATUS:recording\n", "STATUS:connected\n"
 *   Device → Mac:   [0xAA 0x55] [uint16_le pcm_bytes] [int16_le PCM...]
 *
 * Hardware: M5Stack AtomS3R (ESP32-S3-PICO-1-N8R8)
 */

#include <string.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"

#include "jarvis_codec.h"

static const char *TAG = "RECORDER";

// =============================================================================
// CONFIGURATION
// =============================================================================

#ifndef CONFIG_WIFI_SSID
#define CONFIG_WIFI_SSID "YOUR_WIFI_SSID"
#endif
#ifndef CONFIG_WIFI_PASSWORD
#define CONFIG_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#endif
#ifndef CONFIG_RECORDER_SERVER_HOST
#define CONFIG_RECORDER_SERVER_HOST "192.168.1.100"
#endif
#ifndef CONFIG_RECORDER_SERVER_PORT
#define CONFIG_RECORDER_SERVER_PORT 5000
#endif

#define SAMPLE_RATE         16000
#define FRAME_SAMPLES       320     // 20ms @ 16kHz
#define FRAME_BYTES         (FRAME_SAMPLES * 2)  // 640 bytes
#define SOFTWARE_GAIN       4       // 4x digital gain (same as JARVIS streaming path)

// Beep configuration
#define BEEP_FREQ_HZ        880
#define BEEP_DURATION_MS     150
#define BEEP_SAMPLES         (SAMPLE_RATE * BEEP_DURATION_MS / 1000)  // 2400
#define BEEP_AMPLITUDE       6000

// Frame header
#define FRAME_MAGIC_0       0xAA
#define FRAME_MAGIC_1       0x55

// =============================================================================
// WIFI
// =============================================================================

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1

static int wifi_retry_count = 0;
#define WIFI_MAX_RETRY      10

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (wifi_retry_count < WIFI_MAX_RETRY) {
            esp_wifi_connect();
            wifi_retry_count++;
            ESP_LOGI(TAG, "WiFi retry %d/%d...", wifi_retry_count, WIFI_MAX_RETRY);
        } else {
            xEventGroupSetBits(wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "WiFi connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        wifi_retry_count = 0;
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static bool init_wifi(void) {
    wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t inst_any, inst_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, &inst_any));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, &inst_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    strncpy((char *)wifi_config.sta.ssid, CONFIG_WIFI_SSID,
            sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, CONFIG_WIFI_PASSWORD,
            sizeof(wifi_config.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "WiFi connecting to '%s'...", CONFIG_WIFI_SSID);

    EventBits_t bits = xEventGroupWaitBits(wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE, portMAX_DELAY);

    if (bits & WIFI_CONNECTED_BIT) {
        return true;
    }
    ESP_LOGE(TAG, "WiFi connection failed!");
    return false;
}

// =============================================================================
// TCP CLIENT
// =============================================================================

static int tcp_sock = -1;

static int tcp_connect(void) {
    struct sockaddr_in dest_addr;
    dest_addr.sin_addr.s_addr = inet_addr(CONFIG_RECORDER_SERVER_HOST);
    dest_addr.sin_family = AF_INET;
    dest_addr.sin_port = htons(CONFIG_RECORDER_SERVER_PORT);

    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "Socket creation failed: errno %d", errno);
        return -1;
    }

    // Set TCP_NODELAY for low-latency audio
    int flag = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof(flag));

    // Set send buffer size
    int sndbuf = 8192;
    setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    ESP_LOGI(TAG, "Connecting to %s:%d...",
             CONFIG_RECORDER_SERVER_HOST, CONFIG_RECORDER_SERVER_PORT);

    int err = connect(sock, (struct sockaddr *)&dest_addr, sizeof(dest_addr));
    if (err != 0) {
        ESP_LOGE(TAG, "Connect failed: errno %d", errno);
        close(sock);
        return -1;
    }

    ESP_LOGI(TAG, "TCP connected!");
    return sock;
}

static bool tcp_send_status(int sock, const char *status) {
    char buf[64];
    int len = snprintf(buf, sizeof(buf), "STATUS:%s\n", status);
    int sent = send(sock, buf, len, 0);
    return sent == len;
}

static bool tcp_send_audio_frame(int sock, const int16_t *pcm, size_t num_samples) {
    uint16_t pcm_bytes = num_samples * sizeof(int16_t);

    // Build frame: [magic 2B] [length 2B] [PCM data]
    uint8_t header[4];
    header[0] = FRAME_MAGIC_0;
    header[1] = FRAME_MAGIC_1;
    header[2] = (uint8_t)(pcm_bytes & 0xFF);
    header[3] = (uint8_t)((pcm_bytes >> 8) & 0xFF);

    // Send header + data
    if (send(sock, header, 4, 0) != 4) return false;
    if (send(sock, pcm, pcm_bytes, 0) != pcm_bytes) return false;
    return true;
}

// Read a line from TCP (blocking, with timeout via select)
static int tcp_read_line(int sock, char *buf, size_t bufsize, int timeout_ms) {
    size_t pos = 0;
    int64_t deadline = esp_timer_get_time() / 1000 + timeout_ms;

    while (pos < bufsize - 1) {
        int64_t remaining = deadline - (esp_timer_get_time() / 1000);
        if (remaining <= 0) return 0;  // timeout, not error

        fd_set readfds;
        FD_ZERO(&readfds);
        FD_SET(sock, &readfds);

        struct timeval tv;
        tv.tv_sec = remaining / 1000;
        tv.tv_usec = (remaining % 1000) * 1000;

        int sel = select(sock + 1, &readfds, NULL, NULL, &tv);
        if (sel < 0) return -1;
        if (sel == 0) return 0;

        char c;
        int r = recv(sock, &c, 1, 0);
        if (r <= 0) return -1;  // disconnected

        if (c == '\n') {
            buf[pos] = '\0';
            return (int)pos;
        }
        buf[pos++] = c;
    }
    buf[pos] = '\0';
    return (int)pos;
}

// Non-blocking check if data is available
static bool tcp_has_data(int sock) {
    fd_set readfds;
    FD_ZERO(&readfds);
    FD_SET(sock, &readfds);

    struct timeval tv = {0, 0};  // instant check
    return select(sock + 1, &readfds, NULL, NULL, &tv) > 0;
}

// =============================================================================
// BEEP (blocking, inline sine generation via codec)
// =============================================================================

static void play_beep(void) {
    int16_t beep_buf[BEEP_SAMPLES];
    for (int i = 0; i < BEEP_SAMPLES; i++) {
        float t = (float)i / SAMPLE_RATE;
        // Fade in/out (first/last 10ms)
        float env = 1.0f;
        int fade = SAMPLE_RATE / 100;  // 160 samples = 10ms
        if (i < fade) {
            env = (float)i / fade;
        } else if (i > BEEP_SAMPLES - fade) {
            env = (float)(BEEP_SAMPLES - i) / fade;
        }
        beep_buf[i] = (int16_t)(sinf(2.0f * M_PI * BEEP_FREQ_HZ * t) * BEEP_AMPLITUDE * env);
    }

    // Play in blocks
    size_t offset = 0;
    while (offset < BEEP_SAMPLES) {
        size_t block = BEEP_SAMPLES - offset;
        if (block > 256) block = 256;
        jarvis_codec_write(&beep_buf[offset], block);
        offset += block;
    }

    // Flush with silence
    int16_t silence[256] = {0};
    jarvis_codec_write(silence, 256);
}

// =============================================================================
// RECORDING LOOP
// =============================================================================

static void recording_session(int sock) {
    int16_t raw_buf[FRAME_SAMPLES];
    int16_t amp_buf[FRAME_SAMPLES];

    ESP_LOGI(TAG, "Streaming audio (gain=%dx)...", SOFTWARE_GAIN);
    tcp_send_status(sock, "recording");

    uint32_t frames_sent = 0;

    while (1) {
        // Check for STOP command (non-blocking)
        if (tcp_has_data(sock)) {
            char cmd[32];
            int len = tcp_read_line(sock, cmd, sizeof(cmd), 100);
            if (len < 0) {
                ESP_LOGW(TAG, "Connection lost during recording");
                return;
            }
            if (len > 0 && strncmp(cmd, "STOP", 4) == 0) {
                ESP_LOGI(TAG, "STOP received, frames sent: %lu", (unsigned long)frames_sent);
                break;
            }
        }

        // Read from microphone
        int samples = jarvis_codec_read(raw_buf, FRAME_SAMPLES);
        if (samples <= 0) {
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        // Apply software gain with saturation (same as JARVIS streaming path)
        for (int i = 0; i < samples; i++) {
            int32_t amplified = (int32_t)raw_buf[i] * SOFTWARE_GAIN;
            if (amplified > 32767) amplified = 32767;
            if (amplified < -32768) amplified = -32768;
            amp_buf[i] = (int16_t)amplified;
        }

        // Send over TCP
        if (!tcp_send_audio_frame(sock, amp_buf, samples)) {
            ESP_LOGW(TAG, "Send failed, connection lost");
            return;
        }

        frames_sent++;
    }

    tcp_send_status(sock, "idle");
}

// =============================================================================
// MAIN
// =============================================================================

void app_main(void) {
    ESP_LOGI(TAG, "=== AtomS3R Audio Recorder ===");

    // 1. NVS (required for WiFi)
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // 2. WiFi
    if (!init_wifi()) {
        ESP_LOGE(TAG, "WiFi failed, rebooting in 5s...");
        vTaskDelay(pdMS_TO_TICKS(5000));
        esp_restart();
    }

    // 3. Codec (ES8311 + I2S + amplifier)
    if (!jarvis_codec_init()) {
        ESP_LOGE(TAG, "Codec init failed, rebooting in 5s...");
        vTaskDelay(pdMS_TO_TICKS(5000));
        esp_restart();
    }
    ESP_LOGI(TAG, "Codec ready (ES8311, PGA +30dB, ADC +18dB, Vol +16dB)");

    // 4. Main loop: connect → command loop → reconnect
    while (1) {
        int sock = tcp_connect();
        if (sock < 0) {
            ESP_LOGW(TAG, "TCP connect failed, retry in 3s...");
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }

        tcp_sock = sock;
        tcp_send_status(sock, "connected");
        ESP_LOGI(TAG, "Waiting for commands...");

        // Command loop
        bool connected = true;
        while (connected) {
            char cmd[32];
            int len = tcp_read_line(sock, cmd, sizeof(cmd), 30000);

            if (len < 0) {
                ESP_LOGW(TAG, "Connection lost");
                connected = false;
            } else if (len == 0) {
                // Timeout — send keepalive
                tcp_send_status(sock, "idle");
            } else if (strncmp(cmd, "REC", 3) == 0) {
                ESP_LOGI(TAG, "REC command → beep + record");

                // Play beep (blocking)
                play_beep();

                // Small pause after beep to avoid capturing tail
                vTaskDelay(pdMS_TO_TICKS(50));

                // Drain stale I2S samples
                int16_t drain[320];
                for (int i = 0; i < 10; i++) {
                    jarvis_codec_read(drain, 320);
                }

                // Start recording
                recording_session(sock);
            } else {
                ESP_LOGD(TAG, "Unknown command: '%s'", cmd);
            }
        }

        close(sock);
        tcp_sock = -1;
        ESP_LOGI(TAG, "Disconnected, reconnecting in 2s...");
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
