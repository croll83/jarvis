/**
 * =============================================================================
 * JARVIS AtomS3R - Network Module (ESP-IDF)
 * =============================================================================
 *
 * Manages:
 * - WiFi connection
 * - Dynamic configuration from server (device_id = MAC address)
 * - Heartbeat
 * - Temperature reading from Home Assistant
 * - Server state polling
 * - WebSocket audio URL helper
 *
 * Audio streaming is handled by jarvis_ws_audio (WebSocket + Opus).
 */

#ifndef JARVIS_NETWORK_H
#define JARVIS_NETWORK_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Callback types
typedef void (*server_response_callback_t)(bool success, const char* message);
typedef void (*busy_state_callback_t)(bool busy);
typedef void (*config_update_callback_t)(const char* friendly_name, const char* location_id);

// Device configuration struct
typedef struct {
    char device_id[13];          // MAC address (12 chars + null)
    char friendly_name[33];      // Configured name (32 chars + null)
    char location_id[33];        // Location ID (32 chars + null)
    bool is_configured;          // true if server returned a friendly_name
} device_config_t;

/**
 * @brief Initialize network (WiFi connection)
 * @return true on success
 */
bool jarvis_network_init(void);

/**
 * @brief Deinitialize network
 */
void jarvis_network_deinit(void);

/**
 * @brief Check if WiFi is connected
 */
bool jarvis_network_is_connected(void);

/**
 * @brief Get device MAC address as string (format AABBCCDDEEFF)
 * @param out_device_id Buffer of at least 13 bytes
 * @return true on success
 */
bool jarvis_network_get_device_id(char* out_device_id);

/**
 * @brief Fetch device configuration from server
 */
bool jarvis_network_fetch_config(const char* device_id, device_config_t* out_config);

/**
 * @brief Send heartbeat to server
 */
bool jarvis_network_send_heartbeat(const char* device_id, const char* firmware_version, device_config_t* out_config);

/**
 * @brief Set config update callback
 */
void jarvis_network_set_config_callback(config_update_callback_t config_cb);

/**
 * @brief Fetch temperature from Home Assistant
 */
bool jarvis_network_fetch_temperature(const char* room, float* temp);

/**
 * @brief Notify server of DND mode change
 */
void jarvis_network_notify_dnd(const char* device_id, bool enabled);

/**
 * @brief Poll server state (for busy detection)
 */
void jarvis_network_poll_state(const char* device_id);

/**
 * @brief Set callbacks for server responses and busy state
 */
void jarvis_network_set_callbacks(
    server_response_callback_t response_cb,
    busy_state_callback_t busy_cb
);

/**
 * @brief Send speaker suppress request to server (fire-and-forget)
 */
void jarvis_network_suppress_speaker(const char* device_id);

/**
 * @brief Build WebSocket audio URL with device_id and token in query params.
 * @param device_id Device MAC address
 * @param buf Output buffer (at least 384 bytes)
 * @param size Buffer size
 * Produces: "ws://host:port/ws/audio?device_id=XX&token=YY"
 */
void jarvis_network_get_ws_audio_url(const char *device_id, char *buf, size_t size);

/**
 * @brief Get the configured API token.
 * @return API token string (may be empty)
 */
const char* jarvis_network_get_api_token(void);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_NETWORK_H
