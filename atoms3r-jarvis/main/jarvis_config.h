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
#define JARVIS_STREAM_ENDPOINT  "/voice_stream"

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

// Intervallo polling stato busy dal server (ms)
#define BUSY_POLL_INTERVAL_MS   500

// Timeout stato busy (ms)
#define BUSY_STATE_TIMEOUT_MS   10000

// Intervallo heartbeat al server (ms) - 5 minuti
#define HEARTBEAT_INTERVAL_MS   300000

// Timeout fetch config dal server (ms)
#define CONFIG_FETCH_TIMEOUT_MS 10000

// =============================================================================
// DISPLAY CONFIGURATION (AtomS3R: ST7789 128x128)
// =============================================================================

#define DISPLAY_WIDTH       128
#define DISPLAY_HEIGHT      128

// Pin SPI per display AtomS3R (da pinschema ufficiale)
#define DISPLAY_PIN_MOSI    21  // LCD MOSI
#define DISPLAY_PIN_SCLK    17  // LCD SCK
#define DISPLAY_PIN_CS      15  // LCD CS
#define DISPLAY_PIN_DC      33  // LCD DC
#define DISPLAY_PIN_RST     34  // LCD RST
#define DISPLAY_PIN_BL      -1  // Backlight non disponibile (sempre acceso)

// Bottone sotto il display
#define BUTTON_PIN          0   // SCREEN BTN (GPIO0)

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
// AUDIO CONFIGURATION (AtomS3R: PDM Microphone)
// =============================================================================

#define MIC_SAMPLE_RATE     16000
#define MIC_BITS_PER_SAMPLE 16
#define MIC_CHANNEL_NUM     1

// Pin microfono PDM AtomS3R
#define MIC_CLK_PIN         1
#define MIC_DATA_PIN        2

// Buffer audio
#define AUDIO_CHUNK_SIZE    512

// =============================================================================
// TIMING CONFIGURATION
// =============================================================================

#define DISPLAY_UPDATE_IDLE_MS  1000
#define TEMP_REFRESH_MS         30000
#define WAVE_ANIMATION_MS       50
#define BUTTON_DEBOUNCE_MS      200

// =============================================================================
// STREAMING AUDIO CONFIGURATION (VAD-based)
// =============================================================================

#define STREAM_CHUNK_SAMPLES    512     // 32ms di audio per chunk
#define VAD_SILENCE_CHUNKS      30      // ~1 secondo di silenzio
#define STREAM_MAX_DURATION_MS  60000   // 60s safety timeout
#define STREAM_MIN_AUDIO_MS     500     // Minimo audio prima di VAD check

// =============================================================================
// WAKE WORD CONFIGURATION (ESP-SR WakeNet)
// =============================================================================

#define WAKENET_MODEL_NAME      "wn9_jarvis"

// Modalità detection
// DET_MODE_90  = Alta sensibilità
// DET_MODE_95  = Media sensibilità (default)
// DET_MODE_2G75 = Medio-bassa sensibilità
// DET_MODE_3G75 = Bassa sensibilità
#define WAKENET_DET_MODE        DET_MODE_95

#define WAKENET_VAD_ENABLED     true
#define WAKENET_AEC_ENABLED     false

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
