/**
 * =============================================================================
 * JARVIS AtomS3R - Display Module (ESP-IDF)
 * =============================================================================
 *
 * Gestisce il display TFT 128x128 ST7789 con:
 * - Stato IDLE: ora + temperatura
 * - Stato LISTENING: microfono + onde sonore animate
 * - Stato DND: come IDLE ma con bordo rosso
 * - Stato BUSY: icona megafono
 */

#ifndef JARVIS_DISPLAY_H
#define JARVIS_DISPLAY_H

#include <stdint.h>
#include <stdbool.h>

// Use common state definition
// (device_state_t is defined in jarvis_config.h)
typedef int display_state_t;
#define DISPLAY_STATE_IDLE       0
#define DISPLAY_STATE_LISTENING  1
#define DISPLAY_STATE_PROCESSING 2
#define DISPLAY_STATE_BUSY       3
#define DISPLAY_STATE_DND        4
#define DISPLAY_STATE_ERROR      5

/**
 * @brief Initialize display
 * @return true on success
 */
bool jarvis_display_init(void);

/**
 * @brief Update display (call periodically)
 */
void jarvis_display_update(void);

/**
 * @brief Set display state
 * @param state New state
 */
void jarvis_display_set_state(display_state_t state);

/**
 * @brief Get current state
 * @return Current display state
 */
display_state_t jarvis_display_get_state(void);

/**
 * @brief Set time to display
 * @param hour Hour (0-23)
 * @param minute Minute (0-59)
 */
void jarvis_display_set_time(int hour, int minute);

/**
 * @brief Set temperature to display
 * @param temp Temperature in Celsius
 */
void jarvis_display_set_temperature(float temp);

/**
 * @brief Set error message
 * @param msg Error message
 */
void jarvis_display_set_error(const char* msg);

/**
 * @brief Show a centered message (for init screens)
 * @param msg Message to show
 */
void jarvis_display_show_message(const char* msg);

/**
 * @brief Clear display
 */
void jarvis_display_clear(void);

/**
 * @brief Set friendly name to display at top
 * @param name Device friendly name (max 32 chars)
 *
 * Displayed: centered horizontally, 6px from top
 * Uses smallest available font (5-6px height)
 * Does not overlap with mic/speaker icons or temperature
 */
void jarvis_display_set_friendly_name(const char* name);

/**
 * @brief Flash display white briefly (wake word feedback)
 *
 * Riempie lo schermo di bianco per ~80ms, poi torna allo stato corrente.
 * Fornisce feedback visivo immediato della wake word detection.
 */
void jarvis_display_flash_white(void);

/**
 * @brief Flash display red briefly (speaker stop / emergency feedback)
 *
 * Riempie lo schermo di rosso per ~120ms, poi torna allo stato corrente.
 * Fornisce feedback visivo del triple-tap speaker stop.
 */
void jarvis_display_flash_red(void);

/**
 * @brief Start screensaver (Matrix rain animation)
 * Resets animation state. Call once when entering screensaver.
 */
void jarvis_display_screensaver_start(void);

/**
 * @brief Render one screensaver frame (self-throttled to ~20 FPS)
 * Call from main loop every iteration. Returns early if not time to render.
 * @return true if a frame was rendered, false if throttled
 */
bool jarvis_display_screensaver_tick(void);

/**
 * @brief Stop screensaver, clear display
 * Caller should then call jarvis_display_update() to restore normal state.
 */
void jarvis_display_screensaver_stop(void);

/**
 * @brief Set display rotation (180° flip)
 * @param rotated true = 180° rotated, false = normal
 *
 * Flips the frame buffer in software before each flush.
 * Caller should save the rotation state to NVS if needed.
 */
void jarvis_display_set_rotation(bool rotated);

/**
 * @brief Get current rotation state
 * @return true if display is rotated 180°
 */
bool jarvis_display_is_rotated(void);

#endif // JARVIS_DISPLAY_H
