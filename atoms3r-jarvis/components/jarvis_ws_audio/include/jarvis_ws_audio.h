/**
 * =============================================================================
 * JARVIS AtomS3R - WebSocket Audio Module (Persistent)
 * =============================================================================
 *
 * Persistent WebSocket connection to JARVIS server for both:
 * - Audio streaming (Opus-encoded speech, ephemeral sessions)
 * - Control channel (trigger_listen, state updates, keepalive)
 *
 * Protocol:
 *   - Connect:  ws://host:5000/ws/audio?device_id=XX&token=YY
 *   - Device->Server JSON: hello, audio_start, audio_end, state, pong
 *   - Device->Server Binary: Opus frames (20ms each, ~30-80 bytes)
 *   - Server->Device JSON: welcome, ready, speech_end, trigger_listen, tts_done, config_update, ping, error
 *   - Duplex live session only: live_session_start{duplex}, audio_clear, vu, live_session_end
 *     (Device->Server: live_session_end{reason:"button"|"error"}, no call_id: the
 *      wakeword-server adds it and passes the reason through to the orchestrator)
 *
 * Connection lifecycle:
 *   1. jarvis_ws_audio_init()    -- Initialize Opus codec (once at startup)
 *   2. jarvis_ws_audio_connect() -- Start persistent connection task (after WiFi)
 *   3. jarvis_ws_audio_start_session() -- Begin audio streaming (on wake word or trigger)
 *   4. on_session_done callback  -- Audio session ends, connection stays alive
 *   5. goto 3 for next interaction
 *
 * Auto-reconnect with exponential backoff: 1s -> 2s -> 4s -> 8s -> 16s -> 30s max
 */

#ifndef JARVIS_WS_AUDIO_H
#define JARVIS_WS_AUDIO_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Callback when a WS audio session ends (speech captured or error).
 * Called from WS task context (Core 0, priority 6).
 * Implementation MUST be lightweight (set flags, no SPI calls).
 *
 * @param success true if session completed normally (speech detected + processed)
 */
typedef void (*ws_audio_session_done_callback_t)(bool success);

/**
 * Callback when server sends "trigger_listen" command.
 * Called from WS task context (Core 0, priority 6).
 * Implementation MUST be lightweight — just set a flag for main_task to process.
 *
 * @param silent  true = multi-turn follow-up (no wake sound, no speaker suppress)
 *                false = remote enrollment or manual trigger (with wake sound)
 */
typedef void (*ws_trigger_listen_callback_t)(bool silent);

/**
 * Callback when server sends "tts_done" — response delivery is complete.
 * Device should transition from BUSY → IDLE.
 * Called from WS event handler context — keep lightweight.
 */
typedef void (*ws_tts_done_callback_t)(void);

/**
 * Callback when server sends "config_update" with runtime parameters.
 * Called from WS event handler context — keep lightweight.
 *
 * @param wake_word_sensitivity  New sensitivity (0.0-1.0), or -1 if not included
 */
typedef void (*ws_config_update_callback_t)(float wake_word_sensitivity);

/**
 * Callback when server sends "wake_detected" (server-side wake word detection).
 * Called from WS event handler context — keep lightweight (just set a flag).
 * The device should play the wake sound and start an audio session.
 */
typedef void (*ws_wake_detected_callback_t)(void);

/**
 * Initialize WS audio module: Opus encoder/decoder.
 * Call once at startup (after jarvis_codec_init).
 *
 * @return true on success
 */
bool jarvis_ws_audio_init(void);

/**
 * Start persistent WebSocket connection to server.
 * Creates a FreeRTOS task that maintains the connection with auto-reconnect.
 *
 * The connection stays open for the device lifetime. Audio sessions are
 * created/destroyed within this persistent connection.
 *
 * @param device_id    Device MAC address (for URL query param)
 * @param done_cb      Callback when audio session ends
 * @param trigger_cb   Callback when server sends trigger_listen
 * @return true if connection task was created
 */
bool jarvis_ws_audio_connect(const char *device_id,
                              ws_audio_session_done_callback_t done_cb,
                              ws_trigger_listen_callback_t trigger_cb);

/**
 * Register additional callbacks for server commands.
 * Call after jarvis_ws_audio_connect().
 */
void jarvis_ws_audio_set_tts_done_callback(ws_tts_done_callback_t cb);
void jarvis_ws_audio_set_config_update_callback(ws_config_update_callback_t cb);
void jarvis_ws_audio_set_wake_detected_callback(ws_wake_detected_callback_t cb);

/**
 * Start an audio session on the existing persistent connection.
 * Sends {"type":"audio_start"} and begins streaming Opus frames.
 *
 * Does NOT create a new connection — uses the persistent one from connect().
 * If connection is not established, returns false.
 *
 * @return true if audio session was started
 */
bool jarvis_ws_audio_start_session(void);

/**
 * Stop the current audio session. Safe to call if no session active.
 * Connection stays open (only audio streaming stops).
 */
void jarvis_ws_audio_stop_session(void);

/**
 * Check if an audio session is currently active (streaming Opus).
 */
bool jarvis_ws_audio_is_active(void);

/**
 * Check if the persistent WebSocket is connected to the server.
 */
bool jarvis_ws_audio_is_connected(void);

/**
 * Send a state update to the server.
 * Safe to call from any task/core.
 *
 * @param state_str  State string: "idle", "listening", "busy", "dnd", "error"
 */
void jarvis_ws_audio_send_state(const char *state_str);

/**
 * Send speaker_stop command to server (triple-tap emergency stop).
 * Server will stop the output speaker associated with this device.
 * Safe to call from any task/core.
 */
void jarvis_ws_audio_send_speaker_stop(void);

/**
 * Check if a live session is currently active.
 * During live session, audio session timeout is extended and display shows LIVE icon.
 */
bool jarvis_ws_audio_is_live_session(void);

/**
 * Full-duplex live session (Voice Live Hub, via the wakeword-server).
 *
 * Entered on {"type":"live_session_start","duplex":true}, left on
 * {"type":"live_session_end"} (or WS disconnect). While active:
 *   - the mic streams Opus continuously, also while the model speaks
 *   - downlink Opus (model speech) is played by a dedicated task
 *   - {"type":"audio_clear"} drops all queued speech immediately (barge-in)
 *   - {"type":"vu","mic":0..255} carries the post-AEC user level
 * Outside a duplex session nothing of this runs.
 *
 * Device-side ends send {"type":"live_session_end","reason":...}:
 *   "button": single tap, double tap or long press (see jarvis_duplex_policy.h)
 *   "error":  duplex start failed, or no "vu" from the server for 8 s
 *             (the server side of the session is gone)
 */
bool jarvis_ws_audio_is_duplex(void);

/**
 * End the duplex live session from the device (button gestures).
 * Sends {"type":"live_session_end","reason":reason} ("button" or "error",
 * anything else is sent as "button"), stops playback at once and leaves
 * duplex mode. Safe to call from main_task.
 * @return true if a duplex session was active and is now ended
 */
bool jarvis_ws_audio_end_duplex(const char *reason);

/**
 * Current levels for the duplex display (0-255 each).
 * @param jarvis_level  model speech level, aligned to what the speaker plays now
 * @param user_level    post-AEC mic level from the last "vu" message (0 if stale)
 */
void jarvis_ws_audio_get_duplex_levels(uint8_t *jarvis_level, uint8_t *user_level);

/**
 * Disconnect and stop the persistent connection task.
 * Call only on shutdown.
 */
void jarvis_ws_audio_disconnect(void);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_WS_AUDIO_H
