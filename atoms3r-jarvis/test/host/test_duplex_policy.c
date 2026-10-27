/**
 * Host unit tests for components/jarvis_ws_audio/include/jarvis_duplex_policy.h
 * (full-duplex live session decisions that do not need FreeRTOS or hardware).
 *
 * Run: test/host/run_tests.sh   (builds as C and as C++, like main.cpp includes it)
 */

#include <stdio.h>
#include "jarvis_duplex_policy.h"

static int checks = 0;
static int failures = 0;

#define CHECK(cond) do { \
    checks++; \
    if (!(cond)) { failures++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

#define CHECK_STR(a, b) CHECK(strcmp((a), (b)) == 0)

#define WRAP_NEAR 0xFFFFFF00u   // 256 ms before the uint32_t ms counter wraps

// D6 / D7: button gestures during a duplex session; nothing changes outside it
static void test_button_gestures(void) {
    // Feature off / no session: every gesture as today
    CHECK(duplex_button_action(DUPLEX_GESTURE_SINGLE_TAP, false) == DUPLEX_BTN_AS_TODAY);
    CHECK(duplex_button_action(DUPLEX_GESTURE_DOUBLE_TAP, false) == DUPLEX_BTN_AS_TODAY);
    CHECK(duplex_button_action(DUPLEX_GESTURE_TRIPLE_TAP, false) == DUPLEX_BTN_AS_TODAY);
    CHECK(duplex_button_action(DUPLEX_GESTURE_LONG_PRESS, false) == DUPLEX_BTN_AS_TODAY);

    // Single tap ends the session (contract 18.1)
    CHECK(duplex_button_action(DUPLEX_GESTURE_SINGLE_TAP, true) == DUPLEX_BTN_END);
    // D6: double tap ends the session like a single tap (no speaker_stop)
    CHECK(duplex_button_action(DUPLEX_GESTURE_DOUBLE_TAP, true) == DUPLEX_BTN_END);
    // D7: long press ends the session, then toggles DND as today
    CHECK(duplex_button_action(DUPLEX_GESTURE_LONG_PRESS, true) == DUPLEX_BTN_END_THEN_AS_TODAY);
    // Triple tap (rotate display) unchanged
    CHECK(duplex_button_action(DUPLEX_GESTURE_TRIPLE_TAP, true) == DUPLEX_BTN_AS_TODAY);
}

// D4: the Atom only ever sends reason "button" or "error"
static void test_end_reason(void) {
    CHECK_STR(duplex_end_reason("button"), "button");
    CHECK_STR(duplex_end_reason("error"), "error");
    CHECK_STR(duplex_end_reason(NULL), "button");
    CHECK_STR(duplex_end_reason(""), "button");
    CHECK_STR(duplex_end_reason("hub_hangup"), "button");
    CHECK_STR(duplex_end_reason("ERROR"), "button");
}

static void test_ms_helpers(void) {
    CHECK(duplex_ms_diff(1000, 400) == 600);
    CHECK(duplex_ms_diff(400, 1000) == -600);
    CHECK(duplex_ms_diff(0x10u, WRAP_NEAR) == 0x110);       // across the wrap
    CHECK(duplex_ms_reached(500, 500));
    CHECK(!duplex_ms_reached(499, 500));
    CHECK(duplex_ms_reached(0x20u, WRAP_NEAR + 0x100u));     // deadline 0, now 0x20
    CHECK(!duplex_ms_reached(WRAP_NEAR, WRAP_NEAR + 0x120u)); // deadline after the wrap
}

static void test_rx_route(void) {
    // Feature off (no duplex session ever): today's path, byte for byte
    CHECK(duplex_rx_route(false, false, false, 0, 0) == DUPLEX_RX_LEGACY);
    CHECK(duplex_rx_route(false, false, false, 123456, 0) == DUPLEX_RX_LEGACY);

    // Duplex session: play queue
    CHECK(duplex_rx_route(true, true, false, 1000, 0) == DUPLEX_RX_QUEUE);
    // A new session wins over a stale late-drop window
    CHECK(duplex_rx_route(true, true, true, 1000, 2000) == DUPLEX_RX_QUEUE);

    // Server-side end: play task still draining -> one writer only, drop
    CHECK(duplex_rx_route(false, true, false, 1000, 0) == DUPLEX_RX_DROP);

    // Device-side end: stray model speech dropped until the window closes
    CHECK(duplex_rx_route(false, false, true, 1000, 2500) == DUPLEX_RX_DROP);
    CHECK(duplex_rx_route(false, false, true, 2499, 2500) == DUPLEX_RX_DROP);
    CHECK(duplex_rx_route(false, false, true, 2500, 2500) == DUPLEX_RX_LEGACY);
    CHECK(duplex_rx_route(false, false, true, 9000, 2500) == DUPLEX_RX_LEGACY);

    // Late-drop window across the ms wrap
    CHECK(duplex_rx_route(false, false, true, WRAP_NEAR, WRAP_NEAR + 1500u) == DUPLEX_RX_DROP);
    CHECK(duplex_rx_route(false, false, true, 0x100u, WRAP_NEAR + 1500u) == DUPLEX_RX_DROP);
    CHECK(duplex_rx_route(false, false, true, WRAP_NEAR + 1500u, WRAP_NEAR + 1500u) == DUPLEX_RX_LEGACY);
}

// The late-drop window is disarmed once it has passed, otherwise the wrap-safe
// comparison would drop normal TTS again 2^31 ms later
static void test_late_drop_disarm(void) {
    CHECK(!duplex_late_drop_expired(false, 9000, 2500));        // not armed
    CHECK(!duplex_late_drop_expired(true, 2499, 2500));         // window still open
    CHECK(duplex_late_drop_expired(true, 2500, 2500));          // window over: disarm
    CHECK(duplex_late_drop_expired(true, WRAP_NEAR + 1500u, WRAP_NEAR + 1500u));
    // Why: left armed, the same frame 2^31 ms later would be dropped again
    CHECK(duplex_rx_route(false, false, true, 2500u + 0x80000000u, 2500) == DUPLEX_RX_DROP);
    // Once disarmed it is today's path for good
    CHECK(duplex_rx_route(false, false, false, 2500u + 0x80000000u, 2500) == DUPLEX_RX_LEGACY);
}

// Barge-in: unmute once the TX ring has played out, from the playback clock
static void test_barge_in_unmute(void) {
    // 100 ms written, 40 ms elapsed -> 60 ms still in the ring
    CHECK(duplex_play_lead_ms(1040, 1000, 1600, 16) == 60);
    // Ring ran dry: negative lead
    CHECK(duplex_play_lead_ms(1150, 1000, 1600, 16) == -50);
    // Clock across the wrap: 32 ms elapsed, 100 ms written
    CHECK(duplex_play_lead_ms(0x10u, 0xFFFFFFF0u, 1600, 16) == 68);

    // Typical paced case: ~60 ms in the ring -> unmute after 60 + 40 ms, not a flat 360 ms
    CHECK(duplex_unmute_at(5000, 60, 320, 40) == 5100);
    // Ring dry (or clock not running): only the margin
    CHECK(duplex_unmute_at(5000, -30, 320, 40) == 5040);
    CHECK(duplex_unmute_at(5000, 0, 320, 40) == 5040);
    // Bounded by the ring size
    CHECK(duplex_unmute_at(5000, 1000, 320, 40) == 5360);
    CHECK(duplex_unmute_at(5000, 320, 320, 40) == 5360);
    // Deadline across the wrap is still reached at the right time
    {
        uint32_t at = duplex_unmute_at(WRAP_NEAR, 300, 320, 40);
        CHECK(!duplex_ms_reached(WRAP_NEAR + 339u, at));
        CHECK(duplex_ms_reached(WRAP_NEAR + 340u, at));
    }
}

// A second clear, or a device end during a barge-in mute, extends the mute, never shortens it
static void test_mute_deadline(void) {
    CHECK(duplex_mute_deadline(false, 0, 5100) == 5100);        // not muted: new deadline
    CHECK(duplex_mute_deadline(false, 9000, 5100) == 5100);     // stale value ignored
    CHECK(duplex_mute_deadline(true, 5100, 5300) == 5300);      // more audio in the ring: extend
    CHECK(duplex_mute_deadline(true, 5300, 5100) == 5300);      // never shorten
    CHECK(duplex_mute_deadline(true, 0x20u, WRAP_NEAR) == 0x20u);  // later deadline across the wrap
}

// Frames are tagged with the clear generation: only pre-clear frames are dropped
static void test_frame_generation(void) {
    CHECK(!duplex_frame_is_stale(7, 7));     // queued after the last clear: play it
    CHECK(duplex_frame_is_stale(6, 7));      // queued before the clear: stale
    CHECK(!duplex_frame_is_stale(0xFFFFFFFFu, 0xFFFFFFFFu));
    CHECK(duplex_frame_is_stale(0xFFFFFFFFu, 0));   // generation counter wrapped
}

static void test_levels_and_vu(void) {
    // Speaker level window [now - 70, now]
    CHECK(duplex_level_in_window(1000, 1000, 70));
    CHECK(duplex_level_in_window(1000, 931, 70));
    CHECK(!duplex_level_in_window(1000, 930, 70));
    CHECK(!duplex_level_in_window(1000, 1001, 70));  // not playing yet
    CHECK(!duplex_level_in_window(500000, 0, 70));   // reset slot
    CHECK(duplex_level_in_window(0x10u, WRAP_NEAR + 0xF0u, 70));  // 32 ms ago, across the wrap

    // vu freshness
    CHECK(duplex_vu_fresh(1000, 1000, 400));
    CHECK(duplex_vu_fresh(1399, 1000, 400));
    CHECK(!duplex_vu_fresh(1400, 1000, 400));
    CHECK(!duplex_vu_fresh(999, 1000, 400));
    CHECK(duplex_vu_fresh(0x50u, WRAP_NEAR, 400));
}

// Liveness: no vu for 8 s ends the session with reason "error"
static void test_watchdog(void) {
    CHECK(!duplex_watchdog_expired(10000, 10000, 8000));   // session just started
    CHECK(!duplex_watchdog_expired(17999, 10000, 8000));
    CHECK(duplex_watchdog_expired(18000, 10000, 8000));
    CHECK(duplex_watchdog_expired(60000, 10000, 8000));
    // vu at ~10 Hz keeps it alive
    CHECK(!duplex_watchdog_expired(10100, 10000, 8000));
    // Across the wrap
    CHECK(!duplex_watchdog_expired(0x1000u, WRAP_NEAR, 8000));
    CHECK(duplex_watchdog_expired(WRAP_NEAR + 8000u, WRAP_NEAR, 8000));
}

int main(void) {
    test_button_gestures();
    test_end_reason();
    test_ms_helpers();
    test_rx_route();
    test_late_drop_disarm();
    test_barge_in_unmute();
    test_mute_deadline();
    test_frame_generation();
    test_levels_and_vu();
    test_watchdog();

    printf("%s: %d checks, %d failed\n", failures ? "FAILED" : "OK", checks, failures);
    return failures ? 1 : 0;
}
