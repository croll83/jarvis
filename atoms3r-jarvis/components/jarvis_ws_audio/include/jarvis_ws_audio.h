/**
 * =============================================================================
 * JARVIS AtomS3R - WebSocket Audio Module
 * =============================================================================
 *
 * Streams Opus-encoded audio to JARVIS server via WebSocket.
 * Replaces WebRTC (libpeer + DTLS-SRTP) with simple WS binary frames.
 *
 * Protocol:
 *   - Connect:  ws://host:5000/ws/audio?device_id=XX&token=YY
 *   - Client->Server: Binary Opus frames (20ms each, ~30-80 bytes)
 *   - Server->Client: JSON control messages (ready, speech_end, error)
 *   - Server closes WebSocket after VAD detects end of speech
 */

#ifndef JARVIS_WS_AUDIO_H
#define JARVIS_WS_AUDIO_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Callback when WS audio session ends.
 * @param success true if session completed normally (speech detected + processed)
 */
typedef void (*ws_audio_session_done_callback_t)(bool success);

/**
 * Initialize WS audio module: Opus encoder/decoder.
 * Call once at startup (after jarvis_codec_init).
 *
 * @return true on success
 */
bool jarvis_ws_audio_init(void);

/**
 * Start a WebSocket audio session.
 *
 * Opens a WebSocket to the server, waits for "ready", then streams
 * Opus-encoded audio from the ring buffer until the server signals
 * "speech_end" or the session times out.
 *
 * @param ws_url    Full URL including query params:
 *                  "ws://host:5000/ws/audio?device_id=XX&token=YY"
 * @param done_cb   Callback when session ends (called from WS task context)
 * @return true if session task was created
 */
bool jarvis_ws_audio_start_session(const char *ws_url,
                                    ws_audio_session_done_callback_t done_cb);

/**
 * Stop the current session. Safe to call if no session active.
 * Blocks up to ~1s waiting for task to finish.
 */
void jarvis_ws_audio_stop_session(void);

/**
 * Check if a session is currently active.
 */
bool jarvis_ws_audio_is_active(void);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_WS_AUDIO_H
