/**
 * =============================================================================
 * JARVIS AtomS3R - Live from the wake word / the tap (pure logic)
 * =============================================================================
 *
 * Voice Live Hub, Phase 3 item 1 (design 19.2, 19.3, 19.12): "ciao jarvis"
 * (detected by the wakeword-server) and the tap on the Atom open the Gemini
 * Live session directly, without the classic turn "avvia una sessione live".
 * The decisions that do not depend on FreeRTOS, the WebSocket client or the
 * display live here, so they can be unit tested on the host (test/host).
 * Used by jarvis_ws_audio.c and main.cpp.
 *
 * Contract with the wakeword-server (server.py, tests/test_live_wake.py):
 *   hello caps        ["aec_ref", "live_wake"]
 *   server wake       <- wake_detected{live:true, req_id}
 *                     -> live_wake_ack{req_id, status:"ok"|"dnd"|"busy"}
 *   tap               -> live_start{req_id, trigger:"tap"}   (req_id minted here)
 *   exits (one only)  <- live_session_start{duplex:true,...}  duplex as today
 *                     <- wake_detected (no live)              classic turn
 *                     <- live_declined{req_id, reason}        tap only: classic,
 *                                                             or rest (silent reasons)
 *                     <- live_abort{req_id}                   rest, silently
 *   device gives up   -> audio_start          single tap: classic (server sends
 *                                             live_cancel before relaying it)
 *                     -> speaker_stop         double tap (live_cancel, then today's)
 *                     -> live_session_end{reason:"button"}  long press (then DND)
 *                     -> live_session_end{reason:"error"}   15 s safety timeout
 *                                                           of a server wake
 *   The command said in the same breath ("ciao jarvis, accendi la luce") is the
 *   continuous mic stream, buffered by the server while pending: the firmware
 *   keeps streaming and does nothing else for it.
 *
 * Timestamps are uint32_t milliseconds, compared wrap-safe.
 */

#ifndef JARVIS_LIVE_WAKE_POLICY_H
#define JARVIS_LIVE_WAKE_POLICY_H

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

// Firmware safety timeout of a pending live request. Longer than the server's
// worst case (ack 1 s + orchestrator 2.5 s + hub connect 3 s + late accept 7 s).
#define LIVE_PENDING_SAFETY_MS  15000u

// The server accepts req_id strings of 1..64 chars
#define LIVE_REQ_ID_MAX         64

typedef enum {
    LIVE_ORIGIN_WAKE = 0,   // wake_detected{live} from the wakeword-server
    LIVE_ORIGIN_TAP,        // single tap in idle, live_start from the device
} live_origin_t;

typedef enum {
    LIVE_ACK_OK = 0,
    LIVE_ACK_DND,
    LIVE_ACK_BUSY,
} live_ack_t;

typedef enum {
    LIVE_OUT_CLASSIC = 0,   // classic activation as today (wake sound, suppress, audio_start)
    LIVE_OUT_IDLE,          // back to rest, nothing else
} live_outcome_t;

// Gestures while a live request is pending ("connecting")
typedef enum {
    LIVE_GESTURE_SINGLE_TAP = 0,
    LIVE_GESTURE_DOUBLE_TAP,
    LIVE_GESTURE_TRIPLE_TAP,
    LIVE_GESTURE_LONG_PRESS,
} live_gesture_t;

typedef enum {
    LIVE_BTN_AS_TODAY = 0,          // No request pending (or gesture unaffected)
    LIVE_BTN_CLASSIC,               // Drop the request, classic activation (its
                                    // audio_start makes the server send live_cancel)
    LIVE_BTN_DROP_THEN_AS_TODAY,    // Drop the request, then today's action, which
                                    // already tells the server (speaker_stop)
    LIVE_BTN_CANCEL_THEN_AS_TODAY,  // Drop the request, live_session_end{"button"},
                                    // then today's action (DND toggle)
} live_button_action_t;

// -----------------------------------------------------------------------------
// Requests
// -----------------------------------------------------------------------------

static inline bool live_req_id_valid(const char *req_id) {
    if (!req_id || !req_id[0]) return false;
    return strlen(req_id) <= LIVE_REQ_ID_MAX;
}

// A wake_detected opens the live branch only with live:true and a valid req_id;
// anything else (older servers, plain wake) is the classic wake of today.
static inline bool live_wake_is_live(bool live_flag, const char *req_id) {
    return live_flag && live_req_id_valid(req_id);
}

// An exit message (live_abort, live_declined) belongs to the pending request if
// it carries the same req_id. Without a req_id it is taken as ours (robustness:
// the server always sends it). With another req_id it is stale and ignored.
static inline bool live_req_matches(const char *pending, const char *msg_req_id) {
    if (!msg_req_id || !msg_req_id[0]) return true;
    return pending && strcmp(pending, msg_req_id) == 0;
}

// req_id of a tap: 16 lowercase hex chars from two random words (out >= 17 bytes)
static inline void live_make_req_id(uint32_t a, uint32_t b, char *out) {
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 8; i++) {
        out[i] = hex[(a >> (28 - 4 * i)) & 0xF];
        out[8 + i] = hex[(b >> (28 - 4 * i)) & 0xF];
    }
    out[16] = '\0';
}

// -----------------------------------------------------------------------------
// Ack of wake_detected{live}
// -----------------------------------------------------------------------------

// Busy = the device cannot take a live session now: an audio turn (or its start
// already requested), a duplex session, the old live session loop, or another
// pending request.
static inline bool live_device_busy(bool audio_active, bool audio_requested, bool duplex,
                                    bool live_session, bool pending) {
    return audio_active || audio_requested || duplex || live_session || pending;
}

// DND wins over busy: in DND nothing starts, as today with the activation ignored
static inline live_ack_t live_wake_ack(bool dnd, bool busy) {
    if (dnd) return LIVE_ACK_DND;
    if (busy) return LIVE_ACK_BUSY;
    return LIVE_ACK_OK;
}

static inline const char *live_ack_status(live_ack_t ack) {
    switch (ack) {
        case LIVE_ACK_DND:  return "dnd";
        case LIVE_ACK_BUSY: return "busy";
        default:            return "ok";
    }
}

// -----------------------------------------------------------------------------
// Tap in idle
// -----------------------------------------------------------------------------

// The tap asks for the live session only if the server side said live is on
// (live_on_wake:true in the orchestrator's welcome, forwarded by the
// wakeword-server, or a wake_detected{live} seen on this connection), the WS is
// up, nothing is running and the device is at rest (IDLE/DND/ERROR: not while a
// classic reply is still being delivered). Otherwise: the classic tap of today.
// Older servers never send live_on_wake, so the tap stays classic there.
static inline bool live_tap_allowed(bool server_live, bool connected, bool busy, bool at_rest) {
    return server_live && connected && !busy && at_rest;
}

// -----------------------------------------------------------------------------
// Exits
// -----------------------------------------------------------------------------

// live_declined: the reasons that mean "someone else has it / it was not for us"
// go back to rest without a classic turn (the server maps them to live_abort, the
// firmware accepts both); every other reason (disabled, busy, hub_off, hub_down,
// enrollment, timeout, relay, hub_rejected, hub_unreachable, error, ...) falls
// back to the classic turn.
static inline live_outcome_t live_declined_outcome(const char *reason) {
    if (reason && (strcmp(reason, "duplicate") == 0 || strcmp(reason, "voice_gate") == 0 ||
                   strcmp(reason, "gate") == 0)) {
        return LIVE_OUT_IDLE;
    }
    return LIVE_OUT_CLASSIC;
}

// Safety timeout. Server wake: live_session_end{reason:"error"} and rest (design
// 19.2). Tap: classic activation (its audio_start makes the server cancel).
static inline live_outcome_t live_timeout_outcome(live_origin_t origin) {
    return origin == LIVE_ORIGIN_TAP ? LIVE_OUT_CLASSIC : LIVE_OUT_IDLE;
}

static inline bool live_pending_expired(uint32_t now, uint32_t started, uint32_t timeout_ms) {
    return (int32_t)(now - started) >= (int32_t)timeout_ms;
}

// After the device drops a pending request (gesture, safety timeout) the server
// may already have sent the duplex live_session_start for it: the hub accepted
// before the server read the drop. The message that tells the server (audio_start
// ~0.6 s later, after the wake sound; speaker_stop; live_session_end) makes it
// close that hub session, so the start is ignored here instead of putting the
// device in duplex in the middle of a classic turn. Covers the time until the
// drop message is out plus a round trip; a live session started by a classic
// turn ("avvia una sessione live") never comes this soon.
#define LIVE_DROP_GUARD_MS      2000u

static inline bool live_start_after_drop(bool pending, bool duplex, bool drop_armed,
                                         uint32_t now, uint32_t dropped_ms) {
    if (pending || !duplex || !drop_armed) return false;
    return (uint32_t)(now - dropped_ms) < LIVE_DROP_GUARD_MS;
}

// -----------------------------------------------------------------------------
// Gestures while pending (design 19.3 point 6, server _handle_live_pending_device_text)
// -----------------------------------------------------------------------------

// Single tap: today's classic activation (the server sends live_cancel before
// relaying the audio_start). Double tap: today's speaker_stop (the server cancels
// first). Long press: live_session_end{"button"} (the server cancels and rests),
// then today's DND toggle. Triple tap (rotate display): unchanged, the request
// goes on.
static inline live_button_action_t live_pending_button_action(live_gesture_t gesture, bool pending) {
    if (!pending) return LIVE_BTN_AS_TODAY;
    switch (gesture) {
        case LIVE_GESTURE_SINGLE_TAP:
            return LIVE_BTN_CLASSIC;
        case LIVE_GESTURE_DOUBLE_TAP:
            return LIVE_BTN_DROP_THEN_AS_TODAY;
        case LIVE_GESTURE_LONG_PRESS:
            return LIVE_BTN_CANCEL_THEN_AS_TODAY;
        default:
            return LIVE_BTN_AS_TODAY;
    }
}

#endif // JARVIS_LIVE_WAKE_POLICY_H
