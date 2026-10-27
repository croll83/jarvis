/**
 * =============================================================================
 * JARVIS AtomS3R - Full-duplex live session policy (pure logic)
 * =============================================================================
 *
 * Decisions of the duplex live session (Voice Live Hub via the wakeword-server)
 * that do not depend on FreeRTOS, I2S or the WebSocket client, kept here so they
 * can be unit tested on the host (test/host). Used by jarvis_ws_audio.c and
 * main.cpp.
 *
 * Timestamps are uint32_t milliseconds: 32-bit loads and stores are atomic on
 * the Xtensa, so values shared between tasks never tear, and every comparison
 * below is wrap-safe.
 */

#ifndef JARVIS_DUPLEX_POLICY_H
#define JARVIS_DUPLEX_POLICY_H

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

// -----------------------------------------------------------------------------
// Wrap-safe millisecond helpers
// -----------------------------------------------------------------------------

// a - b, correct across the uint32_t wrap as long as |a - b| < 2^31 ms
static inline int32_t duplex_ms_diff(uint32_t a, uint32_t b) {
    return (int32_t)(a - b);
}

// true once now has reached deadline
static inline bool duplex_ms_reached(uint32_t now, uint32_t deadline) {
    return duplex_ms_diff(now, deadline) >= 0;
}

// -----------------------------------------------------------------------------
// Button gestures during a duplex session
// -----------------------------------------------------------------------------

typedef enum {
    DUPLEX_GESTURE_SINGLE_TAP = 0,
    DUPLEX_GESTURE_DOUBLE_TAP,
    DUPLEX_GESTURE_TRIPLE_TAP,
    DUPLEX_GESTURE_LONG_PRESS,
} duplex_gesture_t;

typedef enum {
    DUPLEX_BTN_AS_TODAY = 0,        // Not in a duplex session (or gesture unaffected)
    DUPLEX_BTN_END,                 // End the session, nothing else
    DUPLEX_BTN_END_THEN_AS_TODAY,   // End the session, then the usual action
    DUPLEX_BTN_INTERRUPT,           // Silence Jarvis, the session goes on
} duplex_button_action_t;

// Single tap silences Jarvis (voice barge-in is not possible on the Echo Base:
// the speaker reaches the mic far above the user's voice, so the server keeps the
// uplink closed while it plays); double tap ends the session; long press ends it
// and then toggles DND as today; triple tap (rotate display) is unchanged.
static inline duplex_button_action_t duplex_button_action(duplex_gesture_t gesture, bool duplex) {
    if (!duplex) return DUPLEX_BTN_AS_TODAY;
    switch (gesture) {
        case DUPLEX_GESTURE_SINGLE_TAP:
            return DUPLEX_BTN_INTERRUPT;
        case DUPLEX_GESTURE_DOUBLE_TAP:
            return DUPLEX_BTN_END;
        case DUPLEX_GESTURE_LONG_PRESS:
            return DUPLEX_BTN_END_THEN_AS_TODAY;
        default:
            return DUPLEX_BTN_AS_TODAY;
    }
}

// Reason of a device-side end: "button" or "error" only (anything else = "button")
static inline const char *duplex_end_reason(const char *reason) {
    if (reason && strcmp(reason, "error") == 0) return "error";
    return "button";
}

// -----------------------------------------------------------------------------
// Downlink routing (binary WS frames)
// -----------------------------------------------------------------------------

typedef enum {
    DUPLEX_RX_LEGACY = 0,   // Today's path: decode + jarvis_codec_write in the WS handler
    DUPLEX_RX_QUEUE,        // Duplex session: decode into the play queue
    DUPLEX_RX_DROP,         // Drop the frame
} duplex_rx_route_t;

// play_task_alive: the play task of a session that just ended is still
//   draining or muting; only one writer may feed the I2S TX ring at a time.
// late_drop_armed/until: the device ended the session, stray model speech
//   still in flight is dropped until then.
// Outside duplex (never started): always DUPLEX_RX_LEGACY.
static inline duplex_rx_route_t duplex_rx_route(bool duplex_active, bool play_task_alive,
                                                bool late_drop_armed, uint32_t now,
                                                uint32_t late_drop_until) {
    if (duplex_active) return DUPLEX_RX_QUEUE;
    if (play_task_alive) return DUPLEX_RX_DROP;
    if (late_drop_armed && !duplex_ms_reached(now, late_drop_until)) return DUPLEX_RX_DROP;
    return DUPLEX_RX_LEGACY;
}

// The late-drop window must be disarmed once it has passed: the wrap-safe
// comparison reads "not reached" again 2^31 ms (about 24.9 days) later, and an
// idle Atom would then drop every normal TTS reply for another 24.9 days.
static inline bool duplex_late_drop_expired(bool armed, uint32_t now, uint32_t until) {
    return armed && duplex_ms_reached(now, until);
}

// -----------------------------------------------------------------------------
// Playback clock and barge-in
// -----------------------------------------------------------------------------

// How far the I2S TX ring runs ahead of the speaker: audio written since the
// clock started minus the time elapsed. Negative = the ring ran dry.
static inline int32_t duplex_play_lead_ms(uint32_t now, uint32_t clock_start,
                                          uint32_t written_samples, uint32_t samples_per_ms) {
    return (int32_t)(written_samples / samples_per_ms) - duplex_ms_diff(now, clock_start);
}

// When to unmute the DAC after a barge-in or a device-side end: once the stale
// audio still in the TX ring has played out. lead_ms comes from the playback
// clock, bounded by the ring size; margin covers the estimate's error.
static inline uint32_t duplex_unmute_at(uint32_t now, int32_t lead_ms,
                                        uint32_t ring_ms, uint32_t margin_ms) {
    if (lead_ms < 0) lead_ms = 0;
    if ((uint32_t)lead_ms > ring_ms) lead_ms = (int32_t)ring_ms;
    return now + (uint32_t)lead_ms + margin_ms;
}

// Deadline of a (new) DAC mute: a mute already running is extended, never shortened
static inline uint32_t duplex_mute_deadline(bool muted, uint32_t current_deadline,
                                            uint32_t new_deadline) {
    if (muted && duplex_ms_diff(current_deadline, new_deadline) > 0) return current_deadline;
    return new_deadline;
}

// Frames carry the barge-in generation current when they were queued: only
// frames from before the last audio_clear are stale.
static inline bool duplex_frame_is_stale(uint32_t frame_gen, uint32_t clear_gen) {
    return frame_gen != clear_gen;
}

// -----------------------------------------------------------------------------
// Levels, vu and liveness
// -----------------------------------------------------------------------------

// A speaker level counts for the display frame if its frame reaches the
// speaker within the last window_ms.
static inline bool duplex_level_in_window(uint32_t now, uint32_t play_ms, uint32_t window_ms) {
    int32_t age = duplex_ms_diff(now, play_ms);
    return age >= 0 && age < (int32_t)window_ms;
}

// A "vu" value is shown while younger than stale_ms
static inline bool duplex_vu_fresh(uint32_t now, uint32_t vu_ms, uint32_t stale_ms) {
    int32_t age = duplex_ms_diff(now, vu_ms);
    return age >= 0 && age < (int32_t)stale_ms;
}

// The wakeword-server sends "vu" about every 100 ms for the whole hub session
// (driven by the mic uplink). Nothing for timeout_ms (counted from the session
// start until the first one) = the server side of the session is gone.
static inline bool duplex_watchdog_expired(uint32_t now, uint32_t last_vu_ms, uint32_t timeout_ms) {
    return duplex_ms_diff(now, last_vu_ms) >= (int32_t)timeout_ms;
}

#endif // JARVIS_DUPLEX_POLICY_H
