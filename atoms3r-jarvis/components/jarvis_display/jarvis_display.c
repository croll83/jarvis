/**
 * =============================================================================
 * JARVIS AtomS3R - Display Module Implementation (ESP-IDF)
 * =============================================================================
 */

#include "jarvis_display.h"
#include "jarvis_icons.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"

static const char *TAG = "DISPLAY";

// =============================================================================
// CONFIGURATION
// =============================================================================

#define DISPLAY_WIDTH       128
#define DISPLAY_HEIGHT      128

// Pin definitions for AtomS3R (da M5Unified/M5GFX source code)
// NOTA: AtomS3R ≠ AtomS3! I pin sono diversi perché l'N8R8 usa GPIO33-37 per PSRAM OPI
#define PIN_NUM_MOSI        21  // LCD MOSI
#define PIN_NUM_CLK         15  // LCD SCK
#define PIN_NUM_CS          14  // LCD CS
#define PIN_NUM_DC          42  // LCD DC (Register Select)
#define PIN_NUM_RST         48  // LCD RST
#define PIN_NUM_BL          16  // Backlight (PWM)

// Colors (RGB565)
#define COLOR_BLACK         0x0000
#define COLOR_WHITE         0xFFFF
#define COLOR_RED           0xF800
#define JARVIS_BLUE         0x041F
#define JARVIS_BLUE_DARK    0x020F

#define DND_BORDER_WIDTH    4
#define DND_BORDER_COLOR    COLOR_RED

// Icon positioning
#define ICON_TEXT_GAP       8
#define TEXT_HEIGHT         8
#define TOTAL_CONTENT_HEIGHT (ICON_MIC_HEIGHT + ICON_TEXT_GAP + TEXT_HEIGHT)
#define CONTENT_START_Y     ((DISPLAY_HEIGHT - TOTAL_CONTENT_HEIGHT) / 2)
#define ICON_CENTER_Y       (CONTENT_START_Y + ICON_MIC_HEIGHT / 2)
#define TEXT_Y              (CONTENT_START_Y + ICON_MIC_HEIGHT + ICON_TEXT_GAP + TEXT_HEIGHT / 2)

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// =============================================================================
// STATE
// =============================================================================

static esp_lcd_panel_handle_t panel_handle = NULL;
static display_state_t current_state = DISPLAY_STATE_IDLE;
static display_state_t prev_state = DISPLAY_STATE_IDLE;

static int current_hour = 0;
static int current_minute = 0;
static float current_temp = 0.0f;
static char error_msg[32] = "";
static char friendly_name[33] = "";  // 32 chars + null terminator

static float wave_phase = 0.0f;
static int64_t last_wave_update = 0;

// Frame buffer (RGB565, 128x128 = 32KB)
static uint16_t *frame_buffer = NULL;

// =============================================================================
// HELPER FUNCTIONS
// =============================================================================

static void fb_clear(uint16_t color) {
    if (!frame_buffer) return;
    for (int i = 0; i < DISPLAY_WIDTH * DISPLAY_HEIGHT; i++) {
        frame_buffer[i] = color;
    }
}

static void fb_set_pixel(int x, int y, uint16_t color) {
    if (!frame_buffer) return;
    if (x >= 0 && x < DISPLAY_WIDTH && y >= 0 && y < DISPLAY_HEIGHT) {
        frame_buffer[y * DISPLAY_WIDTH + x] = color;
    }
}

static void fb_draw_rect(int x, int y, int w, int h, uint16_t color) {
    for (int dy = 0; dy < h; dy++) {
        for (int dx = 0; dx < w; dx++) {
            fb_set_pixel(x + dx, y + dy, color);
        }
    }
}

static void fb_draw_border(uint16_t color, int width) {
    for (int i = 0; i < width; i++) {
        // Top
        for (int x = i; x < DISPLAY_WIDTH - i; x++) {
            fb_set_pixel(x, i, color);
        }
        // Bottom
        for (int x = i; x < DISPLAY_WIDTH - i; x++) {
            fb_set_pixel(x, DISPLAY_HEIGHT - 1 - i, color);
        }
        // Left
        for (int y = i; y < DISPLAY_HEIGHT - i; y++) {
            fb_set_pixel(i, y, color);
        }
        // Right
        for (int y = i; y < DISPLAY_HEIGHT - i; y++) {
            fb_set_pixel(DISPLAY_WIDTH - 1 - i, y, color);
        }
    }
}

// Standard 5x7 ASCII font (printable chars 0x20-0x7E = 95 glyphs)
static const uint8_t font_5x7[][5] = {
    {0x00, 0x00, 0x00, 0x00, 0x00}, // 0x20 ' '
    {0x00, 0x00, 0x5F, 0x00, 0x00}, // 0x21 '!'
    {0x00, 0x07, 0x00, 0x07, 0x00}, // 0x22 '"'
    {0x14, 0x7F, 0x14, 0x7F, 0x14}, // 0x23 '#'
    {0x24, 0x2A, 0x7F, 0x2A, 0x12}, // 0x24 '$'
    {0x23, 0x13, 0x08, 0x64, 0x62}, // 0x25 '%'
    {0x36, 0x49, 0x55, 0x22, 0x50}, // 0x26 '&'
    {0x00, 0x05, 0x03, 0x00, 0x00}, // 0x27 '''
    {0x00, 0x1C, 0x22, 0x41, 0x00}, // 0x28 '('
    {0x00, 0x41, 0x22, 0x1C, 0x00}, // 0x29 ')'
    {0x08, 0x2A, 0x1C, 0x2A, 0x08}, // 0x2A '*'
    {0x08, 0x08, 0x3E, 0x08, 0x08}, // 0x2B '+'
    {0x00, 0x50, 0x30, 0x00, 0x00}, // 0x2C ','
    {0x08, 0x08, 0x08, 0x08, 0x08}, // 0x2D '-'
    {0x00, 0x60, 0x60, 0x00, 0x00}, // 0x2E '.'
    {0x20, 0x10, 0x08, 0x04, 0x02}, // 0x2F '/'
    {0x3E, 0x51, 0x49, 0x45, 0x3E}, // 0x30 '0'
    {0x00, 0x42, 0x7F, 0x40, 0x00}, // 0x31 '1'
    {0x42, 0x61, 0x51, 0x49, 0x46}, // 0x32 '2'
    {0x21, 0x41, 0x45, 0x4B, 0x31}, // 0x33 '3'
    {0x18, 0x14, 0x12, 0x7F, 0x10}, // 0x34 '4'
    {0x27, 0x45, 0x45, 0x45, 0x39}, // 0x35 '5'
    {0x3C, 0x4A, 0x49, 0x49, 0x30}, // 0x36 '6'
    {0x01, 0x71, 0x09, 0x05, 0x03}, // 0x37 '7'
    {0x36, 0x49, 0x49, 0x49, 0x36}, // 0x38 '8'
    {0x06, 0x49, 0x49, 0x29, 0x1E}, // 0x39 '9'
    {0x00, 0x36, 0x36, 0x00, 0x00}, // 0x3A ':'
    {0x00, 0x56, 0x36, 0x00, 0x00}, // 0x3B ';'
    {0x00, 0x08, 0x14, 0x22, 0x41}, // 0x3C '<'
    {0x14, 0x14, 0x14, 0x14, 0x14}, // 0x3D '='
    {0x41, 0x22, 0x14, 0x08, 0x00}, // 0x3E '>'
    {0x02, 0x01, 0x51, 0x09, 0x06}, // 0x3F '?'
    {0x32, 0x49, 0x79, 0x41, 0x3E}, // 0x40 '@'
    {0x7E, 0x11, 0x11, 0x11, 0x7E}, // 0x41 'A'
    {0x7F, 0x49, 0x49, 0x49, 0x36}, // 0x42 'B'
    {0x3E, 0x41, 0x41, 0x41, 0x22}, // 0x43 'C'
    {0x7F, 0x41, 0x41, 0x22, 0x1C}, // 0x44 'D'
    {0x7F, 0x49, 0x49, 0x49, 0x41}, // 0x45 'E'
    {0x7F, 0x09, 0x09, 0x01, 0x01}, // 0x46 'F'
    {0x3E, 0x41, 0x41, 0x51, 0x32}, // 0x47 'G'
    {0x7F, 0x08, 0x08, 0x08, 0x7F}, // 0x48 'H'
    {0x00, 0x41, 0x7F, 0x41, 0x00}, // 0x49 'I'
    {0x20, 0x40, 0x41, 0x3F, 0x01}, // 0x4A 'J'
    {0x7F, 0x08, 0x14, 0x22, 0x41}, // 0x4B 'K'
    {0x7F, 0x40, 0x40, 0x40, 0x40}, // 0x4C 'L'
    {0x7F, 0x02, 0x04, 0x02, 0x7F}, // 0x4D 'M'
    {0x7F, 0x04, 0x08, 0x10, 0x7F}, // 0x4E 'N'
    {0x3E, 0x41, 0x41, 0x41, 0x3E}, // 0x4F 'O'
    {0x7F, 0x09, 0x09, 0x09, 0x06}, // 0x50 'P'
    {0x3E, 0x41, 0x51, 0x21, 0x5E}, // 0x51 'Q'
    {0x7F, 0x09, 0x19, 0x29, 0x46}, // 0x52 'R'
    {0x46, 0x49, 0x49, 0x49, 0x31}, // 0x53 'S'
    {0x01, 0x01, 0x7F, 0x01, 0x01}, // 0x54 'T'
    {0x3F, 0x40, 0x40, 0x40, 0x3F}, // 0x55 'U'
    {0x1F, 0x20, 0x40, 0x20, 0x1F}, // 0x56 'V'
    {0x7F, 0x20, 0x18, 0x20, 0x7F}, // 0x57 'W'
    {0x63, 0x14, 0x08, 0x14, 0x63}, // 0x58 'X'
    {0x03, 0x04, 0x78, 0x04, 0x03}, // 0x59 'Y'
    {0x61, 0x51, 0x49, 0x45, 0x43}, // 0x5A 'Z'
    {0x00, 0x00, 0x7F, 0x41, 0x41}, // 0x5B '['
    {0x02, 0x04, 0x08, 0x10, 0x20}, // 0x5C '\'
    {0x41, 0x41, 0x7F, 0x00, 0x00}, // 0x5D ']'
    {0x04, 0x02, 0x01, 0x02, 0x04}, // 0x5E '^'
    {0x40, 0x40, 0x40, 0x40, 0x40}, // 0x5F '_'
    {0x00, 0x01, 0x02, 0x04, 0x00}, // 0x60 '`'
    {0x20, 0x54, 0x54, 0x54, 0x78}, // 0x61 'a'
    {0x7F, 0x48, 0x44, 0x44, 0x38}, // 0x62 'b'
    {0x38, 0x44, 0x44, 0x44, 0x20}, // 0x63 'c'
    {0x38, 0x44, 0x44, 0x48, 0x7F}, // 0x64 'd'
    {0x38, 0x54, 0x54, 0x54, 0x18}, // 0x65 'e'
    {0x08, 0x7E, 0x09, 0x01, 0x02}, // 0x66 'f'
    {0x08, 0x14, 0x54, 0x54, 0x3C}, // 0x67 'g'
    {0x7F, 0x08, 0x04, 0x04, 0x78}, // 0x68 'h'
    {0x00, 0x44, 0x7D, 0x40, 0x00}, // 0x69 'i'
    {0x20, 0x40, 0x44, 0x3D, 0x00}, // 0x6A 'j'
    {0x00, 0x7F, 0x10, 0x28, 0x44}, // 0x6B 'k'
    {0x00, 0x41, 0x7F, 0x40, 0x00}, // 0x6C 'l'
    {0x7C, 0x04, 0x18, 0x04, 0x78}, // 0x6D 'm'
    {0x7C, 0x08, 0x04, 0x04, 0x78}, // 0x6E 'n'
    {0x38, 0x44, 0x44, 0x44, 0x38}, // 0x6F 'o'
    {0x7C, 0x14, 0x14, 0x14, 0x08}, // 0x70 'p'
    {0x08, 0x14, 0x14, 0x18, 0x7C}, // 0x71 'q'
    {0x7C, 0x08, 0x04, 0x04, 0x08}, // 0x72 'r'
    {0x48, 0x54, 0x54, 0x54, 0x20}, // 0x73 's'
    {0x04, 0x3F, 0x44, 0x40, 0x20}, // 0x74 't'
    {0x3C, 0x40, 0x40, 0x20, 0x7C}, // 0x75 'u'
    {0x1C, 0x20, 0x40, 0x20, 0x1C}, // 0x76 'v'
    {0x3C, 0x40, 0x30, 0x40, 0x3C}, // 0x77 'w'
    {0x44, 0x28, 0x10, 0x28, 0x44}, // 0x78 'x'
    {0x0C, 0x50, 0x50, 0x50, 0x3C}, // 0x79 'y'
    {0x44, 0x64, 0x54, 0x4C, 0x44}, // 0x7A 'z'
    {0x00, 0x08, 0x36, 0x41, 0x00}, // 0x7B '{'
    {0x00, 0x00, 0x7F, 0x00, 0x00}, // 0x7C '|'
    {0x00, 0x41, 0x36, 0x08, 0x00}, // 0x7D '}'
    {0x08, 0x08, 0x2A, 0x1C, 0x08}, // 0x7E '~'
};

static void fb_draw_char(int x, int y, char c, uint16_t color, int scale) {
    // Map ASCII char to font index (printable range 0x20-0x7E)
    int idx = 0; // space by default
    if (c >= 0x20 && c <= 0x7E) {
        idx = c - 0x20;
    }

    for (int col = 0; col < 5; col++) {
        uint8_t line = font_5x7[idx][col];
        for (int row = 0; row < 7; row++) {
            if (line & (1 << row)) {
                for (int sy = 0; sy < scale; sy++) {
                    for (int sx = 0; sx < scale; sx++) {
                        fb_set_pixel(x + col * scale + sx, y + row * scale + sy, color);
                    }
                }
            }
        }
    }
}

static void fb_draw_string(int x, int y, const char* str, uint16_t color, int scale) {
    int char_width = 6 * scale;  // 5 pixels + 1 space
    while (*str) {
        fb_draw_char(x, y, *str, color, scale);
        x += char_width;
        str++;
    }
}

static void fb_draw_string_centered(int y, const char* str, uint16_t color, int scale) {
    int len = strlen(str);
    int char_width = 6 * scale;
    int total_width = len * char_width - scale;  // Remove trailing space
    int x = (DISPLAY_WIDTH - total_width) / 2;
    fb_draw_string(x, y, str, color, scale);
}

static void fb_draw_icon_at(const uint8_t* icon, int width, int height, uint16_t color, int center_y) {
    int start_x = (DISPLAY_WIDTH - width) / 2;
    int start_y = center_y - height / 2;

    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            int byte_idx = y * ((width + 7) / 8) + (x / 8);
            int bit_idx = x % 8;

            if (icon[byte_idx] & (1 << bit_idx)) {
                fb_set_pixel(start_x + x, start_y + y, color);
            }
        }
    }
}

static void flush_buffer(void) {
    if (panel_handle && frame_buffer) {
        esp_lcd_panel_draw_bitmap(panel_handle, 0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT, frame_buffer);
    }
}

// =============================================================================
// RENDER FUNCTIONS
// =============================================================================

static void render_idle(bool with_border) {
    fb_clear(COLOR_BLACK);

    if (with_border) {
        fb_draw_border(DND_BORDER_COLOR, DND_BORDER_WIDTH);
    }

    // Friendly name at very top (6px from top, smallest font)
    if (friendly_name[0] != '\0') {
        fb_draw_string_centered(6, friendly_name, JARVIS_BLUE_DARK, 1);
    }

    // Time below friendly name (or at top if no name)
    int time_y = (friendly_name[0] != '\0') ? 18 : 10;
    char time_str[6];
    snprintf(time_str, sizeof(time_str), "%02d:%02d", current_hour, current_minute);
    fb_draw_string_centered(time_y, time_str, JARVIS_BLUE, 2);

    // Temperature in center
    char temp_str[8];
    if (current_temp > -50 && current_temp < 100) {
        snprintf(temp_str, sizeof(temp_str), "%.1f", current_temp);
    } else {
        snprintf(temp_str, sizeof(temp_str), "--.-");
    }
    fb_draw_string_centered(DISPLAY_HEIGHT / 2 - 14, temp_str, JARVIS_BLUE, 4);

    // Celsius symbol
    fb_draw_string(DISPLAY_WIDTH / 2 + 35, DISPLAY_HEIGHT / 2 - 20, "C", JARVIS_BLUE, 2);

    // DND indicator
    if (with_border) {
        fb_draw_string_centered(DISPLAY_HEIGHT - 16, "MUTED", COLOR_RED, 1);
    }

    flush_buffer();
}

static void draw_wave_animation(void) {
    int center_x = DISPLAY_WIDTH / 2;
    int center_y = ICON_CENTER_Y;

    for (int ring = 0; ring < 4; ring++) {
        float ring_phase = wave_phase + ring * 0.5f;
        int base_radius = 50 + ring * 10;

        uint16_t color;
        switch (ring) {
            case 0: color = JARVIS_BLUE; break;
            case 1: color = JARVIS_BLUE_DARK; break;
            case 2: color = 0x010F; break;
            default: color = 0x0007; break;
        }

        for (int angle = 0; angle < 360; angle += 15) {
            float rad = angle * M_PI / 180.0f;
            float wave = sinf(ring_phase + angle * 0.05f) * 5;
            int r = base_radius + (int)wave;

            int x = center_x + (int)(cosf(rad) * r);
            int y = center_y + (int)(sinf(rad) * r);

            fb_set_pixel(x, y, color);
            fb_set_pixel(x + 1, y, color);
            fb_set_pixel(x, y + 1, color);
        }
    }
}

static void render_listening(void) {
    int64_t now = esp_timer_get_time() / 1000;
    if (now - last_wave_update > 50) {  // 50ms animation interval
        wave_phase += 0.3f;
        if (wave_phase > 2 * M_PI) wave_phase -= 2 * M_PI;
        last_wave_update = now;
    }

    fb_clear(COLOR_BLACK);
    draw_wave_animation();
    fb_draw_icon_at(ICON_MIC, ICON_MIC_WIDTH, ICON_MIC_HEIGHT, JARVIS_BLUE, ICON_CENTER_Y);
    fb_draw_string_centered(TEXT_Y, "Listening...", JARVIS_BLUE_DARK, 1);
    flush_buffer();
}

static void render_busy(void) {
    fb_clear(COLOR_BLACK);
    fb_draw_icon_at(ICON_SPEAKER, ICON_SPEAKER_WIDTH, ICON_SPEAKER_HEIGHT, JARVIS_BLUE, ICON_CENTER_Y);
    fb_draw_string_centered(TEXT_Y, "Speaking...", JARVIS_BLUE_DARK, 1);
    flush_buffer();
}

static void render_error(void) {
    fb_clear(COLOR_BLACK);
    fb_draw_string_centered(DISPLAY_HEIGHT / 2 - 15, "ERROR", COLOR_RED, 2);
    fb_draw_string_centered(DISPLAY_HEIGHT / 2 + 15, error_msg, COLOR_RED, 1);
    flush_buffer();
}

// =============================================================================
// PUBLIC API
// =============================================================================

bool jarvis_display_init(void) {
    ESP_LOGI(TAG, "Initializing display...");

    // Log heap disponibile per debug
    ESP_LOGI(TAG, "Free heap: %lu, free DMA: %lu, free PSRAM: %lu",
             (unsigned long)esp_get_free_heap_size(),
             (unsigned long)heap_caps_get_free_size(MALLOC_CAP_DMA),
             (unsigned long)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    // Allocate frame buffer in PSRAM (32KB — risparmia memoria DMA interna)
    frame_buffer = (uint16_t*)heap_caps_malloc(DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t), MALLOC_CAP_SPIRAM);
    if (!frame_buffer) {
        // Fallback: qualsiasi memoria disponibile
        ESP_LOGW(TAG, "PSRAM alloc failed, trying default malloc");
        frame_buffer = (uint16_t*)malloc(DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t));
    }
    if (!frame_buffer) {
        ESP_LOGE(TAG, "Failed to allocate frame buffer");
        return false;
    }
    ESP_LOGI(TAG, "Frame buffer allocated (%d bytes)", DISPLAY_WIDTH * DISPLAY_HEIGHT * 2);

    // Initialize backlight (se disponibile)
    if (PIN_NUM_BL >= 0) {
        gpio_set_direction(PIN_NUM_BL, GPIO_MODE_OUTPUT);
        gpio_set_level(PIN_NUM_BL, 1);
    }

    // Initialize SPI bus
    // max_transfer_sz ridotto: il driver SPI segmenta automaticamente trasferimenti grandi
    spi_bus_config_t buscfg = {
        .mosi_io_num = PIN_NUM_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = PIN_NUM_CLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,  // 4KB — il panel driver segmenta il bitmap
    };

    // AtomS3R usa SPI3_HOST (SPI2 può confliggere con flash/PSRAM su alcune board)
    esp_err_t ret = spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "SPI bus init failed: %s", esp_err_to_name(ret));
        free(frame_buffer);
        frame_buffer = NULL;
        return false;
    }
    ESP_LOGI(TAG, "SPI bus initialized (SPI3_HOST)");
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield to WDT

    // Initialize LCD panel IO
    esp_lcd_panel_io_handle_t io_handle = NULL;
    esp_lcd_panel_io_spi_config_t io_config = {
        .dc_gpio_num = PIN_NUM_DC,
        .cs_gpio_num = PIN_NUM_CS,
        .pclk_hz = 40 * 1000 * 1000,  // 40MHz (come da M5Unified)
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 10,
    };

    ret = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI3_HOST, &io_config, &io_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "LCD panel IO init failed: %s", esp_err_to_name(ret));
        free(frame_buffer);
        frame_buffer = NULL;
        return false;
    }
    ESP_LOGI(TAG, "LCD panel IO initialized");

    // Il GC9107 dell'AtomS3R è compatibile con il driver ST7789 di esp_lcd.
    // I comandi base (MADCTL, COLMOD, set column/row, draw bitmap) sono identici.
    // La differenza è solo nei registri di init specifici del produttore, che
    // vengono inviati manualmente dopo panel_init().
    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = PIN_NUM_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR,  // GC9107 usa BGR
        .bits_per_pixel = 16,
    };

    ret = esp_lcd_new_panel_st7789(io_handle, &panel_config, &panel_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Panel driver init failed: %s", esp_err_to_name(ret));
        free(frame_buffer);
        frame_buffer = NULL;
        return false;
    }
    ESP_LOGI(TAG, "Panel driver created (ST7789-compat for GC9107)");
    vTaskDelay(pdMS_TO_TICKS(10));  // Yield to WDT

    ret = esp_lcd_panel_reset(panel_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Panel reset failed: %s", esp_err_to_name(ret));
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(120));  // GC9107 richiede ~120ms dopo hardware reset

    ret = esp_lcd_panel_init(panel_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Panel init failed: %s", esp_err_to_name(ret));
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(50));  // Post-init stabilizzazione

    // GC9107: comandi vendor-specific post-init (da M5Unified/LovyanGFX)
    // Questi configurano il display 128x128 correttamente
    uint8_t param;
    // Inter register enable 1 & 2
    esp_lcd_panel_io_tx_param(io_handle, 0xFE, NULL, 0);  // Inter Register Enable1
    esp_lcd_panel_io_tx_param(io_handle, 0xEF, NULL, 0);  // Inter Register Enable2
    // Display resolution: 128x128
    param = 0x14;  // 128 lines
    esp_lcd_panel_io_tx_param(io_handle, 0xB4, &param, 1);

    // GC9107 128x128: offset Y=32 (da M5GFX source, il controller ha 128x160 di RAM)
    esp_lcd_panel_set_gap(panel_handle, 0, 32);

    esp_lcd_panel_invert_color(panel_handle, true);
    esp_lcd_panel_disp_on_off(panel_handle, true);

    fb_clear(COLOR_BLACK);
    flush_buffer();

    ESP_LOGI(TAG, "Display initialized");
    return true;
}

void jarvis_display_update(void) {
    switch (current_state) {
        case DISPLAY_STATE_IDLE:
            render_idle(false);
            break;
        case DISPLAY_STATE_DND:
            render_idle(true);
            break;
        case DISPLAY_STATE_LISTENING:
        case DISPLAY_STATE_PROCESSING:
            render_listening();
            break;
        case DISPLAY_STATE_BUSY:
            render_busy();
            break;
        case DISPLAY_STATE_ERROR:
            render_error();
            break;
    }
}

void jarvis_display_set_state(display_state_t state) {
    if (state != current_state) {
        prev_state = current_state;
        current_state = state;
        jarvis_display_update();
    }
}

display_state_t jarvis_display_get_state(void) {
    return current_state;
}

void jarvis_display_set_time(int hour, int minute) {
    current_hour = hour;
    current_minute = minute;
}

void jarvis_display_set_temperature(float temp) {
    current_temp = temp;
}

void jarvis_display_set_error(const char* msg) {
    strncpy(error_msg, msg, sizeof(error_msg) - 1);
    error_msg[sizeof(error_msg) - 1] = '\0';
}

void jarvis_display_show_message(const char* msg) {
    fb_clear(COLOR_BLACK);
    fb_draw_string_centered(DISPLAY_HEIGHT / 2 - 4, msg, JARVIS_BLUE, 1);
    flush_buffer();
}

void jarvis_display_clear(void) {
    fb_clear(COLOR_BLACK);
    flush_buffer();
}

void jarvis_display_set_friendly_name(const char* name) {
    if (name) {
        strncpy(friendly_name, name, sizeof(friendly_name) - 1);
        friendly_name[sizeof(friendly_name) - 1] = '\0';
        ESP_LOGI(TAG, "Friendly name set to: %s", friendly_name);
    } else {
        friendly_name[0] = '\0';
    }
}

void jarvis_display_flash_white(void) {
    if (!frame_buffer || !panel_handle) return;

    // Flash bianco
    fb_clear(COLOR_WHITE);
    flush_buffer();

    // Durata flash: 80ms
    vTaskDelay(pdMS_TO_TICKS(80));

    // Torna allo stato corrente (il chiamante imposterà LISTENING subito dopo)
    jarvis_display_update();
}
