/**
 * =============================================================================
 * JARVIS AtomS3R - Network Module (ESP-IDF)
 * =============================================================================
 *
 * Gestisce:
 * - Connessione WiFi
 * - Configurazione dinamica dal server (device_id = MAC address)
 * - Streaming audio a JARVIS server
 * - Heartbeat periodico
 * - Lettura temperatura da Home Assistant
 * - Polling stato dal server
 */

#ifndef JARVIS_NETWORK_H
#define JARVIS_NETWORK_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// Callback types
typedef void (*server_response_callback_t)(bool success, const char* message);
typedef void (*busy_state_callback_t)(bool busy);
typedef void (*config_update_callback_t)(const char* friendly_name, const char* location_id);

// Device configuration struct
typedef struct {
    char device_id[13];          // MAC address senza separatori (12 chars + null)
    char friendly_name[33];      // Nome configurato (32 chars + null)
    char location_id[33];        // Location ID (32 chars + null)
    bool is_configured;          // true se il server ha restituito un friendly_name
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
 * @brief Get device MAC address as string (formato AABBCCDDEEFF)
 * @param out_device_id Buffer di almeno 13 bytes per il risultato
 * @return true on success
 */
bool jarvis_network_get_device_id(char* out_device_id);

/**
 * @brief Fetch device configuration from server
 * @param device_id MAC address del device
 * @param out_config Struct per memorizzare la configurazione
 * @return true on success (anche se device non configurato)
 */
bool jarvis_network_fetch_config(const char* device_id, device_config_t* out_config);

/**
 * @brief Send heartbeat to server
 * @param device_id MAC address del device
 * @param firmware_version Versione firmware (può essere NULL)
 * @param out_config Struct per ricevere eventuali aggiornamenti config
 * @return true on success
 */
bool jarvis_network_send_heartbeat(const char* device_id, const char* firmware_version, device_config_t* out_config);

/**
 * @brief Set config update callback (chiamato quando config cambia)
 */
void jarvis_network_set_config_callback(config_update_callback_t config_cb);

/**
 * @brief Start audio streaming session
 * @param device_id Device MAC address (nuovo metodo)
 * @return true on success
 */
bool jarvis_network_start_stream(const char* device_id);

/**
 * @brief Send audio chunk during streaming
 * @param chunk Audio samples
 * @param samples Number of samples
 * @return true on success
 */
bool jarvis_network_send_chunk(int16_t* chunk, size_t samples);

/**
 * @brief End streaming session and wait for response
 * @param use_local_speaker Output: true se il server indica di usare lo speaker locale
 * @return true on success
 */
bool jarvis_network_end_stream(bool* use_local_speaker);

/**
 * @brief Check if currently streaming
 */
bool jarvis_network_is_streaming(void);

/**
 * @brief Fetch temperature from Home Assistant
 * @param room Room name (friendly_name del device)
 * @param temp Output temperature value
 * @return true on success
 */
bool jarvis_network_fetch_temperature(const char* room, float* temp);

/**
 * @brief Notify server of DND mode change
 * @param device_id MAC address del device
 * @param enabled DND enabled/disabled
 */
void jarvis_network_notify_dnd(const char* device_id, bool enabled);

/**
 * @brief Poll server state (for busy detection)
 * @param device_id MAC address del device
 */
void jarvis_network_poll_state(const char* device_id);

/**
 * @brief Set callbacks
 */
void jarvis_network_set_callbacks(
    server_response_callback_t response_cb,
    busy_state_callback_t busy_cb
);

/**
 * @brief Send speaker suppress request to server (fire-and-forget)
 *
 * Chiamato alla wake word detection per abbassare il volume dello speaker
 * Echo associato a questo device. Il server ripristinerà il volume
 * automaticamente dopo lo STT.
 *
 * @param device_id MAC address del device
 */
void jarvis_network_suppress_speaker(const char* device_id);

#endif // JARVIS_NETWORK_H
