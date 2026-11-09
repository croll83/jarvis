/**
 * Host unit tests for components/jarvis_ws_audio/include/jarvis_live_wake_policy.h
 * (live from the wake word / the tap: decisions that do not need FreeRTOS or hardware).
 * The expected values follow the wakeword-server contract (server.py,
 * tests/test_live_wake.py) and design 19.3.
 *
 * Run: test/host/run_tests.sh   (builds as C and as C++, like main.cpp includes it)
 */

#include <stdio.h>
#include "jarvis_live_wake_policy.h"

static int checks = 0;
static int failures = 0;

#define CHECK(cond) do { \
    checks++; \
    if (!(cond)) { failures++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

#define CHECK_STR(a, b) CHECK(strcmp((a), (b)) == 0)

#define WRAP_NEAR 0xFFFFFF00u   // 256 ms before the uint32_t ms counter wraps

// wake_detected opens the live branch only with live:true and a valid req_id
static void test_wake_is_live(void) {
    char long_id[LIVE_REQ_ID_MAX + 2];
    memset(long_id, 'a', sizeof(long_id) - 1);
    long_id[sizeof(long_id) - 1] = '\0';          // 65 chars: the server would refuse it

    CHECK(live_wake_is_live(true, "3f2a9c0d11e84b7a"));
    // Older servers: plain wake_detected (no live, no req_id) = classic of today
    CHECK(!live_wake_is_live(false, NULL));
    CHECK(!live_wake_is_live(false, "3f2a9c0d11e84b7a"));
    // live without a usable req_id: cannot be acked, classic
    CHECK(!live_wake_is_live(true, NULL));
    CHECK(!live_wake_is_live(true, ""));
    CHECK(!live_wake_is_live(true, long_id));
    long_id[LIVE_REQ_ID_MAX] = '\0';               // exactly 64: fine
    CHECK(live_wake_is_live(true, long_id));
}

// live_wake_ack{status}: DND first, then busy, else ok
static void test_ack(void) {
    CHECK(live_wake_ack(false, false) == LIVE_ACK_OK);
    CHECK(live_wake_ack(true, false) == LIVE_ACK_DND);
    CHECK(live_wake_ack(false, true) == LIVE_ACK_BUSY);
    CHECK(live_wake_ack(true, true) == LIVE_ACK_DND);   // in DND nothing starts, as today

    // Exact strings the server reads (status != "ok" -> live_abort)
    CHECK_STR(live_ack_status(LIVE_ACK_OK), "ok");
    CHECK_STR(live_ack_status(LIVE_ACK_DND), "dnd");
    CHECK_STR(live_ack_status(LIVE_ACK_BUSY), "busy");
}

static void test_busy(void) {
    CHECK(!live_device_busy(false, false, false, false, false));
    CHECK(live_device_busy(true, false, false, false, false));    // classic turn streaming
    CHECK(live_device_busy(false, true, false, false, false));    // audio_start requested
    CHECK(live_device_busy(false, false, true, false, false));    // duplex session
    CHECK(live_device_busy(false, false, false, true, false));    // old live session loop
    CHECK(live_device_busy(false, false, false, false, true));    // another request pending
}

// Tap in idle: live only when the server side said so; classic otherwise
static void test_tap_allowed(void) {
    CHECK(live_tap_allowed(true, true, false, true));
    // Older server / live off (no live_on_wake:true seen): classic tap of today
    CHECK(!live_tap_allowed(false, true, false, true));
    CHECK(!live_tap_allowed(true, false, false, true));   // WS down
    CHECK(!live_tap_allowed(true, true, true, true));     // something running
    CHECK(!live_tap_allowed(true, true, false, false));   // classic reply being delivered
}

// Exit messages carry the req_id of the request they close
static void test_req_matches(void) {
    CHECK(live_req_matches("abc123", "abc123"));
    CHECK(!live_req_matches("abc123", "abc124"));          // stale exit (an older request)
    CHECK(!live_req_matches("abc123", "abc1234"));
    CHECK(live_req_matches("abc123", NULL));               // no req_id: taken as ours
    CHECK(live_req_matches("abc123", ""));
    CHECK(!live_req_matches(NULL, "abc123"));
}

static void test_make_req_id(void) {
    char id[17];
    live_make_req_id(0x0123ABCDu, 0xFFFFFFFFu, id);
    CHECK_STR(id, "0123abcdffffffff");
    CHECK(live_req_id_valid(id));
    live_make_req_id(0, 0, id);
    CHECK_STR(id, "0000000000000000");
    CHECK(strlen(id) == 16);
}

// live_declined: duplicate / voice_gate rest silently, every other reason is classic
static void test_declined(void) {
    CHECK(live_declined_outcome("duplicate") == LIVE_OUT_IDLE);
    CHECK(live_declined_outcome("voice_gate") == LIVE_OUT_IDLE);
    CHECK(live_declined_outcome("gate") == LIVE_OUT_IDLE);
    // Orchestrator reasons (design 19.2) and the wakeword-server's own
    CHECK(live_declined_outcome("disabled") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("hub_off") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("hub_down") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("busy") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("enrollment") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("timeout") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("relay") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("hub_rejected") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("hub_unreachable") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("error") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("") == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome(NULL) == LIVE_OUT_CLASSIC);
    CHECK(live_declined_outcome("Duplicate") == LIVE_OUT_CLASSIC);  // exact match only
}

// 15 s safety timeout: wake -> live_session_end{"error"} + rest, tap -> classic
static void test_timeout(void) {
    CHECK(live_timeout_outcome(LIVE_ORIGIN_WAKE) == LIVE_OUT_IDLE);
    CHECK(live_timeout_outcome(LIVE_ORIGIN_TAP) == LIVE_OUT_CLASSIC);

    CHECK(!live_pending_expired(1000, 1000, LIVE_PENDING_SAFETY_MS));
    CHECK(!live_pending_expired(15999, 1000, LIVE_PENDING_SAFETY_MS));
    CHECK(live_pending_expired(16000, 1000, LIVE_PENDING_SAFETY_MS));
    CHECK(live_pending_expired(90000, 1000, LIVE_PENDING_SAFETY_MS));
    // Across the ms wrap
    CHECK(!live_pending_expired(0x100u, WRAP_NEAR, LIVE_PENDING_SAFETY_MS));
    CHECK(live_pending_expired(WRAP_NEAR + LIVE_PENDING_SAFETY_MS, WRAP_NEAR, LIVE_PENDING_SAFETY_MS));
    // Longer than the server's worst case: ack 1 + orchestrator 2.5 + connect 3 + accept 7 s
    CHECK(LIVE_PENDING_SAFETY_MS > 1000u + 2500u + 3000u + 7000u);
}

// Duplex start already in flight when the device dropped the request
static void test_start_after_drop(void) {
    // Within the guard, nothing pending, duplex: ignored
    CHECK(live_start_after_drop(false, true, true, 1000, 1000));
    CHECK(live_start_after_drop(false, true, true, 1000 + LIVE_DROP_GUARD_MS - 1, 1000));
    // Guard over
    CHECK(!live_start_after_drop(false, true, true, 1000 + LIVE_DROP_GUARD_MS, 1000));
    CHECK(!live_start_after_drop(false, true, true, 900000, 1000));
    // A request pending: it is that request's start
    CHECK(!live_start_after_drop(true, true, true, 1000, 1000));
    // Not duplex (old live session loop) or no drop: as today
    CHECK(!live_start_after_drop(false, false, true, 1000, 1000));
    CHECK(!live_start_after_drop(false, true, false, 1000, 1000));
    // Across the ms wrap
    CHECK(live_start_after_drop(false, true, true, 0x100u, WRAP_NEAR));
    CHECK(!live_start_after_drop(false, true, true, WRAP_NEAR + LIVE_DROP_GUARD_MS, WRAP_NEAR));
    // Covers the classic activation (flash 80 + wake sound 500 ms) before audio_start
    CHECK(LIVE_DROP_GUARD_MS > 80u + 500u + 500u);
}

// Gestures while pending (design 19.3 point 6, server tests in test_live_wake.py)
static void test_pending_gestures(void) {
    // Nothing pending: every gesture as today
    CHECK(live_pending_button_action(LIVE_GESTURE_SINGLE_TAP, false) == LIVE_BTN_AS_TODAY);
    CHECK(live_pending_button_action(LIVE_GESTURE_DOUBLE_TAP, false) == LIVE_BTN_AS_TODAY);
    CHECK(live_pending_button_action(LIVE_GESTURE_TRIPLE_TAP, false) == LIVE_BTN_AS_TODAY);
    CHECK(live_pending_button_action(LIVE_GESTURE_LONG_PRESS, false) == LIVE_BTN_AS_TODAY);

    // Single tap: classic activation (server: live_cancel, then audio_start)
    CHECK(live_pending_button_action(LIVE_GESTURE_SINGLE_TAP, true) == LIVE_BTN_CLASSIC);
    // Double tap: today's speaker_stop (server: live_cancel, then speaker_stop)
    CHECK(live_pending_button_action(LIVE_GESTURE_DOUBLE_TAP, true) == LIVE_BTN_DROP_THEN_AS_TODAY);
    // Long press: live_session_end{"button"} (server: live_cancel, rest), then DND
    CHECK(live_pending_button_action(LIVE_GESTURE_LONG_PRESS, true) == LIVE_BTN_CANCEL_THEN_AS_TODAY);
    // Triple tap (rotate display): unchanged, the request goes on
    CHECK(live_pending_button_action(LIVE_GESTURE_TRIPLE_TAP, true) == LIVE_BTN_AS_TODAY);
}

int main(void) {
    test_wake_is_live();
    test_ack();
    test_busy();
    test_tap_allowed();
    test_req_matches();
    test_make_req_id();
    test_declined();
    test_timeout();
    test_start_after_drop();
    test_pending_gestures();

    printf("%s: %d checks, %d failed\n", failures ? "FAILED" : "OK", checks, failures);
    return failures ? 1 : 0;
}
