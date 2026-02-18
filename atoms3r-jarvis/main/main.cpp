/**
 * =============================================================================
 * JARVIS AtomS3R - Main Application (ESP-IDF C++)
 * =============================================================================
 *
 * Firmware per AtomS3R che:
 * - Legge MAC address come device_id
 * - Recupera configurazione dal server (friendly_name, location)
 * - Mantiene WebSocket persistente verso il server (audio + controllo)
 * - Ascolta wake word "Jarvis" (microWakeWord TFLite)
 * - Audio streaming via Opus su WebSocket persistente
 * - Supporta trigger_listen dal server (multi-turn, enrollment)
 * - Mostra stato su display TFT 128x128
 * - Gestisce DND mode con click sul display
 *
 * Hardware: M5Stack AtomS3R (ESP32-S3-PICO-1-N8R8)
 * - ESP32-S3 + 8MB PSRAM OPI
 * - Display TFT 128x128 (ST7789)
 * - Atomic Echo Base: ES8311 codec + NS4150B amp
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
#include "jarvis_codec.h"
#include "jarvis_audio.h"
#include "jarvis_network.h"
#include "jarvis_speaker.h"
#include "jarvis_ws_audio.h"
}

static const char *TAG = "JARVIS";

// Firmware version
#define FIRMWARE_VERSION "5.0.0-ws"

// =============================================================================
// GLOBAL STATE
// =============================================================================

static device_state_t current_state = STATE_IDLE;
static bool dnd_mode = false;

// Device configuration (from server)
static device_config_t device_config = {};
static bool config_loaded = false;

// Timing
static int64_t last_display_update = 0;
static int64_t last_temp_fetch = 0;
// busy_state_start removed — no more HTTP polling, state comes via WS

// Cached data
static float cached_temperature = -99.0f;
static int current_hour = 0;
static int current_minute = 0;

// Error state auto-clear
static int64_t error_clear_time = 0;

// Screensaver
static int64_t last_activity_time = 0;
static bool screensaver_active = false;
static int64_t screensaver_start_time = 0;

// Deferred display update flag (avoids SPI race between ws_audio task and main task)
static volatile bool display_state_dirty = false;
static volatile device_state_t display_state_value = STATE_IDLE;

// Deferred wake word flag (wake callback runs in afe_detect_task, must not block it)
static volatile bool wake_word_pending = false;

// Deferred remote-trigger listen flag (set by WS control callback, processed in main_task)
static volatile bool remote_trigger_pending = false;
static volatile bool remote_trigger_silent = true;

// Deferred tts_done flag (set by WS tts_done callback, processed in main_task)
static volatile bool tts_done_pending = false;

// Deferred config_update flag (set by WS config_update callback, processed in main_task)
static volatile bool config_update_pending = false;
static volatile float config_new_sensitivity = 0.82f;

// Forward declarations
static void on_wake_word_detected(void);
static void activate_listening(bool silent);
static void handle_short_press(void);
static void handle_long_press(void);
static void handle_triple_tap(void);
static void on_session_done(bool success);
static void on_remote_trigger(bool silent);
static void on_tts_done(void);
static void on_config_update_ws(float wake_word_sensitivity);
static void reset_activity_timer(void);

// Helper: update state and notify server
static void set_state(device_state_t new_state) {
    reset_activity_timer();
    current_state = new_state;
    jarvis_display_set_state(new_state);

    // Notify server via persistent WS
    const char *state_str = "unknown";
    switch (new_state) {
        case STATE_IDLE:       state_str = "idle"; break;
        case STATE_LISTENING:  state_str = "listening"; break;
        case STATE_PROCESSING: state_str = "processing"; break;
        case STATE_BUSY:       state_str = "busy"; break;
        case STATE_DND:        state_str = "dnd"; break;
        case STATE_ERROR:      state_str = "error"; break;
    }
    jarvis_ws_audio_send_state(state_str);
}

// Helper: reset inactivity timer (exits screensaver if active)
static void reset_activity_timer(void) {
    last_activity_time = esp_timer_get_time() / 1000;
    if (screensaver_active) {
        screensaver_active = false;
        jarvis_display_screensaver_stop();
        jarvis_display_update();  // Restore IDLE display
        ESP_LOGI(TAG, "Screensaver exited (activity)");
    }
}

// =============================================================================
// SNTP TIME SYNC
// =============================================================================

static bool sntp_initialized = false;

static void init_sntp(void) {
    ESP_LOGI(TAG, "Initializing SNTP...");
    setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3", 1);
    tzset();
    esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, "pool.ntp.org");
    esp_sntp_setservername(1, "time.google.com");
    esp_sntp_init();
    sntp_initialized = true;
    ESP_LOGI(TAG, "SNTP initialized");
}

static void update_local_time(void) {
    time_t now;
    struct tm timeinfo;
    time(&now);
    localtime_r(&now, &timeinfo);
    if (timeinfo.tm_year > (2024 - 1900)) {
        current_hour = timeinfo.tm_hour;
        current_minute = timeinfo.tm_min;
    }
}

// =============================================================================
// BUTTON HANDLER (single tap, long press, triple-tap)
// =============================================================================
//
// State machine:
//   button_down → start hold timer
//   button_held > 800ms → LONG PRESS (DND toggle), consume gesture
//   button_up (< 800ms) → tap_count++
//     tap_count == 3 within 600ms → TRIPLE TAP (speaker stop)
//     no more taps within 300ms → SINGLE TAP (activate/stop listening)
//

#define LONG_PRESS_MS       800
#define MULTI_TAP_WINDOW_MS 300   // Max ms between consecutive taps
#define TRIPLE_TAP_COUNT    3
#define TAP_DEBOUNCE_MS     50    // Ignore taps shorter than this

static bool button_was_pressed = false;
static int64_t button_press_start = 0;
static bool long_press_handled = false;

// Multi-tap state
static int tap_count = 0;
static int64_t first_tap_time = 0;
static int64_t last_tap_time = 0;

// Deferred single-tap flag (processed in main_task after MULTI_TAP_WINDOW_MS)
static volatile bool single_tap_pending = false;
static int64_t single_tap_deadline = 0;

static void handle_triple_tap(void);

static void handle_button_down(void) {
    reset_activity_timer();
    if (!button_was_pressed) {
        button_was_pressed = true;
        button_press_start = esp_timer_get_time() / 1000;
        long_press_handled = false;
    }
    if (!long_press_handled) {
        int64_t held_ms = (esp_timer_get_time() / 1000) - button_press_start;
        if (held_ms >= LONG_PRESS_MS) {
            long_press_handled = true;
            // Long press cancels any pending tap sequence
            tap_count = 0;
            single_tap_pending = false;
            handle_long_press();
        }
    }
}

static void handle_button_up(void) {
    if (!button_was_pressed) return;
    int64_t held_ms = (esp_timer_get_time() / 1000) - button_press_start;
    button_was_pressed = false;
    if (long_press_handled) return;
    if (held_ms < TAP_DEBOUNCE_MS) return;

    int64_t now = esp_timer_get_time() / 1000;

    // Check if this tap is within the multi-tap window
    if (tap_count > 0 && (now - last_tap_time) > MULTI_TAP_WINDOW_MS) {
        // Previous tap sequence expired — that was a single tap we missed
        // (shouldn't happen because main_task processes the deadline, but safety)
        tap_count = 0;
    }

    tap_count++;
    last_tap_time = now;
    if (tap_count == 1) {
        first_tap_time = now;
    }

    if (tap_count >= TRIPLE_TAP_COUNT) {
        // Triple tap detected!
        ESP_LOGI(TAG, ">>> TRIPLE TAP DETECTED! <<< (speaker stop)");
        tap_count = 0;
        single_tap_pending = false;
        handle_triple_tap();
        return;
    }

    // Set deadline for single-tap (wait for more taps)
    single_tap_pending = true;
    single_tap_deadline = now + MULTI_TAP_WINDOW_MS;
}

// Minimum time (ms) to wait after activation before allowing button-stop.
static int64_t activation_time = 0;
#define MIN_SESSION_LIFETIME_MS  1000

static void handle_short_press(void) {
    ESP_LOGI(TAG, "Short press detected!");

    // If WS audio session is active, stop it (but only after minimum lifetime)
    if (jarvis_ws_audio_is_active()) {
        int64_t now = esp_timer_get_time() / 1000;
        int64_t elapsed = now - activation_time;
        if (elapsed < MIN_SESSION_LIFETIME_MS) {
            ESP_LOGW(TAG, "Button ignored - WS session in progress (%lldms < %dms)",
                     elapsed, MIN_SESSION_LIFETIME_MS);
            return;
        }
        ESP_LOGI(TAG, "Button during WS session - stopping");
        jarvis_ws_audio_stop_session();
        return;
    }

    // If in DND, exit DND first then activate
    if (dnd_mode) {
        dnd_mode = false;
        ESP_LOGI(TAG, "DND mode DISABLED (via button activate)");
        jarvis_audio_start_listening();
        jarvis_network_notify_dnd(device_config.device_id, false);
    }

    // If idle/busy/dnd/error/listening(stuck) -> manual activation
    if (current_state == STATE_IDLE || current_state == STATE_DND ||
        current_state == STATE_BUSY || current_state == STATE_ERROR ||
        (current_state == STATE_LISTENING && !jarvis_ws_audio_is_active())) {
        ESP_LOGI(TAG, ">>> MANUAL ACTIVATION (button) <<< (from state=%d)", current_state);
        activate_listening(false);  // Button press = not silent (play wake sound)
    }
}

static void handle_long_press(void) {
    ESP_LOGI(TAG, "Long press detected!");

    if (jarvis_ws_audio_is_active()) {
        int64_t now = esp_timer_get_time() / 1000;
        int64_t elapsed = now - activation_time;
        if (elapsed < MIN_SESSION_LIFETIME_MS) {
            ESP_LOGW(TAG, "Long press ignored - WS session in progress (%lldms < %dms)",
                     elapsed, MIN_SESSION_LIFETIME_MS);
            return;
        }
        ESP_LOGI(TAG, "Long press during WS session - stopping");
        jarvis_ws_audio_stop_session();
        return;
    }

    // Toggle DND mode
    dnd_mode = !dnd_mode;
    if (dnd_mode) {
        ESP_LOGI(TAG, "DND mode ENABLED");
        jarvis_audio_stop_listening();
        set_state(STATE_DND);
        jarvis_network_notify_dnd(device_config.device_id, true);
    } else {
        ESP_LOGI(TAG, "DND mode DISABLED");
        jarvis_audio_start_listening();
        set_state(STATE_IDLE);
        jarvis_network_notify_dnd(device_config.device_id, false);
    }
}

static void handle_triple_tap(void) {
    ESP_LOGI(TAG, "🛑 TRIPLE TAP — SPEAKER STOP");

    // Flash red for visual feedback
    jarvis_display_flash_red();

    // Send speaker_stop to server via persistent WS
    jarvis_ws_audio_send_speaker_stop();

    // If we were in BUSY (waiting for TTS), go back to IDLE
    if (current_state == STATE_BUSY) {
        set_state(dnd_mode ? STATE_DND : STATE_IDLE);
    }
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
    }
    if (location_id && strcmp(device_config.location_id, location_id) != 0) {
        strncpy(device_config.location_id, location_id, sizeof(device_config.location_id) - 1);
        changed = true;
    }
    if (changed) {
        jarvis_display_set_friendly_name(device_config.friendly_name);
        jarvis_display_update();
    }
}

// =============================================================================
// SPEAKER SUPPRESS TASK
// =============================================================================

static void suppress_speaker_task(void* arg) {
    char* device_id = (char*)arg;
    jarvis_network_suppress_speaker(device_id);
    vTaskDelete(NULL);
}

// =============================================================================
// ACTIVATION HANDLER (shared by wake word, button, and remote trigger)
// =============================================================================

static void activate_listening(bool silent) {
    if (dnd_mode) {
        ESP_LOGI(TAG, "Activation ignored (DND mode)");
        jarvis_ws_audio_send_state("dnd");
        return;
    }

    // Guard: don't activate if already in a session
    if (current_state == STATE_LISTENING || current_state == STATE_PROCESSING) {
        ESP_LOGW(TAG, "Activation ignored (already in state=%d)", current_state);
        return;
    }
    if (jarvis_ws_audio_is_active()) {
        ESP_LOGW(TAG, "Activation ignored (WS audio session active)");
        return;
    }

    ESP_LOGI(TAG, ">>> ACTIVATING (silent=%d) <<<", silent);

    if (!silent) {
        // Non-silent: flash + wake sound + speaker suppress
        jarvis_display_flash_white();
        jarvis_speaker_play_wake_sound();

        xTaskCreatePinnedToCore(
            suppress_speaker_task, "suppress_spk", 4096,
            (void*)device_config.device_id, 3, NULL, 1
        );

        jarvis_speaker_wait_done(500);
    }
    // Silent mode (multi-turn): skip wake sound + speaker suppress
    // User is already in conversation, speaker just finished TTS

    // Transition to LISTENING state
    set_state(STATE_LISTENING);

    // Stop wake word detection, enable raw audio streaming to ring buffer
    jarvis_audio_stop_listening();
    jarvis_audio_set_streaming(true);

    // Start audio session on persistent WS
    activation_time = esp_timer_get_time() / 1000;

    if (!jarvis_ws_audio_start_session()) {
        ESP_LOGE(TAG, "Failed to start WS audio session");
        jarvis_audio_set_streaming(false);
        set_state(STATE_ERROR);
        jarvis_display_set_error("WS audio failed");
        error_clear_time = esp_timer_get_time() / 1000 + 2000;
        jarvis_audio_start_listening();
    }
}

static void on_wake_word_detected(void) {
    ESP_LOGI(TAG, ">>> WAKE WORD 'JARVIS' DETECTED! <<<");
    activate_listening(false);  // Wake word = not silent (play sound)
}

// =============================================================================
// REMOTE TRIGGER CALLBACK (called from WS task context — lightweight!)
// =============================================================================

static void on_remote_trigger(bool silent) {
    remote_trigger_pending = true;
    remote_trigger_silent = silent;
}

// =============================================================================
// TTS DONE CALLBACK (called from WS task — set flag, main_task processes)
// =============================================================================

static void on_tts_done(void) {
    tts_done_pending = true;
}

// =============================================================================
// CONFIG UPDATE CALLBACK (called from WS task — set flag, main_task processes)
// =============================================================================

static void on_config_update_ws(float wake_word_sensitivity) {
    config_new_sensitivity = wake_word_sensitivity;
    config_update_pending = true;
}

// =============================================================================
// WS AUDIO SESSION DONE CALLBACK
// =============================================================================

static void on_session_done(bool success) {
    ESP_LOGI(TAG, "WS audio session done: %s (was state=%d)", success ? "OK" : "FAIL", current_state);

    // Stop streaming ring buffer
    jarvis_audio_set_streaming(false);

    // NOTE: This callback runs in ws_audio task (Core 0, pri 6).
    // Do NOT call jarvis_display_* here — it causes SPI bus race with main_task.
    // Just set state + dirty flag; main_task will update display safely.
    if (success) {
        current_state = STATE_BUSY;
        display_state_value = STATE_BUSY;
        ESP_LOGI(TAG, "State -> BUSY (display_dirty=true)");
    } else {
        current_state = STATE_ERROR;
        display_state_value = STATE_ERROR;
        error_clear_time = esp_timer_get_time() / 1000 + 2000;
        ESP_LOGI(TAG, "State -> ERROR (display_dirty=true)");
    }
    display_state_dirty = true;

    // Send state to server (safe from any context — just sets a JSON send flag)
    jarvis_ws_audio_send_state(success ? "busy" : "error");

    // Re-enable wake word listening
    if (!dnd_mode) {
        jarvis_audio_start_listening();
    }
}

// =============================================================================
// NETWORK CALLBACKS
// =============================================================================

static void on_server_response(bool success, const char* message) {
    ESP_LOGI(TAG, "Server response: %s - %s", success ? "OK" : "FAIL", message);
}

// on_busy_state removed — no more HTTP polling, state transitions via WS

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
// HEARTBEAT TASK (still uses HTTP for backward compat — config updates)
// =============================================================================

static void heartbeat_task(void* arg) {
    ESP_LOGI(TAG, "Heartbeat task started");
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));
        if (!jarvis_network_is_connected()) continue;
        if (jarvis_ws_audio_is_active()) continue;

        device_config_t new_config = {};
        if (jarvis_network_send_heartbeat(device_config.device_id, FIRMWARE_VERSION, &new_config)) {
            if (new_config.friendly_name[0] != '\0' &&
                strcmp(new_config.friendly_name, device_config.friendly_name) != 0) {
                on_config_update(new_config.friendly_name, new_config.location_id);
            }
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

        // Check button
        if (gpio_get_level((gpio_num_t)BUTTON_PIN) == 0) {
            handle_button_down();
        } else {
            handle_button_up();
        }

        // Deferred single-tap: fire after MULTI_TAP_WINDOW_MS with no more taps
        if (single_tap_pending && !button_was_pressed && now >= single_tap_deadline) {
            single_tap_pending = false;
            tap_count = 0;
            handle_short_press();
        }

        // Process audio (wake word detection — now a no-op, detection via flag)
        jarvis_audio_process();

        // Deferred wake word activation (set by afe_detect_task callback)
        if (wake_word_pending) {
            wake_word_pending = false;
            reset_activity_timer();
            ESP_LOGI(TAG, "Wake word flag processed by main_task");
            on_wake_word_detected();
        }

        // Deferred remote trigger activation (set by WS control callback)
        if (remote_trigger_pending) {
            remote_trigger_pending = false;
            reset_activity_timer();
            bool silent = remote_trigger_silent;
            ESP_LOGI(TAG, "Remote trigger flag processed by main_task (silent=%d)", silent);
            activate_listening(silent);
        }

        // Deferred tts_done (server finished TTS playback → return to IDLE)
        if (tts_done_pending) {
            tts_done_pending = false;
            if (current_state == STATE_BUSY) {
                ESP_LOGI(TAG, "TTS done received - BUSY -> IDLE");
                set_state(dnd_mode ? STATE_DND : STATE_IDLE);
            } else {
                ESP_LOGD(TAG, "TTS done received but state=%d (ignored)", current_state);
            }
        }

        // Deferred config_update (server pushed new sensitivity via WS)
        if (config_update_pending) {
            config_update_pending = false;
            float new_sens = config_new_sensitivity;
            ESP_LOGI(TAG, "Config update: wake_word_sensitivity=%.2f", new_sens);
            jarvis_audio_set_sensitivity(new_sens);
        }

        // Deferred display state update (from callbacks running in other tasks)
        if (display_state_dirty) {
            display_state_dirty = false;
            reset_activity_timer();
            current_state = display_state_value;
            ESP_LOGI(TAG, "Display dirty: setting state=%d", current_state);
            jarvis_display_set_state(current_state);
            if (current_state == STATE_ERROR) {
                jarvis_display_set_error("Session error");
            }
            jarvis_display_update();
        }

        // Screensaver: activate after inactivity (only in IDLE, not DND)
        if (!screensaver_active && current_state == STATE_IDLE &&
            last_activity_time > 0 && (now - last_activity_time > SCREENSAVER_TIMEOUT_MS)) {
            screensaver_active = true;
            screensaver_start_time = now;
            jarvis_display_screensaver_start();
            ESP_LOGI(TAG, "Screensaver activated (inactivity %llds)", (now - last_activity_time) / 1000);
        }

        // Screensaver: tick animation or auto-exit after duration
        if (screensaver_active) {
            if (now - screensaver_start_time > SCREENSAVER_DURATION_MS) {
                // Duration expired — exit screensaver, return to IDLE
                screensaver_active = false;
                jarvis_display_screensaver_stop();
                last_activity_time = now;  // Reset timer to prevent immediate re-trigger
                jarvis_display_update();
                ESP_LOGI(TAG, "Screensaver finished (duration)");
            } else {
                jarvis_display_screensaver_tick();
            }
        }

        // Update display in IDLE/DND (skip if screensaver is running)
        if (!screensaver_active && (current_state == STATE_IDLE || current_state == STATE_DND)) {
            if (now - last_display_update > DISPLAY_UPDATE_IDLE_MS) {
                last_display_update = now;
                update_local_time();
                jarvis_display_set_time(current_hour, current_minute);
                jarvis_display_set_temperature(cached_temperature);
                jarvis_display_update();
            }
        }

        // Continuous display update for animations
        if (current_state == STATE_LISTENING || current_state == STATE_PROCESSING) {
            jarvis_display_update();
        }

        // Busy state safety timeout (in case server never responds)
        // Note: with persistent WS, state transitions come via WebSocket,
        // but we keep a generous timeout as safety net
        static int64_t busy_entered_at = 0;
        if (current_state == STATE_BUSY) {
            if (busy_entered_at == 0) busy_entered_at = now;
            if (now - busy_entered_at > BUSY_STATE_TIMEOUT_MS) {
                ESP_LOGW(TAG, "BUSY safety timeout - returning to IDLE");
                set_state(dnd_mode ? STATE_DND : STATE_IDLE);
                busy_entered_at = 0;
            }
        } else {
            busy_entered_at = 0;
        }

        // Error state auto-clear
        if (current_state == STATE_ERROR && error_clear_time > 0 && now >= error_clear_time) {
            ESP_LOGI(TAG, "Error display timeout - returning to IDLE");
            error_clear_time = 0;
            set_state(dnd_mode ? STATE_DND : STATE_IDLE);
        }

        // Fetch temperature periodically
        if (now - last_temp_fetch > TEMP_REFRESH_MS) {
            last_temp_fetch = now;
            if (!jarvis_ws_audio_is_active() && device_config.friendly_name[0] != '\0') {
                float new_temp;
                if (jarvis_network_fetch_temperature(device_config.friendly_name, &new_temp)) {
                    cached_temperature = new_temp;
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
    ESP_LOGI(TAG, "(Persistent WS + Opus + microWakeWord)");
    ESP_LOGI(TAG, "=================================");

    // Initialize NVS
    init_nvs();
    vTaskDelay(pdMS_TO_TICKS(10));

    // Initialize button
    init_button();

    // Initialize display
    ESP_LOGI(TAG, "Initializing display...");
    if (!jarvis_display_init()) {
        ESP_LOGE(TAG, "Display init failed - continuing without display");
    }
    vTaskDelay(pdMS_TO_TICKS(50));

    jarvis_display_set_state(STATE_IDLE);
    jarvis_display_set_temperature(-99);
    jarvis_display_set_time(0, 0);
    jarvis_display_update();
    jarvis_display_show_message("Connecting...");
    vTaskDelay(pdMS_TO_TICKS(10));

    // Initialize network (WiFi)
    if (!jarvis_network_init()) {
        jarvis_display_show_message("WiFi FAILED");
        ESP_LOGE(TAG, "WiFi failed - halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    vTaskDelay(pdMS_TO_TICKS(10));

    // Get device MAC address
    if (!jarvis_network_get_device_id(device_config.device_id)) {
        jarvis_display_show_message("MAC ERROR");
        ESP_LOGE(TAG, "Failed to get MAC address - halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ESP_LOGI(TAG, "Device ID (MAC): %s", device_config.device_id);

    // Initialize codec (I2S, ES8311, amplifier) - MUST be before audio/speaker
    ESP_LOGI(TAG, "Initializing codec...");
    if (!jarvis_codec_init()) {
        jarvis_display_show_message("CODEC FAILED");
        ESP_LOGE(TAG, "Codec init failed - halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    vTaskDelay(pdMS_TO_TICKS(10));

    // Initialize SNTP
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
    vTaskDelay(pdMS_TO_TICKS(10));

    // Set network callbacks
    jarvis_network_set_callbacks(on_server_response, NULL);  // No busy polling — state via WS
    jarvis_network_set_config_callback(on_config_update);

    // Fetch initial temperature
    if (device_config.friendly_name[0] != '\0' && device_config.is_configured) {
        if (jarvis_network_fetch_temperature(device_config.friendly_name, &cached_temperature)) {
            ESP_LOGI(TAG, "Initial temperature: %.1f", cached_temperature);
        }
    }
    vTaskDelay(pdMS_TO_TICKS(10));

    // Initialize audio module (microWakeWord TFLite + ring buffer)
    ESP_LOGI(TAG, "Initializing audio + microWakeWord...");
    if (!jarvis_audio_init()) {
        jarvis_display_show_message("MIC FAILED");
        ESP_LOGE(TAG, "Audio init failed - halting");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    vTaskDelay(pdMS_TO_TICKS(50));

    // Initialize speaker (uses codec for I2S TX)
    if (!jarvis_speaker_init()) {
        ESP_LOGW(TAG, "Speaker init failed - wake sound feedback disabled");
    }
    vTaskDelay(pdMS_TO_TICKS(10));

    // Initialize WS Audio module (Opus encoder/decoder)
    if (!jarvis_ws_audio_init()) {
        ESP_LOGW(TAG, "WS Audio init failed - voice streaming disabled");
    }
    vTaskDelay(pdMS_TO_TICKS(10));

    // Start persistent WebSocket connection (audio + control on single channel)
    ESP_LOGI(TAG, "Starting persistent WebSocket connection...");
    if (!jarvis_ws_audio_connect(device_config.device_id, on_session_done, on_remote_trigger)) {
        ESP_LOGW(TAG, "Persistent WS connect failed - will retry in background");
    }
    vTaskDelay(pdMS_TO_TICKS(10));

    // Register tts_done callback (server signals TTS playback complete → BUSY → IDLE)
    jarvis_ws_audio_set_tts_done_callback(on_tts_done);

    // Register config_update callback (server pushes new sensitivity from dashboard)
    jarvis_ws_audio_set_config_update_callback(on_config_update_ws);

    // Set wake word callback (lightweight — just sets flag, main_task processes it)
    jarvis_audio_set_wake_callback([]() {
        wake_word_pending = true;
    });

    // Start listening
    jarvis_audio_start_listening();

    // Update display
    jarvis_display_set_time(current_hour, current_minute);
    jarvis_display_set_temperature(cached_temperature);
    jarvis_display_set_state(STATE_IDLE);

    ESP_LOGI(TAG, "=================================");
    ESP_LOGI(TAG, "JARVIS AtomS3R Ready!");
    ESP_LOGI(TAG, "Device ID: %s", device_config.device_id);
    if (device_config.is_configured) {
        ESP_LOGI(TAG, "Room: %s", device_config.friendly_name);
    }
    ESP_LOGI(TAG, "Say 'Jarvis' to activate");
    ESP_LOGI(TAG, "=================================");

    // Initialize screensaver inactivity timer
    last_activity_time = esp_timer_get_time() / 1000;

    // Create heartbeat task (still uses HTTP for config updates)
    xTaskCreatePinnedToCore(heartbeat_task, "heartbeat_task", 4096, NULL, 3, NULL, 1);

    // Create main task
    xTaskCreatePinnedToCore(main_task, "main_task", 8192, NULL, 5, NULL, 0);
}
