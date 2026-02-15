/**
 * =============================================================================
 * JARVIS AtomS3R - WebRTC Module
 * =============================================================================
 *
 * Manages WebRTC peer connection with JARVIS server:
 * - Opus encoding of microphone audio (from jarvis_audio ring buffer)
 * - Opus decoding of server audio (to jarvis_codec speaker output)
 * - DTLS-SRTP transport via libpeer
 * - SDP signaling via HTTP POST to /webrtc/offer
 *
 * Lifecycle:
 *   1. jarvis_webrtc_init() — create Opus encoder/decoder
 *   2. jarvis_webrtc_start_session() — create PeerConnection, signal, connect
 *   3. Audio flows bidirectionally until session ends
 *   4. done_callback fires → caller re-enables wake word listening
 *   5. jarvis_webrtc_stop_session() — explicit stop if needed
 */

#ifndef JARVIS_WEBRTC_H
#define JARVIS_WEBRTC_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Callback when WebRTC session ends (either success or failure).
 * @param success true if session completed normally, false on error
 */
typedef void (*webrtc_session_done_callback_t)(bool success);

/**
 * @brief Initialize WebRTC module: Opus encoder/decoder.
 * Must be called once at startup (after jarvis_codec_init).
 * @return true on success
 */
bool jarvis_webrtc_init(void);

/**
 * @brief Start a WebRTC session with the JARVIS server.
 *
 * Creates PeerConnection, performs SDP signaling via HTTP POST,
 * and starts audio streaming when connected.
 *
 * @param signaling_url Full URL for SDP offer (e.g. "https://server:5000/webrtc/offer")
 * @param device_id     Device MAC address for X-Device-Id header
 * @param api_token     Bearer token for Authorization header (can be empty)
 * @param done_cb       Callback when session ends
 * @return true if session start initiated successfully
 */
bool jarvis_webrtc_start_session(const char *signaling_url,
                                  const char *device_id,
                                  const char *api_token,
                                  webrtc_session_done_callback_t done_cb);

/**
 * @brief Stop the current WebRTC session.
 * Safe to call even if no session is active.
 */
void jarvis_webrtc_stop_session(void);

/**
 * @brief Check if a WebRTC session is currently active.
 */
bool jarvis_webrtc_is_active(void);

#ifdef __cplusplus
}
#endif

#endif // JARVIS_WEBRTC_H
