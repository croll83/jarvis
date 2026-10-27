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
#include "jarvis_duplex_policy.h"

#include <string.h>
#include <stdio.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_event.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
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

// Full-duplex live session (Voice Live Hub via wakeword-server)
#define DUPLEX_QUEUE_FRAMES         25      // Downlink jitter queue: 25 x 20ms = 500ms
#define DUPLEX_ENQUEUE_TIMEOUT_MS   40      // Short: the WS handler holds the client lock (mic sends wait)
#define DUPLEX_PREBUFFER_FRAMES     3       // Cushion before (re)starting playback (60ms)...
#define DUPLEX_PREBUFFER_MAX_MS     60      // ...or start anyway after this long
#define DUPLEX_UNDERRUN_MS          100     // Queue empty this long = talk spurt over, prebuffer again
#define DUPLEX_RING_MS              320     // I2S TX DMA ring: 8 x 640 frames @16kHz
#define DUPLEX_UNMUTE_MARGIN_MS     40      // Slack on the playback clock estimate (mute after clear)
#define DUPLEX_LATE_DROP_MS         1500    // Drop stray downlink frames after a local (button) end
#define DUPLEX_VU_STALE_MS          400     // "vu" older than this = user level 0
#define DUPLEX_WATCHDOG_MS          8000    // No "vu" this long = server side gone, end the session
#define DUPLEX_LEVEL_SLOTS          32      // Speaker level history (32 x 20ms > TX ring latency)
#define DUPLEX_LEVEL_WINDOW_MS      70      // Peak over this window per display frame
#define DUPLEX_PLAY_TASK_STACK      6144
#define DUPLEX_PLAY_TASK_PRIO       5
// AEC reference uplink (cap "aec_ref"): 0xA5 | format | seq u16 LE | 320 x [mic, ref] s16le.
// 0xA5 is never the TOC byte of our mono Opus frames (its stereo bit is set). PCM, not
// Opus: the echo canceller subtracts waveforms, a lossy reference would cap it.
// Format bit 7 = DAC muted during the packet (barge-in): the speaker is silent even if
// the loopback still carries the TX ring play-out, so the server drops the reference.
#define DUPLEX_REF_MAGIC            0xA5
#define DUPLEX_REF_FMT_PCM          0x01
#define DUPLEX_REF_FLAG_MUTED       0x80
#define DUPLEX_REF_HEADER           4
#define DUPLEX_REF_VALUES           (OPUS_FRAME_SAMPLES * 2)
// 64 KB/s fills the 5.7 KB lwIP send buffer in ~90 ms: a short WiFi stall must block the
// sender (the ring holds 500 ms of pairs), not time out, since esp_websocket_client drops
// the whole connection when a write times out
#define DUPLEX_REF_SEND_TIMEOUT_MS  1000

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

// Full-duplex live session state.
// The decoder is only ever used from the WS event handler (downlink, clear, start),
// the play task only reads the queue and writes the codec.
// Timestamps shared between tasks are uint32_t ms (atomic on Xtensa, wrap-safe compares).
typedef struct {
    uint16_t samples;
    uint32_t gen;       // duplex_clear_gen when queued (older = dropped by barge-in)
    int16_t pcm[BUFFER_SAMPLES];
} duplex_frame_t;

typedef struct {
    uint32_t play_ms;   // When this frame reaches the speaker (estimated)
    uint8_t level;
} duplex_level_t;

// Guards duplex_active / duplex_end_flush / duplex_session_gen / duplex_play_task_handle
// transitions (start, stop, play task exit) across the WS handler, ws_persist and main_task.
static portMUX_TYPE duplex_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool duplex_active = false;
static volatile bool duplex_end_flush = false;       // true = stop playback at once on exit
static volatile uint32_t duplex_session_gen = 0;     // Bumped on every duplex start
static volatile uint32_t duplex_clear_gen = 0;       // Bumped on every audio_clear
static volatile bool duplex_late_drop_armed = false;
static volatile uint32_t duplex_late_drop_until_ms = 0;
static volatile uint8_t duplex_mic_vu = 0;
static volatile uint32_t duplex_mic_vu_ms = 0;       // Last "vu" (session start until the first one)
static size_t duplex_mic_fill = 0;                   // Samples accumulated in enc_input_buffer
static bool duplex_fill_ref_layout = false;          // Layout duplex_mic_fill was counted in
static uint8_t *duplex_ref_pkt = NULL;               // AEC reference packet being filled
static uint16_t duplex_ref_seq = 0;
static bool duplex_ref_muted = false;                // DAC muted during the packet being filled
static bool duplex_mic_scale_changed = false;        // ADC scale set by the server for this session
static wifi_ps_type_t duplex_saved_ps = WIFI_PS_MIN_MODEM;
static bool duplex_ps_changed = false;               // WiFi power save turned off for this session

// Allocated on the first duplex session and kept (PSRAM, no CPU cost when idle)
static QueueHandle_t duplex_play_q = NULL;
static StaticQueue_t duplex_play_q_buf;
static uint8_t *duplex_play_q_storage = NULL;
static duplex_frame_t *duplex_rx_frame = NULL;       // WS handler side
static duplex_frame_t *duplex_play_frame = NULL;     // Play task side
static duplex_level_t duplex_levels[DUPLEX_LEVEL_SLOTS];
static int duplex_level_idx = 0;

// Play task exists only during a duplex session
static volatile TaskHandle_t duplex_play_task_handle = NULL;

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
#ifndef USE_LOCAL_WAKEWORD
    // aec_ref: in duplex the uplink can carry the ES8311 DAC loopback next to the mic
    cJSON *caps = cJSON_AddArrayToObject(json, "caps");
    if (caps) cJSON_AddItemToArray(caps, cJSON_CreateString("aec_ref"));
#endif

    char *str = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (!str) return false;

    int ret = esp_websocket_client_send_text(ws_client, str, strlen(str), pdMS_TO_TICKS(1000));
    free(str);

    return (ret >= 0);
}

// =============================================================================
// FULL-DUPLEX LIVE SESSION (Voice Live Hub via wakeword-server)
// =============================================================================
//
// Active only between live_session_start{"duplex":true} and live_session_end.
//   uplink:   mic ring buffer -> Opus -> WS (continuous, also while playing)
//   downlink: WS Opus -> decode (WS handler) -> queue -> play task -> I2S
// The WS handler never blocks on I2S here, so audio_clear is handled as soon
// as it arrives: queue reset + decoder reset, then the play task mutes the DAC
// for the time the I2S TX DMA ring needs to play out what it already holds.

static bool duplex_alloc(void) {
    if (duplex_play_q) return true;

    duplex_play_q_storage = heap_caps_malloc(DUPLEX_QUEUE_FRAMES * sizeof(duplex_frame_t),
                                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    duplex_rx_frame = heap_caps_malloc(sizeof(duplex_frame_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    duplex_play_frame = heap_caps_malloc(sizeof(duplex_frame_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    duplex_ref_pkt = heap_caps_malloc(DUPLEX_REF_HEADER + DUPLEX_REF_VALUES * sizeof(int16_t),
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!duplex_play_q_storage || !duplex_rx_frame || !duplex_play_frame || !duplex_ref_pkt) {
        ESP_LOGE(TAG, "Duplex buffer alloc failed");
        free(duplex_play_q_storage);
        free(duplex_rx_frame);
        free(duplex_play_frame);
        free(duplex_ref_pkt);
        duplex_play_q_storage = NULL;
        duplex_rx_frame = NULL;
        duplex_play_frame = NULL;
        duplex_ref_pkt = NULL;
        return false;
    }

    duplex_play_q = xQueueCreateStatic(DUPLEX_QUEUE_FRAMES, sizeof(duplex_frame_t),
                                       duplex_play_q_storage, &duplex_play_q_buf);
    return duplex_play_q != NULL;
}

static void duplex_levels_reset(void) {
    memset(duplex_levels, 0, sizeof(duplex_levels));
}

// RMS of a PCM frame mapped to 0-255 (sqrt curve, so normal speech fills the lane)
static uint8_t duplex_pcm_level(const int16_t *pcm, size_t samples) {
    if (samples == 0) return 0;
    int64_t sum = 0;
    for (size_t i = 0; i < samples; i++) {
        sum += (int32_t)pcm[i] * pcm[i];
    }
    float rms = sqrtf((float)sum / (float)samples);
    int level = (int)(sqrtf(rms / 32768.0f) * 360.0f);
    return (uint8_t)(level > 255 ? 255 : level);
}

static inline uint32_t duplex_now_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

// Mute the DAC until the TX ring has played out what it holds now (lead_ms from
// the playback clock). Never shortens a mute already running.
static void duplex_mute_ring(bool *muted, uint32_t *unmute_at_ms, uint32_t now, int32_t lead_ms) {
    uint32_t at = duplex_unmute_at(now, lead_ms, DUPLEX_RING_MS, DUPLEX_UNMUTE_MARGIN_MS);
    *unmute_at_ms = duplex_mute_deadline(*muted, *unmute_at_ms, at);
    if (!*muted) {
        jarvis_codec_set_mute(true);
        *muted = true;
    }
}

static void duplex_play_task(void *arg) {
    bool muted = false;
    uint32_t unmute_at_ms = 0;
    bool keep_running = true;

    ESP_LOGI(TAG, "Duplex play task started");

    // One iteration per session. A session started while this task is still
    // finishing the previous one (quick end + restart) is carried on here.
    while (keep_running) {
        const uint32_t session = duplex_session_gen;
        uint32_t seen_gen = duplex_clear_gen;
        bool prebuffering = true;
        bool prebuf_timing = false;
        uint32_t prebuf_since = 0;
        uint32_t last_write_ms = duplex_now_ms();
        // Playback clock: how far the DMA ring runs ahead of the speaker
        bool clock_running = false;
        uint32_t clock_start_ms = 0;
        uint32_t written_samples = 0;

        while (duplex_active && session == duplex_session_gen) {
            uint32_t now = duplex_now_ms();

            // Barge-in: queue already dropped by the WS handler, silence the DMA ring
            // until what it still holds has played out (playback clock, bounded by the ring)
            if (seen_gen != duplex_clear_gen) {
                seen_gen = duplex_clear_gen;
                int32_t lead_ms = clock_running ?
                    duplex_play_lead_ms(now, clock_start_ms, written_samples, SAMPLE_RATE / 1000) : 0;
                duplex_mute_ring(&muted, &unmute_at_ms, now, lead_ms);
                duplex_levels_reset();
                prebuffering = true;
                prebuf_timing = false;
            }
            if (muted && duplex_ms_reached(now, unmute_at_ms)) {
                jarvis_codec_set_mute(false);
                muted = false;
            }

            if (prebuffering) {
                UBaseType_t waiting = uxQueueMessagesWaiting(duplex_play_q);
                if (waiting == 0) {
                    prebuf_timing = false;
                    xQueuePeek(duplex_play_q, duplex_play_frame, pdMS_TO_TICKS(20));
                    continue;
                }
                if (!prebuf_timing) {
                    prebuf_timing = true;
                    prebuf_since = now;
                }
                if (waiting < DUPLEX_PREBUFFER_FRAMES &&
                    duplex_ms_diff(now, prebuf_since) < DUPLEX_PREBUFFER_MAX_MS) {
                    vTaskDelay(pdMS_TO_TICKS(5));
                    continue;
                }
                prebuffering = false;
            }

            if (xQueueReceive(duplex_play_q, duplex_play_frame, pdMS_TO_TICKS(10)) != pdTRUE) {
                if (duplex_ms_diff(duplex_now_ms(), last_write_ms) > DUPLEX_UNDERRUN_MS) {
                    prebuffering = true;
                    prebuf_timing = false;
                }
                continue;
            }
            // Queued before the last audio_clear: stale model speech
            if (duplex_frame_is_stale(duplex_play_frame->gen, duplex_clear_gen)) continue;

            now = duplex_now_ms();
            int32_t lead_ms = clock_running ?
                duplex_play_lead_ms(now, clock_start_ms, written_samples, SAMPLE_RATE / 1000) : -1;
            if (lead_ms < 0) {
                // Ring ran dry (or first frame): restart the clock
                clock_running = true;
                clock_start_ms = now;
                written_samples = 0;
                lead_ms = 0;
            }
            duplex_levels[duplex_level_idx].play_ms = now + (uint32_t)lead_ms;
            duplex_levels[duplex_level_idx].level = duplex_pcm_level(duplex_play_frame->pcm,
                                                                     duplex_play_frame->samples);
            duplex_level_idx = (duplex_level_idx + 1) % DUPLEX_LEVEL_SLOTS;

            jarvis_codec_write_stream(duplex_play_frame->pcm, duplex_play_frame->samples);
            written_samples += duplex_play_frame->samples;
            last_write_ms = duplex_now_ms();
        }

        // Session over (skipped if a new session already took over)
        if (session == duplex_session_gen) {
            if (duplex_end_flush) {
                // Device-side end: stop talking now
                xQueueReset(duplex_play_q);
                uint32_t now = duplex_now_ms();
                int32_t lead_ms = clock_running ?
                    duplex_play_lead_ms(now, clock_start_ms, written_samples, SAMPLE_RATE / 1000) : 0;
                duplex_mute_ring(&muted, &unmute_at_ms, now, lead_ms);
            } else {
                // Server-side end (hangup): let the last words (bounded by the queue) play
                while (!muted && !duplex_active &&
                       xQueueReceive(duplex_play_q, duplex_play_frame, 0) == pdTRUE) {
                    if (duplex_frame_is_stale(duplex_play_frame->gen, duplex_clear_gen)) continue;
                    jarvis_codec_write_stream(duplex_play_frame->pcm, duplex_play_frame->samples);
                }
                if (!duplex_active) xQueueReset(duplex_play_q);
            }
        }

        // Wait out the mute, unless a new session starts (its loop unmutes on time)
        while (muted && !duplex_active) {
            int32_t left = duplex_ms_diff(unmute_at_ms, duplex_now_ms());
            if (left <= 0) {
                jarvis_codec_set_mute(false);
                muted = false;
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(left < 20 ? left : 20));
        }

        // Exit only if no new session started meanwhile: checked atomically with
        // duplex_start(), which creates a new task only when the handle is NULL
        if (!duplex_active) duplex_levels_reset();
        portENTER_CRITICAL(&duplex_mux);
        keep_running = duplex_active;
        if (!keep_running) duplex_play_task_handle = NULL;
        portEXIT_CRITICAL(&duplex_mux);
        if (keep_running) {
            ESP_LOGI(TAG, "Duplex play task carries on into the new session");
        }
    }

    ESP_LOGI(TAG, "Duplex play task exiting");
    vTaskDelete(NULL);
}

// Session media options from live_session_start: AEC reference (feed task switches
// the ring layout on its next read) and an optional ADC scale while in duplex.
static void duplex_media_on(bool aec_ref, int mic_scale_db) {
    // Modem sleep holds the server's TCP ACKs until the next beacon (100-300 ms): fine
    // for 30 kbit/s Opus, not for the continuous duplex uplink. Only while in session.
    if (esp_wifi_get_ps(&duplex_saved_ps) == ESP_OK && duplex_saved_ps != WIFI_PS_NONE &&
        esp_wifi_set_ps(WIFI_PS_NONE) == ESP_OK) {
        duplex_ps_changed = true;
        ESP_LOGI(TAG, "WiFi power save OFF for the live session");
    }
    duplex_ref_seq = 0;
    jarvis_audio_set_aec_ref(aec_ref);
    duplex_mic_scale_changed = (mic_scale_db >= 0);
    if (duplex_mic_scale_changed) jarvis_codec_set_mic_scale_db(mic_scale_db);
}

static void duplex_media_off(void) {
    jarvis_audio_set_aec_ref(false);
    if (duplex_ps_changed) {
        esp_wifi_set_ps(duplex_saved_ps);
        duplex_ps_changed = false;
        ESP_LOGI(TAG, "WiFi power save restored");
    }
    if (duplex_mic_scale_changed) {
        jarvis_codec_set_mic_scale_db(JARVIS_CODEC_MIC_SCALE_DB);
        duplex_mic_scale_changed = false;
    }
}

// Called from the WS event handler. Never waits: a play task still finishing
// the previous session carries on into this one.
static bool duplex_start(bool aec_ref, int mic_scale_db) {
#ifdef USE_LOCAL_WAKEWORD
    // The continuous mic stream only exists with server-side wake word
    ESP_LOGW(TAG, "Duplex live session not supported with local wake word");
    return false;
#endif
    if (duplex_active) return true;
    if (!opus_encoder || !opus_decoder || !duplex_alloc()) return false;

    xQueueReset(duplex_play_q);
    duplex_levels_reset();
    duplex_mic_vu = 0;
    duplex_mic_vu_ms = duplex_now_ms();  // Watchdog counts from here until the first "vu"
    duplex_mic_fill = 0;
    duplex_late_drop_armed = false;
    opus_decoder_ctl(opus_decoder, OPUS_RESET_STATE);
    duplex_media_on(aec_ref, mic_scale_db);

    portENTER_CRITICAL(&duplex_mux);
    duplex_end_flush = false;
    duplex_session_gen++;
    duplex_active = true;
    bool need_task = (duplex_play_task_handle == NULL);
    portEXIT_CRITICAL(&duplex_mux);

    // Handle is stored before the task can run, so its exit always clears it
    if (need_task &&
        xTaskCreatePinnedToCore(duplex_play_task, "duplex_play", DUPLEX_PLAY_TASK_STACK,
                                NULL, DUPLEX_PLAY_TASK_PRIO,
                                (TaskHandle_t *)&duplex_play_task_handle, 1) != pdPASS) {
        ESP_LOGE(TAG, "Failed to create duplex play task");
        portENTER_CRITICAL(&duplex_mux);
        duplex_active = false;
        duplex_play_task_handle = NULL;
        portEXIT_CRITICAL(&duplex_mux);
        duplex_media_off();
        return false;
    }
    ESP_LOGI(TAG, "Duplex live session started%s%s", need_task ? "" : " (play task reused)",
             aec_ref ? " with AEC reference" : "");
    return true;
}

// flush=true: drop queued speech at once (device end, disconnect).
// flush=false: play what is queued, then stop (server end).
// Returns true if a duplex session was active.
static bool duplex_stop(bool flush) {
    portENTER_CRITICAL(&duplex_mux);
    bool was_active = duplex_active;
    if (was_active) {
        duplex_end_flush = flush;
        duplex_active = false;  // Play task drains/flushes and deletes itself
    }
    portEXIT_CRITICAL(&duplex_mux);
    if (was_active) {
        duplex_media_off();
        ESP_LOGI(TAG, "Duplex live session stopped (flush=%d)", flush);
    }
    return was_active;
}

// Device-side end (button from main_task, watchdog from ws_persist).
// Leaves duplex mode at once and tells the server; only the caller that
// actually ended the session sends live_session_end.
static bool duplex_end_local(const char *reason) {
    const char *why = duplex_end_reason(reason);
    uint32_t drop_until = duplex_now_ms() + DUPLEX_LATE_DROP_MS;

    portENTER_CRITICAL(&duplex_mux);
    bool was_active = duplex_active;
    if (was_active) {
        // Armed before leaving duplex, so the WS handler drops in-flight speech
        duplex_late_drop_until_ms = drop_until;
        duplex_late_drop_armed = true;
        duplex_end_flush = true;
        duplex_active = false;
    }
    portEXIT_CRITICAL(&duplex_mux);
    if (!was_active) return false;

    duplex_media_off();
    live_session_active = false;
    ESP_LOGI(TAG, "Ending duplex live session (reason=%s)", why);
    send_json("live_session_end", "reason", why);
    return true;
}

// Barge-in (WS event handler context)
static void duplex_clear(void) {
    duplex_clear_gen++;
    xQueueReset(duplex_play_q);
    opus_decoder_ctl(opus_decoder, OPUS_RESET_STATE);
}

// Downlink Opus frame (WS event handler context)
static void duplex_on_downlink(const char *data, int len) {
    static uint32_t dropped = 0;
    if (!opus_decoder || !duplex_rx_frame) return;

    int samples = opus_decode(opus_decoder, (const unsigned char *)data, len,
                              duplex_rx_frame->pcm, BUFFER_SAMPLES, 0);
    if (samples <= 0) return;
    duplex_rx_frame->samples = (uint16_t)samples;
    duplex_rx_frame->gen = duplex_clear_gen;

    if (xQueueSend(duplex_play_q, duplex_rx_frame,
                   pdMS_TO_TICKS(DUPLEX_ENQUEUE_TIMEOUT_MS)) != pdTRUE) {
        dropped++;
        if (dropped == 1 || dropped % 50 == 0) {
            ESP_LOGW(TAG, "Duplex play queue full, frame dropped (total=%lu)", (unsigned long)dropped);
        }
    }
}

// AEC reference uplink: exact 20ms [mic, ref] packets, PCM (see DUPLEX_REF_MAGIC)
static void duplex_stream_mic_ref(void) {
    int16_t *pairs = (int16_t *)(duplex_ref_pkt + DUPLEX_REF_HEADER);
    for (int i = 0; i < 4; i++) {  // Up to 4 reads per tick to catch up after a stall
        size_t got = jarvis_audio_read_raw(pairs + duplex_mic_fill,
                                           DUPLEX_REF_VALUES - duplex_mic_fill,
                                           i == 0 ? TICK_INTERVAL_MS : 0);
        if (got == 0) break;
        duplex_ref_muted |= jarvis_codec_is_muted();
        duplex_mic_fill += got;
        if (duplex_mic_fill < DUPLEX_REF_VALUES) continue;
        duplex_mic_fill = 0;

        duplex_ref_pkt[0] = DUPLEX_REF_MAGIC;
        duplex_ref_pkt[1] = DUPLEX_REF_FMT_PCM | (duplex_ref_muted ? DUPLEX_REF_FLAG_MUTED : 0);
        duplex_ref_muted = false;
        duplex_ref_pkt[2] = (uint8_t)(duplex_ref_seq & 0xFF);
        duplex_ref_pkt[3] = (uint8_t)(duplex_ref_seq >> 8);
        duplex_ref_seq++;
        esp_websocket_client_send_bin(ws_client, (const char *)duplex_ref_pkt,
                                      DUPLEX_REF_HEADER + DUPLEX_REF_VALUES * sizeof(int16_t),
                                      pdMS_TO_TICKS(DUPLEX_REF_SEND_TIMEOUT_MS));
    }
}

// Uplink: accumulate exact 20ms frames from the mic ring buffer (ws_persist task).
// Unlike the idle stream, partial reads are kept, so no audio is lost.
static void duplex_stream_mic(void) {
    if (jarvis_audio_aec_ref_pending()) {
        // Ring layout switching: its contents are neither mono nor pairs for sure
        jarvis_audio_read_raw(enc_input_buffer, BUFFER_SAMPLES, TICK_INTERVAL_MS);
        duplex_mic_fill = 0;
        return;
    }
    bool ref_layout = jarvis_audio_aec_ref_active();
    if (ref_layout != duplex_fill_ref_layout) {
        duplex_fill_ref_layout = ref_layout;
        duplex_mic_fill = 0;  // A partial frame in the other layout would misalign mic and ref
    }
    if (ref_layout) {
        duplex_stream_mic_ref();
        return;
    }
    for (int i = 0; i < 4; i++) {  // Up to 4 reads per tick to catch up after a stall
        size_t got = jarvis_audio_read_raw(enc_input_buffer + duplex_mic_fill,
                                           OPUS_FRAME_SAMPLES - duplex_mic_fill,
                                           i == 0 ? TICK_INTERVAL_MS : 0);
        if (got == 0) break;
        duplex_mic_fill += got;
        if (duplex_mic_fill < OPUS_FRAME_SAMPLES) continue;
        duplex_mic_fill = 0;

        int enc_size = opus_encode(opus_encoder, enc_input_buffer, OPUS_FRAME_SAMPLES,
                                   enc_output_buffer, OPUS_MAX_PACKET_SIZE);
        if (enc_size > 0) {
            esp_websocket_client_send_bin(ws_client, (const char *)enc_output_buffer,
                                          enc_size, pdMS_TO_TICKS(100));
        }
    }
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
                                    // Speaker volume (0-100) — direct I2C write, no deferral needed
                                    cJSON *vol = cJSON_GetObjectItem(msg, "speaker_volume");
                                    if (vol && cJSON_IsNumber(vol)) {
                                        ESP_LOGI(TAG, "Config: speaker_volume=%d", vol->valueint);
                                        jarvis_codec_set_volume(vol->valueint);
                                    }

                                } else if (strcmp(type, "wake_detected") == 0) {
                                    ESP_LOGI(TAG, "Server: wake_detected (server-side wake word)");
                                    if (wake_detected_cb) {
                                        wake_detected_cb();
                                    }

                                } else if (strcmp(type, "live_session_start") == 0) {
                                    bool duplex = cJSON_IsTrue(cJSON_GetObjectItem(msg, "duplex"));
                                    bool aec_ref = cJSON_IsTrue(cJSON_GetObjectItem(msg, "aec_ref"));
                                    cJSON *gain = cJSON_GetObjectItem(msg, "mic_gain_db");
                                    int mic_scale_db = (gain && cJSON_IsNumber(gain)) ? gain->valueint : -1;
                                    ESP_LOGI(TAG, "Server: live_session_start%s", duplex ? " (duplex)" : "");
                                    live_session_active = true;
                                    if (duplex && !duplex_start(aec_ref, mic_scale_db)) {
                                        // Cannot do full-duplex: end it cleanly rather than half-work
                                        send_json("live_session_end", "reason", "error");
                                        live_session_active = false;
                                    }

                                } else if (strcmp(type, "live_session_end") == 0) {
                                    ESP_LOGI(TAG, "Server: live_session_end");
                                    live_session_active = false;
                                    duplex_stop(false);

                                } else if (strcmp(type, "audio_clear") == 0) {
                                    if (duplex_active) {
                                        ESP_LOGI(TAG, "Server: audio_clear (barge-in)");
                                        duplex_clear();
                                    }

                                } else if (strcmp(type, "vu") == 0) {
                                    cJSON *mic = cJSON_GetObjectItem(msg, "mic");
                                    if (duplex_active && mic && cJSON_IsNumber(mic)) {
                                        int v = mic->valueint;
                                        duplex_mic_vu = (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v));
                                        duplex_mic_vu_ms = duplex_now_ms();
                                    }

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
                uint32_t rx_now = duplex_now_ms();
                if (duplex_late_drop_expired(duplex_late_drop_armed, rx_now, duplex_late_drop_until_ms)) {
                    duplex_late_drop_armed = false;
                }
                duplex_rx_route_t route = duplex_rx_route(duplex_active,
                                                          duplex_play_task_handle != NULL,
                                                          duplex_late_drop_armed,
                                                          rx_now,
                                                          duplex_late_drop_until_ms);
                if (route == DUPLEX_RX_QUEUE) {
                    // Duplex live session: model speech goes through the play queue
                    if (data->data_ptr && data->data_len > 0) {
                        duplex_on_downlink(data->data_ptr, data->data_len);
                    }
                } else if (route == DUPLEX_RX_DROP) {
                    // Duplex session just ended: its play task still owns the speaker,
                    // or the device ended it and stray model speech is still in flight
                } else if (opus_decoder && dec_output_buffer && data->data_ptr && data->data_len > 0) {
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
            if (duplex_active) {
                // The wakeword-server tears down the hub side when the device drops
                duplex_stop(true);
                live_session_active = false;
            }
            break;

        case WEBSOCKET_EVENT_ERROR:
            ESP_LOGE(TAG, "WebSocket error");
            conn_state = CONN_STATE_DISCONNECTED;
            audio_session_active = false;
            if (duplex_active) {
                duplex_stop(true);
                live_session_active = false;
            }
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
            .task_stack = 8192,  // 8KB: event handler runs opus_decode (~5KB SILK) + codec_write
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
        send_json_hello(device_id_buf, "5.2.0-ws");

        // === Phase 3: Main loop (idle + audio sessions) ===
        int64_t last_ping_time = esp_timer_get_time() / 1000;

        while (task_should_run && conn_state >= CONN_STATE_CONNECTED) {
            int64_t now = esp_timer_get_time() / 1000;

            // Continuous Opus stream to server (for server-side wake word detection)
            // Only when idle — during active sessions, run_audio_session handles streaming
            if (duplex_active && conn_state == CONN_STATE_CONNECTED && !audio_session_requested) {
                // Duplex live session: same continuous stream, without losing partial reads
                duplex_stream_mic();
            }
            #ifndef USE_LOCAL_WAKEWORD
            else if (conn_state == CONN_STATE_CONNECTED && !audio_session_requested &&
                     (jarvis_audio_aec_ref_active() || jarvis_audio_aec_ref_pending())) {
                // Duplex just ended: drop [mic, ref] pairs until the feed task is back to mono
                jarvis_audio_read_raw(enc_input_buffer, BUFFER_SAMPLES, TICK_INTERVAL_MS);
            }
            else if (conn_state == CONN_STATE_CONNECTED && !audio_session_requested) {
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

            // Duplex liveness: "vu" stops when the server side of the session is gone
            if (duplex_active &&
                duplex_watchdog_expired(duplex_now_ms(), duplex_mic_vu_ms, DUPLEX_WATCHDOG_MS)) {
                ESP_LOGW(TAG, "Duplex: no vu from server for %dms, ending session", DUPLEX_WATCHDOG_MS);
                duplex_end_local("error");
            }

            // Late-drop window over: disarm it even if no frame arrives (see duplex_late_drop_expired)
            if (duplex_late_drop_expired(duplex_late_drop_armed, duplex_now_ms(), duplex_late_drop_until_ms)) {
                duplex_late_drop_armed = false;
            }

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

bool jarvis_ws_audio_is_duplex(void) {
    return duplex_active;
}

bool jarvis_ws_audio_end_duplex(const char *reason) {
    return duplex_end_local(reason);
}

void jarvis_ws_audio_get_duplex_levels(uint8_t *jarvis_level, uint8_t *user_level) {
    uint32_t now = duplex_now_ms();

    if (jarvis_level) {
        // Peak of the frames the speaker is playing in the last display frame
        uint8_t peak = 0;
        for (int i = 0; i < DUPLEX_LEVEL_SLOTS; i++) {
            if (duplex_level_in_window(now, duplex_levels[i].play_ms, DUPLEX_LEVEL_WINDOW_MS) &&
                duplex_levels[i].level > peak) {
                peak = duplex_levels[i].level;
            }
        }
        *jarvis_level = duplex_active ? peak : 0;
    }
    if (user_level) {
        bool fresh = duplex_vu_fresh(now, duplex_mic_vu_ms, DUPLEX_VU_STALE_MS);
        *user_level = (duplex_active && fresh) ? duplex_mic_vu : 0;
    }
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
