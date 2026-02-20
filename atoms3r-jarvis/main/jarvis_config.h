/**
 * =============================================================================
 * JARVIS AtomS3R - Configuration Header (ESP-IDF)
 * =============================================================================
 */

#ifndef JARVIS_CONFIG_H
#define JARVIS_CONFIG_H

#include <stdint.h>
#include <stdbool.h>

// =============================================================================
// NETWORK CONFIGURATION
// =============================================================================

// WiFi credentials (sostituisci con i tuoi)
#define WIFI_SSID           "YOUR_WIFI_SSID"
#define WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"

// JARVIS Server
#define JARVIS_SERVER_HOST  "jarvis.local"  // O IP: "192.168.1.100"
#define JARVIS_SERVER_PORT  5000
#define JARVIS_ENDPOINT     "/voice_command"

// Home Assistant (per temperatura, opzionale)
#define HASS_HOST           "homeassistant.local"
#define HASS_PORT           8123
#define HASS_TOKEN          "YOUR_LONG_LIVED_TOKEN"

// NTP Server
#define NTP_SERVER          "pool.ntp.org"
#define NTP_OFFSET_SECONDS  3600  // UTC+1 (Italia)
#define NTP_UPDATE_INTERVAL 60000 // 1 minuto

// =============================================================================
// DEVICE CONFIGURATION (Dynamic - from server)
// =============================================================================

// Il device_id è il MAC address (formato AABBCCDDEEFF) letto a runtime.
// La configurazione (friendly_name, location, speaker) viene recuperata dal server.

// Lunghezza MAC address senza separatori
#define DEVICE_ID_LENGTH    12

// Lunghezza massima friendly_name
#define FRIENDLY_NAME_MAX   32

// Lunghezza massima location_id
#define LOCATION_ID_MAX     32

// Pattern sensore temperatura in Home Assistant (usa friendly_name o location)
#define TEMP_SENSOR_PATTERN "sensor.temperatura_%s"

// Safety timeout stato busy (ms) — fallback if server never responds via WS
// Server sends tts_done when done; this is only a last-resort safety net.
// Must be long enough for complex responses (TTS can be 30-60s+).
#define BUSY_STATE_TIMEOUT_MS   120000

// Intervallo heartbeat al server (ms) - 5 minuti
#define HEARTBEAT_INTERVAL_MS   300000

// Timeout fetch config dal server (ms)
#define CONFIG_FETCH_TIMEOUT_MS 10000

// =============================================================================
// DISPLAY CONFIGURATION (AtomS3R: GC9107/ST7735S 128x128)
// =============================================================================

#define DISPLAY_WIDTH       128
#define DISPLAY_HEIGHT      128

// Pin SPI per display AtomS3R (da M5Unified/M5GFX source)
// NOTA: AtomS3R ≠ AtomS3! GPIO33-37 riservati per PSRAM OPI su N8R8
#define DISPLAY_PIN_MOSI    21  // LCD MOSI
#define DISPLAY_PIN_SCLK    15  // LCD SCK
#define DISPLAY_PIN_CS      14  // LCD CS
#define DISPLAY_PIN_DC      42  // LCD DC (Register Select)
#define DISPLAY_PIN_RST     48  // LCD RST
#define DISPLAY_PIN_BL      16  // Backlight (PWM)

// Bottone sotto il display
#define BUTTON_PIN          41  // BTN (GPIO41 su AtomS3R, non GPIO0)

// Colori (RGB565)
#define COLOR_BLACK         0x0000
#define COLOR_WHITE         0xFFFF
#define COLOR_RED           0xF800
#define COLOR_GREEN         0x07E0
#define JARVIS_BLUE         0x041F
#define JARVIS_BLUE_DARK    0x020F
#define JARVIS_BLUE_LIGHT   0x063F

// Bordo DND
#define DND_BORDER_WIDTH    4
#define DND_BORDER_COLOR    COLOR_RED

// =============================================================================
// AUDIO CONFIGURATION (Atomic Echo Base: ES8311 codec + NS4150B amp)
// =============================================================================
// L'audio è gestito dal codec ES8311 sull'Atomic Echo Base.
// I2S standard full-duplex su un singolo port:
//   BCLK=GPIO8, WS=GPIO6, DIN=GPIO7 (mic), DOUT=GPIO5 (spk)
// ES8311 controllato via I2C: SDA=GPIO38, SCL=GPIO39, addr=0x18

#define MIC_SAMPLE_RATE     16000
#define MIC_BITS_PER_SAMPLE 16
#define MIC_CHANNEL_NUM     1

// Buffer audio
#define AUDIO_CHUNK_SIZE    512

// =============================================================================
// TIMING CONFIGURATION
// =============================================================================

#define DISPLAY_UPDATE_IDLE_MS  1000
#define TEMP_REFRESH_MS         30000
#define WAVE_ANIMATION_MS       50
#define BUTTON_DEBOUNCE_MS      200

// Screensaver (Matrix rain)
#define SCREENSAVER_TIMEOUT_MS   600000   // 10 min inattivita
#define SCREENSAVER_DURATION_MS   60000   // 60s animazione
#define SCREENSAVER_FRAME_MS         50   // ~20 FPS

// =============================================================================
// WEBSOCKET AUDIO + OPUS CONFIGURATION
// =============================================================================

#define WS_AUDIO_PATH               "/ws/audio"
#define WS_SESSION_TIMEOUT_MS       30000   // 30s max session

// Opus codec
#define OPUS_BITRATE                30000
#define OPUS_COMPLEXITY             0       // Lowest CPU usage
#define OPUS_FRAME_SAMPLES          320     // 20ms @ 16kHz
#define OPUS_MAX_PACKET_SIZE        1276

// Raw audio ring buffer (for WS audio streaming path)
#define RAW_RINGBUF_SIZE            (16000 * 2)  // 1s = 32KB @ 16kHz 16-bit

// =============================================================================
// WAKE WORD CONFIGURATION (microWakeWord TFLite)
// =============================================================================

// Detection threshold: sliding window average of model output probabilities.
// Model outputs uint8 0-255 (0.0-1.0 quantized). This is compared against
// the mean of SLIDING_WINDOW_SIZE recent predictions.
// microWakeWord V2 hey_jarvis (pre-trained by ESPHome/Kevin Ahrendt)
#define MWW_PROBABILITY_CUTOFF      0.75f
#define MWW_SLIDING_WINDOW_SIZE     5
#define MWW_FEATURE_STEP_MS         10       // 10ms hop (v2-style model)
#define MWW_MIN_SLICES_BEFORE_DET   148      // ~1.5s at 10ms step

// =============================================================================
// STATE MACHINE
// =============================================================================

typedef enum {
    STATE_IDLE,
    STATE_LISTENING,
    STATE_PROCESSING,
    STATE_BUSY,
    STATE_DND,
    STATE_ERROR
} device_state_t;

#endif // JARVIS_CONFIG_H
