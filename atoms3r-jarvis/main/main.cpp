/**
 * =============================================================================
 * JARVIS AtomS3R - Main Application (ESP-IDF C++)
 * =============================================================================
 *
 * Firmware per AtomS3R che:
 * - Legge MAC address come device_id
 * - Recupera configurazione dal server (friendly_name, location)
 * - Ascolta wake word "Jarvis" (ESP-SR WakeNet)
 * - Streaming audio con VAD
 * - Mostra stato su display TFT 128x128
 * - Gestisce DND mode con click sul display
 * - Invia heartbeat periodico al server
 *
 * Hardware: M5Stack AtomS3R (ESP32-S3-PICO-1-N8R8)
 * - ESP32-S3 + 8MB PSRAM OPI
 * - Display TFT 128x128 (ST7789)
 * - Microfono PDM
 * - Bottone (GPIO41)
 */

#include <cstdio>
#include <cstring>
#include <ctime>

extern "C" {
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sntp.h"
#include "nvs_flash.h"
#include "driver/gpio.h"

#include "jarvis_config.h"
#include "jarvis_display.h"
#include "jarvis_audio.h"
#include "jarvis_network.h"
#include "jarvis_speaker.h"
}

static const char *TAG = "JARVIS";

// Firmware version
#define FIRMWARE_VERSION "2.0.0"

// =============================================================================
// GLOBAL STATE
// =============================================================================

static device_state_t current_state = STATE_IDLE;
static bool dnd_mode = false;
static bool streaming_active = false;

// Device configuration (from server)
static device_config_t device_config = {};
static bool config_loaded = false;

// Timing
static int64_t last_display_update = 0;
static int64_t last_temp_fetch = 0;
static int64_t last_button_press = 0;
static int64_t busy_state_start = 0;
static int64_t last_busy_poll = 0;
static int64_t last_heartbeat = 0;

// Cached data
static float cached_temperature = -99.0f;
static int current_hour = 0;
static int current_minute = 0;

// Forward declarations (per risolvere dipendenze circolari tra button handler e audio callbacks)
static void on_wake_word_detected(void);
static void handle_short_press(void);
static void handle_long_press(void);

// =============================================================================
// SNTP TIME SYNC
// =============================================================================

static bool sntp_initialized = false;

static void init_sntp(void) {
    ESP_LOGI(TAG, "Initializing SNTP...");

    // Timezone: CET-1CEST,M3.5.0,M10.5.0/3 = Central European Time con DST automatico
    setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3", 1);
    tzset();

    esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, "pool.ntp.org");
    esp_sntp_setservername(1, "time.google.com");
    esp_sntp_init();

    sntp_initialized = true;
    ESP_LOGI(TAG, "SNTP initialized (pool.ntp.org + time.google.com)");
}

static void update_local_time(void) {
    time_t now;
    struct tm timeinfo;
    time(&now);
    localtime_r(&now, &timeinfo);

    // Se l'anno è > 2024 vuol dire che SNTP ha sincronizzato
    if (timeinfo.tm_year > (2024 - 1900)) {
        current_hour = timeinfo.tm_hour;
        current_minute = timeinfo.tm_min;
    }
}

// =============================================================================
// BUTTON HANDLER (short press = manual activate, long press = DND toggle)
// =============================================================================

// Long press threshold (ms)
#define LONG_PRESS_MS 800

static void IRAM_ATTR button_isr_handler(void* arg) {
    // Just set a flag, handle in main loop
    BaseType_t xHigherPriorityTaskWoken = pdFALSE;
    // Could use a queue here for more robust handling
}

// Stato interno del pulsante
static bool button_was_pressed = false;
static int64_t button_press_start = 0;
static bool long_press_handled = false;

static void handle_button_down(void) {
    // Primo frame in cui il pulsante è premuto
    if (!button_was_pressed) {
        button_was_pressed = true;
        button_press_start = esp_timer_get_time() / 1000;
        long_press_handled = false;
    }

    // Controlla long press mentre il pulsante è ancora premuto
    if (!long_press_handled) {
        int64_t held_ms = (esp_timer_get_time() / 1000) - button_press_start;
        if (held_ms >= LONG_PRESS_MS) {
            long_press_handled = true;
            handle_long_press();
        }
    }
}

static void handle_button_up(void) {
    if (!button_was_pressed) return;

    int64_t held_ms = (esp_timer_get_time() / 1000) - button_press_start;
    button_was_pressed = false;

    // Se il long press è già stato gestito, ignora il rilascio
    if (long_press_handled) return;

    // Debounce: ignora press troppo brevi (< 50ms, probabilmente rumore)
    if (held_ms < 50) return;

    // Short press!
    handle_short_press();
}

static void handle_short_press(void) {
    ESP_LOGI(TAG, "🔘 Short press detected!");

    // Se in streaming, stop (comportamento esistente)
    if (jarvis_audio_is_streaming()) {
        ESP_LOGI(TAG, "Button during streaming - stopping");
        jarvis_audio_stop_streaming();
        return;
    }

    // Se in DND, esci prima da DND poi attiva
    if (dnd_mode) {
        dnd_mode = false;
        ESP_LOGI(TAG, "DND mode DISABLED (via button activate)");
        jarvis_audio_start_listening();
        jarvis_network_notify_dnd(device_config.device_id, false);
    }

    // Se idle/busy/dnd → attiva manualmente (stesso flusso della wake word)
    if (current_state == STATE_IDLE || current_state == STATE_DND ||
        current_state == STATE_BUSY || current_state == STATE_ERROR) {
        ESP_LOGI(TAG, ">>> MANUAL ACTIVATION (button) <<<");
        on_wake_word_detected();  // Riusa lo stesso flusso: flash + sound + suppress + listen
    }
}

static void handle_long_press(void) {
    ESP_LOGI(TAG, "🔘 Long press detected!");

    // Se in streaming, stop
    if (jarvis_audio_is_streaming()) {
        ESP_LOGI(TAG, "Long press during streaming - stopping");
        jarvis_audio_stop_streaming();
        return;
    }

    // Toggle DND mode
    dnd_mode = !dnd_mode;

    if (dnd_mode) {
        ESP_LOGI(TAG, "DND mode ENABLED");
        jarvis_audio_stop_listening();
        current_state = STATE_DND;
        jarvis_network_notify_dnd(device_config.device_id, true);
    } else {
        ESP_LOGI(TAG, "DND mode DISABLED");
        jarvis_audio_start_listening();
        current_state = STATE_IDLE;
        jarvis_network_notify_dnd(device_config.device_id, false);
    }

    jarvis_display_set_state(current_state);
}

// =============================================================================
// CONFIG UPDATE CALLBACK
// =============================================================================

static void on_config_update(const char* friendly_name, const char* location_id) {
    bool changed = false;

    if (friendly_name && strcmp(device_config.friendly_name, friendly_name) != 0) {
        strncpy(device_config.friendly_name, friendly_name, sizeof(device_config.friendly_name) - 1);
        device_config.is_configured = true;
        changed = true;
        ESP_LOGI(TAG, "Config update: friendly_name = %s", friendly_name);
    }

    if (location_id && strcmp(device_config.location_id, location_id) != 0) {
        strncpy(device_config.location_id, location_id, sizeof(device_config.location_id) - 1);
        changed = true;
        ESP_LOGI(TAG, "Config update: location_id = %s", location_id);
    }

    if (changed) {
        // Aggiorna display con nuovo friendly_name
        jarvis_display_set_friendly_name(device_config.friendly_name);
        jarvis_display_update();
    }
}

// =============================================================================
// SPEAKER SUPPRESS TASK (fire-and-forget, asincrono)
// =============================================================================

static void suppress_speaker_task(void* arg) {
    char* device_id = (char*)arg;
    ESP_LOGI(TAG, "🔉 Sending speaker suppress for %s", device_id);
    jarvis_network_suppress_speaker(device_id);
    ESP_LOGI(TAG, "🔉 Speaker suppress sent");
    vTaskDelete(NULL);
}

// =============================================================================
// AUDIO CALLBACKS
// =============================================================================

static void on_wake_word_detected(void) {
    if (dnd_mode) {
        ESP_LOGI(TAG, "Wake word ignored (DND mode)");
        return;
    }

    ESP_LOGI(TAG, ">>> WAKE WORD 'JARVIS' DETECTED! <<<");

    // 1. Flash bianco immediato (feedback visivo ~80ms)
    jarvis_display_flash_white();

    // 2. Suono feedback (non-bloccante, task separato)
    jarvis_speaker_play_wake_sound();

    // 3. Speaker suppress (fire-and-forget HTTP, task separato)
    //    Usa il device_id statico (vive per tutta la sessione)
    xTaskCreatePinnedToCore(
        suppress_speaker_task,
        "suppress_spk",
        4096,
        (void*)device_config.device_id,  // Puntatore a stringa statica
        3,      // Priorità media
        NULL,
        1       // Core 1
    );

    // 4. Transizione a stato LISTENING
    current_state = STATE_LISTENING;
    jarvis_display_set_state(STATE_LISTENING);

    // 5. Avvia registrazione audio
    jarvis_audio_stop_listening();
    streaming_active = true;
    jarvis_audio_start_streaming();
}

static bool on_stream_chunk(int16_t* chunk, size_t samples) {
    if (!jarvis_network_is_streaming()) {
        // Usa device_id (MAC address) invece di room
        if (!jarvis_network_start_stream(device_config.device_id)) {
            ESP_LOGE(TAG, "Failed to start network stream");
            return false;
        }
    }

    if (!jarvis_network_send_chunk(chunk, samples)) {
        ESP_LOGE(TAG, "Failed to send chunk");
        return false;
    }

    return true;
}

// Task separato per finalizzare lo stream (bloccante: aspetta risposta HTTP)
// Così il main loop continua a fare fetch() dall'AFE e non riempie il ringbuffer
static void stream_finalize_task(void* arg) {
    bool use_local_speaker = false;
    bool success = jarvis_network_end_stream(&use_local_speaker);

    if (success) {
        if (use_local_speaker) {
            ESP_LOGI(TAG, "Server requested local speaker playback");
            jarvis_display_show_message("Local audio");
            vTaskDelay(pdMS_TO_TICKS(500));
        }

        current_state = STATE_BUSY;
        jarvis_display_set_state(STATE_BUSY);
        busy_state_start = esp_timer_get_time() / 1000;
    } else {
        jarvis_display_set_error("Send failed");
        current_state = STATE_ERROR;
        jarvis_display_set_state(STATE_ERROR);
        vTaskDelay(pdMS_TO_TICKS(2000));
        current_state = STATE_IDLE;
        jarvis_display_set_state(STATE_IDLE);
    }

    streaming_active = false;
    jarvis_audio_start_listening();

    vTaskDelete(NULL);
}

static void on_stream_end(void) {
    ESP_LOGI(TAG, "Stream end callback - finalizing in background");

    current_state = STATE_PROCESSING;
    jarvis_display_set_state(STATE_PROCESSING);

    // Lancia il finalize in un task separato per non bloccare il main loop
    // Il main loop deve continuare a fare fetch() dall'AFE
    xTaskCreatePinnedToCore(
        stream_finalize_task,
        "stream_fin",
        4096,
        NULL,
        3,
        NULL,
        1  // Core 1
    );
}

// =============================================================================
// NETWORK CALLBACKS
// =============================================================================

static void on_server_response(bool success, const char* message) {
    ESP_LOGI(TAG, "Server response: %s - %s", success ? "OK" : "FAIL", message);
}

static void on_busy_state(bool busy) {
    ESP_LOGI(TAG, "Busy state update: %s", busy ? "SPEAKING" : "IDLE");

    if (busy) {
        if (current_state != STATE_BUSY && current_state != STATE_DND &&
            current_state != STATE_LISTENING && current_state != STATE_PROCESSING) {
            current_state = STATE_BUSY;
            jarvis_display_set_state(STATE_BUSY);
            busy_state_start = esp_timer_get_time() / 1000;
            ESP_LOGI(TAG, "Entering BUSY state - JARVIS is speaking");
        } else if (current_state == STATE_BUSY) {
            busy_state_start = esp_timer_get_time() / 1000;
        }
    } else {
        if (current_state == STATE_BUSY) {
            current_state = dnd_mode ? STATE_DND : STATE_IDLE;
            jarvis_display_set_state(current_state);
            ESP_LOGI(TAG, "Exiting BUSY state - JARVIS finished speaking");
        }
    }
}

// =============================================================================
// INITIALIZATION
// =============================================================================

static void init_button(void) {
    gpio_config_t io_conf = {};
    io_conf.pin_bit_mask = (1ULL << BUTTON_PIN);
    io_conf.mode = GPIO_MODE_INPUT;
    io_conf.pull_up_en = GPIO_PULLUP_ENABLE;
    io_conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io_conf.intr_type = GPIO_INTR_NEGEDGE;
    gpio_config(&io_conf);
}

static void init_nvs(void) {
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

// =============================================================================
// HEARTBEAT TASK
// =============================================================================

static void heartbeat_task(void* arg) {
    ESP_LOGI(TAG, "Heartbeat task started");

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));

        if (!jarvis_network_is_connected()) {
            ESP_LOGW(TAG, "Skipping heartbeat - WiFi not connected");
            continue;
        }

        if (jarvis_audio_is_streaming()) {
            ESP_LOGD(TAG, "Skipping heartbeat - streaming in progress");
            continue;
        }

        device_config_t new_config = {};
        if (jarvis_network_send_heartbeat(device_config.device_id, FIRMWARE_VERSION, &new_config)) {
            // Verifica se la config è cambiata
            if (new_config.friendly_name[0] != '\0' &&
                strcmp(new_config.friendly_name, device_config.friendly_name) != 0) {
                on_config_update(new_config.friendly_name, new_config.location_id);
            }
            ESP_LOGD(TAG, "Heartbeat sent successfully");
        } else {
            ESP_LOGW(TAG, "Heartbeat failed");
        }
    }
}

// =============================================================================
// MAIN TASK
// =============================================================================

static void main_task(void* arg) {
    ESP_LOGI(TAG, "Main task started");

    while (1) {
        int64_t now = esp_timer_get_time() / 1000;

        // Check button (short press / long press detection)
        if (gpio_get_level((gpio_num_t)BUTTON_PIN) == 0) {
            handle_button_down();
        } else {
            handle_button_up();
        }

        // Process audio
        jarvis_audio_process();

        // Update display in IDLE/DND
        if (current_state == STATE_IDLE || current_state == STATE_DND) {
            if (now - last_display_update > DISPLAY_UPDATE_IDLE_MS) {
                last_display_update = now;
                update_local_time();  // Legge ora corrente da RTC (sincronizzato via SNTP)
                jarvis_display_set_time(current_hour, current_minute);
                jarvis_display_set_temperature(cached_temperature);
                jarvis_display_update();
            }
        }

        // Continuous display update for animations
        if (current_state == STATE_LISTENING || current_state == STATE_PROCESSING) {
            jarvis_display_update();
        }

        // Busy state timeout
        if (current_state == STATE_BUSY) {
            if (now - busy_state_start > BUSY_STATE_TIMEOUT_MS) {
                ESP_LOGI(TAG, "BUSY timeout - returning to IDLE");
                current_state = dnd_mode ? STATE_DND : STATE_IDLE;
                jarvis_display_set_state(current_state);
            }
        }

        // Poll server state
        if (!dnd_mode && !jarvis_audio_is_streaming() &&
            current_state != STATE_LISTENING && current_state != STATE_PROCESSING) {
            if (now - last_busy_poll > BUSY_POLL_INTERVAL_MS) {
                last_busy_poll = now;
                jarvis_network_poll_state(device_config.device_id);
            }
        }

        // Fetch temperature periodically
        if (now - last_temp_fetch > TEMP_REFRESH_MS) {
            last_temp_fetch = now;
            if (!jarvis_audio_is_streaming() && device_config.friendly_name[0] != '\0') {
                float new_temp;
                if (jarvis_network_fetch_temperature(device_config.friendly_name, &new_temp)) {
                    cached_temperature = new_temp;
                    ESP_LOGI(TAG, "Temperature updated: %.1f°C", cached_temperature);
                }
            }
        }

        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

// =============================================================================
// APP MAIN
// =============================================================================

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "=================================");
    ESP_LOGI(TAG, "JARVIS AtomS3R Starting...");
    ESP_LOGI(TAG, "Firmware: %s", FIRMWARE_VERSION);
    ESP_LOGI(TAG, "(ESP-IDF + ESP-SR WakeNet)");
    ESP_LOGI(TAG, "=================================");

    // Initialize NVS
    init_nvs();
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield to WDT

    // Initialize button
    init_button();

    // Initialize display (SPI bus + panel reset/init — può richiedere tempo)
    ESP_LOGI(TAG, "Initializing display...");
    if (!jarvis_display_init()) {
        ESP_LOGE(TAG, "Display init failed - continuing without display");
    }
    vTaskDelay(pdMS_TO_TICKS(50));  // Yield dopo display init (pesante)

    jarvis_display_set_state(STATE_IDLE);
    jarvis_display_set_temperature(-99);
    jarvis_display_set_time(0, 0);
    jarvis_display_update();
    jarvis_display_show_message("Connecting...");
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield to WDT

    // Initialize network (WiFi)
    if (!jarvis_network_init()) {
        jarvis_display_show_message("WiFi FAILED");
        ESP_LOGE(TAG, "WiFi failed - halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield to WDT

    // Get device MAC address
    if (!jarvis_network_get_device_id(device_config.device_id)) {
        jarvis_display_show_message("MAC ERROR");
        ESP_LOGE(TAG, "Failed to get MAC address - halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    ESP_LOGI(TAG, "Device ID (MAC): %s", device_config.device_id);

    // Initialize SNTP for time sync (needs WiFi)
    init_sntp();

    jarvis_display_show_message("Fetching config...");

    // Fetch configuration from server
    if (jarvis_network_fetch_config(device_config.device_id, &device_config)) {
        config_loaded = true;
        if (device_config.is_configured) {
            ESP_LOGI(TAG, "Device configured: %s @ %s",
                     device_config.friendly_name, device_config.location_id);
            jarvis_display_set_friendly_name(device_config.friendly_name);
        } else {
            ESP_LOGW(TAG, "Device not configured on server");
            // Mostra MAC address sul display per facilitare la configurazione
            char msg[32];
            snprintf(msg, sizeof(msg), "MAC: %s", device_config.device_id);
            jarvis_display_show_message(msg);
            vTaskDelay(pdMS_TO_TICKS(3000));
            jarvis_display_set_friendly_name("Not configured");
        }
    } else {
        ESP_LOGW(TAG, "Failed to fetch config - using defaults");
        jarvis_display_set_friendly_name("Offline");
    }
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield to WDT

    // Set network callbacks
    jarvis_network_set_callbacks(on_server_response, on_busy_state);
    jarvis_network_set_config_callback(on_config_update);

    // Fetch initial temperature (se configurato)
    if (device_config.friendly_name[0] != '\0' && device_config.is_configured) {
        if (jarvis_network_fetch_temperature(device_config.friendly_name, &cached_temperature)) {
            ESP_LOGI(TAG, "Initial temperature: %.1f°C", cached_temperature);
        }
    }
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield prima di init audio pesante

    // Initialize audio with ESP-SR (PESANTE: carica modello WakeNet da SPIFFS)
    ESP_LOGI(TAG, "Initializing audio + WakeNet (may take a few seconds)...");
    if (!jarvis_audio_init()) {
        jarvis_display_show_message("MIC FAILED");
        ESP_LOGE(TAG, "Audio init failed - halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    vTaskDelay(pdMS_TO_TICKS(50));  // Yield dopo init audio pesante

    // Initialize speaker (Atomic SPK Base NS4168)
    if (!jarvis_speaker_init()) {
        ESP_LOGW(TAG, "Speaker init failed - wake sound feedback disabled");
        // Non è fatale: il device funziona senza speaker feedback
    }
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield to WDT

    // Set audio callbacks
    jarvis_audio_set_callbacks(on_wake_word_detected, on_stream_chunk, on_stream_end);

    // Start listening
    jarvis_audio_start_listening();

    // Update display with initial data
    jarvis_display_set_time(current_hour, current_minute);
    jarvis_display_set_temperature(cached_temperature);
    jarvis_display_set_state(STATE_IDLE);

    ESP_LOGI(TAG, "=================================");
    ESP_LOGI(TAG, "JARVIS AtomS3R Ready!");
    ESP_LOGI(TAG, "Device ID: %s", device_config.device_id);
    if (device_config.is_configured) {
        ESP_LOGI(TAG, "Room: %s", device_config.friendly_name);
        ESP_LOGI(TAG, "Location: %s", device_config.location_id);
    } else {
        ESP_LOGI(TAG, "Status: Not configured");
    }
    ESP_LOGI(TAG, "Say 'Jarvis' to activate");
    ESP_LOGI(TAG, "=================================");

    // Create heartbeat task
    xTaskCreatePinnedToCore(heartbeat_task, "heartbeat_task", 4096, NULL, 3, NULL, 1);

    // Create main task (stack 8KB per sicurezza)
    xTaskCreatePinnedToCore(main_task, "main_task", 8192, NULL, 5, NULL, 0);
}
