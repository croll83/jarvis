"""Phase 3, item 1: from the wake word (or the tap) straight to the hub session (design 19, 19.12).

The peer asks with live_start{req_id, trigger, preroll}, the orchestrator decides: either the
enriched live_session_start (req_id, origin, identified, offer_extra) or live_declined{req_id,
reason}. Everything is refused before any computation; the identity is forced or comes from the
voice ID on the pre-roll (600 ms cap, short-clip threshold and margin). The room is muted only
at live_session_active; a failed wake/tap attempt leaves no trace (no conversion, no
live_session_end, no tts_done, unmute in background only if it had muted). Also: live_cancel,
voice_command with call_id, the identify endpoint with its own token, the read-only speaker_id
tool, the live_on_wake flag (column, welcome, device_api), the shadow and the voice gate on the
wake pre-roll, announcements handed to the session's peer.

Resemblyzer, Home Assistant and the hub are fakes. Runs like the other tests:
  docker run --rm --network none -v $PWD/jarvis-orchestrator/tests:/app/tests:ro \
    jarvis-orchestrator python -m unittest -v tests.test_live_wake
"""
import asyncio
import base64
import json
import os
import time
import unittest

os.environ.update({
    "AI_BACKEND": "local",
    "SKIP_PRE_ROUTE": "true",
    "AI_AGENT_TOKEN": "test-token",
    "JARVIS_MEMORY_URL": "http://127.0.0.1:1",
    "JARVIS_MEMORY_TOKEN": "test",
    "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:1/0"),
    "MEM0_BASE_URL": os.environ.get("MEM0_BASE_URL", "http://127.0.0.1:1"),
})

from types import SimpleNamespace as NS  # noqa: E402

from fastapi import HTTPException  # noqa: E402

import config  # noqa: E402
import database  # noqa: E402
import device_api  # noqa: E402
import integrations  # noqa: E402
import live_room_mute  # noqa: E402
import live_wake  # noqa: E402
import main  # noqa: E402
import router_model  # noqa: E402
import tools_api  # noqa: E402
import voice_corpus  # noqa: E402
import voice_recognition  # noqa: E402
import ws_audio_handler as wsh  # noqa: E402

database.init_db()
for _name in ("Marco", "Ada"):
    if not database.get_user_by_name(_name):
        database.create_user(_name)
MARCO = database.get_user_by_name("Marco").id
ADA = database.get_user_by_name("Ada").id

DEV = "AB12CD34EF56"
DEV2 = "AB12CD34EF57"
LOC = "albani20"
ECHO = "media_player.echo_studio"


def pcm_b64(seconds=1.5):
    return base64.b64encode(b"\x10\x00" * int(16000 * seconds)).decode()


def preroll(seconds=1.5):
    return {"codec": "pcm_s16le", "rate": 16000, "b64": pcm_b64(seconds)}


class FakeWebSocket:
    def __init__(self, incoming=()):
        self.sent = []
        self.incoming = list(incoming)
        self.client_state = None

    async def send_json(self, obj):
        self.sent.append(obj)

    async def accept(self):
        pass

    async def close(self, *a, **k):
        pass

    async def receive(self):
        if self.incoming:
            return {"type": "websocket.receive", "text": json.dumps(self.incoming.pop(0))}
        # let the live tasks (live_start, wake_preroll) finish before the device goes away
        while wsh._live_tasks:
            await asyncio.sleep(0.01)
        return {"type": "websocket.disconnect"}

    def types(self):
        return [m.get("type") for m in self.sent if m.get("type") != "welcome"]

    def of(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


class _Base(unittest.IsolatedAsyncioTestCase):
    device_type = "AtomS3R"

    async def asyncSetUp(self):
        self.ws = FakeWebSocket()
        wsh._persistent_connections[DEV] = wsh.PersistentDeviceConnection(DEV, self.ws, main._process_ws_audio)
        self.ws2 = FakeWebSocket()
        wsh._persistent_connections[DEV2] = wsh.PersistentDeviceConnection(DEV2, self.ws2, main._process_ws_audio)
        self.cfg = {DEV: self.make_cfg(), DEV2: self.make_cfg(friendly_name="Studio")}
        self.scores = {MARCO: 0.85, ADA: 0.60}
        self.score_delay = 0.0
        self.score_calls = []
        self.mutes, self.unmutes, self.locations, self.ha_calls = [], [], [], []

        def score_sync(pcm):
            self.score_calls.append(len(pcm))
            if self.score_delay:
                time.sleep(self.score_delay)
            return dict(self.scores) if self.scores is not None else None

        async def mute_room(device_id, location_id=None, room=None):
            self.mutes.append(device_id)
            return {"status": "muted"}

        async def unmute_room(device_id, reason="session_end"):
            self.unmutes.append((device_id, reason))
            return {"status": "restored"}

        async def call_hass_service(*a, **k):
            self.ha_calls.append(a)
            return True, None

        self._saved = [(main, n, getattr(main, n)) for n in (
            "get_device_speaker_config", "set_user_location", "speak")]
        self._saved += [(live_wake, "_score_sync", live_wake._score_sync),
                        (live_room_mute, "mute_room", live_room_mute.mute_room),
                        (live_room_mute, "unmute_room", live_room_mute.unmute_room),
                        (router_model, "stanza_valida", router_model.stanza_valida),
                        (integrations, "call_hass_service", integrations.call_hass_service),
                        (voice_recognition.voice_recognizer, "get_all_active_enrollments",
                         voice_recognition.voice_recognizer.get_all_active_enrollments)]
        self._saved += [(config, n, getattr(config, n)) for n in (
            "VOICE_LIVE_HUB_PORT", "VOICE_LIVE_HUB_HOST", "VOICE_LIVE_HUB_DEVICE_TYPES",
            "VOICE_LIVE_HUB_EXCLUDE_DEVICES", "VOICE_LIVE_HUB_MAX_S", "AI_AGENT_SPEAKER_PROFILES",
            "AI_AGENT_DEFAULT_PROFILE", "VOICE_LIVE_ON_WAKE", "VOICE_LIVE_HUB_MAX_SESSIONS",
            "LIVE_WAKE_ID_CAP_MS", "WAKE_VOICE_GATE", "WAKE_PREROLL_SHADOW", "WAKE_VOICE_GATE_THRESHOLD",
            "VOICE_LIVE_IDENTIFY_TOKEN", "VOICE_CORPUS_ENABLED", "LIVE_HUB_BREAKER_S")]

        main.get_device_speaker_config = lambda d: self.cfg.get((d or "").upper())
        main.set_user_location = lambda uid, loc, src: self.locations.append((uid, loc, src))

        async def speak(*a, **k):
            self.ha_calls.append(("speak",) + a)
        main.speak = speak
        live_wake._score_sync = score_sync
        live_room_mute.mute_room = mute_room
        live_room_mute.unmute_room = unmute_room
        router_model.stanza_valida = lambda loc, nome: nome
        integrations.call_hass_service = call_hass_service
        voice_recognition.voice_recognizer.get_all_active_enrollments = lambda: []
        config.VOICE_LIVE_HUB_PORT = 8790
        config.VOICE_LIVE_HUB_HOST = "100.116.99.9"
        config.VOICE_LIVE_HUB_DEVICE_TYPES = {"AtomS3R", "NabuVoice", "AndroidPhone", "AndroidWear"}
        config.VOICE_LIVE_HUB_EXCLUDE_DEVICES = set()
        config.VOICE_LIVE_HUB_MAX_S = 3600.0
        config.AI_AGENT_SPEAKER_PROFILES = {"marco": "hermes-marco", "ada": "hermes-ada"}
        config.AI_AGENT_DEFAULT_PROFILE = "hermes-shared"
        config.VOICE_LIVE_ON_WAKE = True
        config.VOICE_LIVE_HUB_MAX_SESSIONS = 2
        config.LIVE_WAKE_ID_CAP_MS = 600
        config.WAKE_VOICE_GATE = False
        config.WAKE_PREROLL_SHADOW = True
        config.WAKE_VOICE_GATE_THRESHOLD = 0.65
        config.VOICE_LIVE_IDENTIFY_TOKEN = "hub-identify-token"
        config.VOICE_CORPUS_ENABLED = False
        config.LIVE_HUB_BREAKER_S = 60.0
        self.reset_state()

    def reset_state(self):
        live_wake.breaker.reset()
        live_wake._recent_by_home.clear()
        live_wake._fallback_until.clear()
        live_wake._shadow.clear()
        main._live_requests.clear()
        main._early_cancels.clear()
        main._live_sessions.clear()
        main._room_mute_tasks.clear()
        live_room_mute._holds.clear()

    def make_cfg(self, **kw):
        cfg = {"device_type": self.device_type, "location_id": LOC, "friendly_name": "Studio",
               "use_internal_speaker": False, "output_speaker": ECHO, "live_on_wake": True,
               "forced_user_id": None}
        cfg.update(kw)
        return cfg

    async def asyncTearDown(self):
        await self.settle()
        for obj, name, value in self._saved:
            setattr(obj, name, value)
        for d in (DEV, DEV2):
            wsh._persistent_connections.pop(d, None)
            wsh._open_turns.pop(d, None)
        self.reset_state()

    async def settle(self):
        for _ in range(50):
            pending = [t for t in list(main._live_bg_tasks) + list(wsh._live_tasks)
                       + list(main._room_mute_tasks.values()) if not t.done()]
            if not pending:
                return
            await asyncio.sleep(0.01)

    async def live_start(self, dev=DEV, trigger="wake", req_id="r1", **extra):
        msg = {"type": "live_start", "req_id": req_id, "trigger": trigger, **extra}
        if trigger == "wake" and "preroll" not in extra:
            msg["preroll"] = preroll()
        await main.handle_live_start(dev, msg)
        return main._live_sessions.get(dev)

    def declined(self, ws=None):
        ws = ws or self.ws
        return [(m["req_id"], m["reason"]) for m in ws.of("live_declined")]


# ---------------------------------------------------------------------------
class LiveStartIdentity(_Base):
    async def test_tap_with_forced_identity(self):
        self.cfg[DEV] = self.make_cfg(device_type="AndroidPhone", forced_user_id=MARCO,
                                      use_internal_speaker=True, output_speaker=None)
        session = await self.live_start(trigger="tap", req_id="tap-1")
        self.assertEqual(self.score_calls, [], "forced identity: no voice ID")
        self.assertEqual(self.ws.types(), ["live_session_start"])
        msg = self.ws.sent[0]
        self.assertEqual(msg["req_id"], "tap-1")
        self.assertEqual((msg["origin"], msg["identified"]), ("tap", True))
        self.assertEqual((msg["profile"], msg["name"]), ("hermes-marco", "Marco"))
        self.assertEqual(msg["offer_extra"], {"origin": "tap", "identified": True,
                                              "command": False, "quick": True})
        self.assertEqual(msg["backend"], "hub")
        self.assertEqual(msg["call_id"], session.hub_call_id)
        self.assertEqual((session.origin, session.req_id, session.active), ("tap", "tap-1", False))
        self.assertEqual(self.locations, [(MARCO, LOC, "voice")])
        await self.settle()
        self.assertEqual(self.mutes, [], "the room is muted only at live_session_active")

    async def test_voice_id_on_the_preroll_identifies(self):
        session = await self.live_start()
        self.assertEqual(len(self.score_calls), 1)
        self.assertEqual(self.score_calls[0], 16000 * 2 * 1.5)
        self.assertTrue(session.identified)
        self.assertEqual(self.ws.sent[0]["name"], "Marco")
        self.assertEqual(self.ws.sent[0]["offer_extra"]["identified"], True)
        self.assertEqual(self.locations, [(MARCO, LOC, "voice")])

    async def test_postroll_is_used_too(self):
        await self.live_start(postroll=preroll(0.5))
        self.assertEqual(self.score_calls, [16000 * 2 * 2])

    async def test_below_the_short_clip_threshold_is_famiglia(self):
        self.scores = {MARCO: 0.72, ADA: 0.40}  # would pass today's 0.70
        session = await self.live_start()
        msg = self.ws.sent[0]
        self.assertFalse(session.identified)
        self.assertEqual((msg["profile"], msg["name"], msg["identified"]), ("hermes-shared", "Famiglia", False))
        self.assertEqual(self.locations, [], "no location change without a strong match")

    async def test_too_close_to_the_second_candidate_is_famiglia(self):
        self.scores = {MARCO: 0.80, ADA: 0.78}
        session = await self.live_start()
        self.assertFalse(session.identified)
        self.assertEqual(self.ws.sent[0]["name"], "Famiglia")

    async def test_cap_exceeded_becomes_famiglia_and_the_late_result_is_dropped(self):
        config.LIVE_WAKE_ID_CAP_MS = 50
        self.score_delay = 0.3
        t0 = time.monotonic()
        session = await self.live_start()
        self.assertLess(time.monotonic() - t0, 0.25)
        self.assertFalse(session.identified)
        self.assertEqual(self.ws.sent[0]["name"], "Famiglia")
        await asyncio.sleep(0.35)  # the late score arrives and changes nothing
        self.assertFalse(main._live_sessions[DEV].identified)

    async def test_no_preroll_and_no_forced_user_is_famiglia(self):
        session = await self.live_start(trigger="tap")
        self.assertEqual(self.score_calls, [])
        self.assertFalse(session.identified)

    async def test_command_flag_from_the_peer_is_carried(self):
        await self.live_start(command=True)
        self.assertEqual(self.ws.sent[0]["offer_extra"]["command"], True)

    async def test_unreachable_device_leaves_no_session_and_no_request(self):
        wsh._persistent_connections.pop(DEV, None)
        await self.live_start()
        self.assertNotIn(DEV, main._live_sessions)
        self.assertNotIn(DEV, main._live_requests)


class LiveStartDeclined(_Base):
    async def assert_declined(self, reason, req_id="r1"):
        self.assertEqual(self.declined(), [(req_id, reason)])
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.score_calls, [], "refused before any computation")
        self.assertEqual(self.ws.types(), ["live_declined"])

    async def test_device_not_enabled(self):
        self.cfg[DEV]["live_on_wake"] = False
        await self.live_start()
        await self.assert_declined("disabled")

    async def test_global_switch_off(self):
        config.VOICE_LIVE_ON_WAKE = False
        await self.live_start()
        await self.assert_declined("disabled")

    async def test_unconfigured_device(self):
        self.cfg.pop(DEV)
        await self.live_start()
        await self.assert_declined("disabled")

    async def test_malformed_request(self):
        await self.live_start(trigger="shout")
        await self.assert_declined("disabled")

    async def test_not_eligible_for_the_hub(self):
        config.VOICE_LIVE_HUB_DEVICE_TYPES = {"AndroidPhone"}
        await self.live_start()
        await self.assert_declined("hub_off")

    async def test_hub_turned_off(self):
        config.VOICE_LIVE_HUB_PORT = 0
        await self.live_start()
        await self.assert_declined("hub_off")

    async def test_busy_with_a_session(self):
        await self.live_start(req_id="first")
        self.ws.sent.clear()
        self.score_calls.clear()
        await self.live_start(req_id="second")
        self.assertEqual(self.declined(), [("second", "busy")])

    async def test_hub_full(self):
        config.VOICE_LIVE_HUB_MAX_SESSIONS = 1
        self.cfg[DEV2]["location_id"] = "wagmi"
        await self.live_start(dev=DEV2, req_id="other")
        self.assertIn(DEV2, main._live_sessions)
        self.score_calls.clear()
        await self.live_start()
        await self.assert_declined("busy")

    async def test_enrollment_in_progress(self):
        voice_recognition.voice_recognizer.get_all_active_enrollments = lambda: [MARCO]
        await self.live_start()
        await self.assert_declined("enrollment")

    async def test_circuit_breaker_open(self):
        live_wake.breaker.record_failure("hub_unreachable")
        await self.live_start()
        await self.assert_declined("hub_down")

    async def test_breaker_lets_one_trial_through_after_the_window(self):
        config.LIVE_HUB_BREAKER_S = 0.05
        live_wake.breaker.record_failure("hub_rejected", "provider_unavailable")
        await asyncio.sleep(0.06)
        session = await self.live_start(req_id="trial")
        self.assertIsNotNone(session)
        self.cfg[DEV2]["location_id"] = "wagmi"
        await self.live_start(dev=DEV2, req_id="second")
        self.assertEqual(self.declined(self.ws2), [("second", "hub_down")])
        # the trial session is accepted: the breaker closes
        await main.handle_live_session_active(DEV, session.hub_call_id)
        self.assertTrue(live_wake.breaker.allow())

    async def test_breaker_ignores_device_errors_and_too_many_calls(self):
        self.assertFalse(live_wake.breaker.record_failure("error"))
        self.assertFalse(live_wake.breaker.record_failure("hub_rejected", "too many calls"))
        self.assertFalse(live_wake.breaker.record_failure("hub_rejected", "timeout"))
        self.assertTrue(live_wake.breaker.allow())

    async def test_duplicate_in_the_same_home(self):
        await self.live_start(dev=DEV2, req_id="first")
        self.assertIn(DEV2, main._live_sessions)
        self.score_calls.clear()
        await self.live_start(req_id="dup")
        await self.assert_declined("duplicate", "dup")

    async def test_another_home_is_not_a_duplicate(self):
        self.cfg[DEV2]["location_id"] = "wagmi"
        await self.live_start(dev=DEV2, req_id="first")
        await self.live_start(req_id="mine")
        self.assertIn(DEV, main._live_sessions)

    async def test_a_tap_is_never_a_duplicate(self):
        # A tap is a deliberate gesture on one device: "duplicate" would drop it in silence
        await self.live_start(dev=DEV2, req_id="first")
        self.assertIn(DEV2, main._live_sessions)
        await self.live_start(trigger="tap", req_id="tap")
        self.assertIn(DEV, main._live_sessions)
        self.assertEqual(self.declined(), [])

    async def test_a_tap_does_not_claim_the_home(self):
        await self.live_start(dev=DEV2, trigger="tap", req_id="tap")
        self.assertIn(DEV2, main._live_sessions)
        await self.live_start(req_id="wake")
        self.assertIn(DEV, main._live_sessions)
        self.assertEqual(self.declined(), [])

    async def test_duplicate_window_expires(self):
        await self.live_start(dev=DEV2, req_id="first")
        live_wake._recent_by_home[LOC] = (DEV2, time.monotonic() - config.LIVE_WAKE_DEDUPE_S - 0.1)
        config.VOICE_LIVE_HUB_MAX_SESSIONS = 3
        await self.live_start(req_id="later")
        self.assertIn(DEV, main._live_sessions)


class VoiceGate(_Base):
    async def test_gate_on_drops_a_wake_that_matches_nobody(self):
        config.WAKE_VOICE_GATE = True
        self.scores = {MARCO: 0.50, ADA: 0.40}
        await self.live_start()
        self.assertEqual(self.declined(), [("r1", "voice_gate")])
        self.assertNotIn(DEV, main._live_sessions)

    async def test_gate_on_keeps_a_family_voice_even_if_not_identified(self):
        config.WAKE_VOICE_GATE = True
        self.scores = {MARCO: 0.70, ADA: 0.68}  # above the gate, not identified
        session = await self.live_start()
        self.assertIsNotNone(session)
        self.assertFalse(session.identified)

    async def test_gate_never_applies_to_a_tap(self):
        config.WAKE_VOICE_GATE = True
        self.scores = {MARCO: 0.10}
        session = await self.live_start(trigger="tap", preroll=preroll())
        self.assertIsNotNone(session)

    async def test_gate_off_by_default_does_not_drop(self):
        self.scores = {MARCO: 0.10}
        self.assertIsNotNone(await self.live_start())

    async def test_gate_fails_open_on_timeout(self):
        config.WAKE_VOICE_GATE = True
        config.LIVE_WAKE_ID_CAP_MS = 50
        self.score_delay = 0.2
        self.assertIsNotNone(await self.live_start())


class LiveCancel(_Base):
    async def test_cancel_during_the_voice_id_sends_nothing(self):
        self.score_delay = 0.15
        task = asyncio.create_task(self.live_start(req_id="c1"))
        await asyncio.sleep(0.03)
        self.assertIn(DEV, main._live_requests)
        await main.handle_live_cancel(DEV, "c1")
        await task
        self.assertEqual(self.ws.sent, [])
        self.assertNotIn(DEV, main._live_sessions)
        self.assertNotIn(DEV, main._live_requests)

    async def test_cancel_after_the_answer_removes_the_session_silently(self):
        await self.live_start(req_id="c2")
        self.ws.sent.clear()
        await main.handle_live_cancel(DEV, "c2")
        await self.settle()
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.ws.sent, [])
        self.assertEqual((self.mutes, self.unmutes), ([], []), "never muted, nothing to restore")

    async def test_cancel_after_active_restores_the_room_in_background(self):
        session = await self.live_start(req_id="c3")
        await main.handle_live_session_active(DEV, session.hub_call_id)
        await self.settle()
        self.ws.sent.clear()
        await main.handle_live_cancel(DEV, "c3")
        await self.settle()
        self.assertEqual(self.ws.sent, [])
        self.assertEqual([d for d, _ in self.unmutes], [DEV])

    async def test_cancel_before_the_start_task_runs(self):
        # The WS loop spawns live_start in a task: a live_cancel handled before the task runs
        # must still win, or a session the peer ignores keeps the device and a hub slot busy
        await main.handle_live_cancel(DEV, "c5")
        await self.live_start(req_id="c5")
        self.assertEqual(self.ws.sent, [])
        self.assertNotIn(DEV, main._live_sessions)
        self.assertNotIn(DEV, main._live_requests)
        await self.live_start(req_id="c6")
        self.assertIn(DEV, main._live_sessions, "only that req_id is discarded")

    async def test_stale_req_id_is_ignored(self):
        session = await self.live_start(req_id="c4")
        await main.handle_live_cancel(DEV, "old")
        self.assertIs(main._live_sessions.get(DEV), session)


class ActiveAndFallback(_Base):
    async def test_room_is_muted_only_after_live_session_active(self):
        session = await self.live_start()
        await self.settle()
        self.assertEqual(self.mutes, [])
        await main.handle_live_session_active(DEV, "deadbeef")  # stale call_id
        await self.settle()
        self.assertEqual(self.mutes, [])
        await main.handle_live_session_active(DEV, session.hub_call_id)
        await self.settle()
        self.assertEqual(self.mutes, [DEV])
        self.assertTrue(session.active and session.muted)
        await main.handle_live_session_active(DEV, session.hub_call_id)  # once only
        await self.settle()
        self.assertEqual(self.mutes, [DEV])

    async def test_phrase_origin_still_mutes_at_the_start(self):
        await main.start_live_session(device_id=DEV, speaker_id=MARCO, speaker_name="Marco",
                                      location_id=LOC, room="Studio", media_player_id=ECHO)
        await self.settle()
        self.assertEqual(self.mutes, [DEV])
        self.assertNotIn("req_id", self.ws.sent[0])

    async def test_hub_unreachable_on_a_wake_session_leaves_no_trace(self):
        session = await self.live_start()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, session.hub_call_id, "hub_unreachable")
        await self.settle()
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.ws.sent, [], "no conversion, no live_session_end, no tts_done")
        self.assertEqual((self.mutes, self.unmutes), ([], []))
        self.assertTrue(live_wake.breaker.is_open())

    async def test_no_live_retry_for_that_device_after_a_fallback(self):
        session = await self.live_start()
        await main.handle_device_live_session_end(DEV, session.hub_call_id, "error")
        self.assertFalse(live_wake.breaker.is_open(), "a device error does not trip the breaker")
        self.assertEqual(main._live_session_backend(DEV), "orchestrator")
        self.ws.sent.clear()
        await self.live_start(req_id="again")
        self.assertEqual(self.declined(), [("again", "hub_down")])

    async def test_rejected_after_it_had_muted_unmutes_in_background(self):
        session = await self.live_start()
        await main.handle_live_session_active(DEV, session.hub_call_id)
        await self.settle()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, session.hub_call_id, "hub_rejected",
                                                  detail="provider_unavailable")
        await self.settle()
        self.assertEqual(self.ws.sent, [])
        self.assertEqual([d for d, _ in self.unmutes], [DEV])
        self.assertTrue(live_wake.breaker.is_open())

    async def test_too_many_calls_does_not_trip_the_breaker(self):
        session = await self.live_start()
        await main.handle_device_live_session_end(DEV, session.hub_call_id, "hub_rejected",
                                                  detail="too many calls")
        self.assertFalse(live_wake.breaker.is_open())

    async def test_button_ends_a_wake_session_as_today(self):
        session = await self.live_start()
        await main.handle_live_session_active(DEV, session.hub_call_id)
        await self.settle()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, session.hub_call_id, "button")
        self.assertEqual(self.ws.types(), ["live_session_end", "tts_done"])
        self.assertEqual([d for d, _ in self.unmutes], [DEV])

    async def test_turn_during_a_wake_session_is_silent(self):
        await self.live_start()
        self.ws.sent.clear()
        await main.end_hub_session_for_turn(DEV)
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.ws.sent, [])

    async def test_phrase_session_still_finishes_with_today_live_session_end(self):
        await main.start_live_session(device_id=DEV, speaker_id=MARCO, speaker_name="Marco",
                                      location_id=LOC, room="Studio", media_player_id=ECHO)
        self.ws.sent.clear()
        await main.end_hub_session_for_turn(DEV)
        self.assertEqual(self.ws.types(), ["live_session_end"])


class NeverAccepted(_Base):
    async def run_monitor_once(self):
        calls = []
        real_sleep = asyncio.sleep

        async def one_tick(_s):
            calls.append(_s)
            if len(calls) > 1:
                raise asyncio.CancelledError
            await real_sleep(0)
        asyncio.sleep = one_tick
        try:
            with self.assertRaises(asyncio.CancelledError):
                await main.live_session_monitor()
        finally:
            asyncio.sleep = real_sleep

    async def test_a_wake_session_never_accepted_is_dropped_silently(self):
        session = await self.live_start()
        session.started_at = time.time() - config.LIVE_ACTIVE_TIMEOUT_S - 1
        self.ws.sent.clear()
        await self.run_monitor_once()
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.ws.sent, [])

    async def test_an_accepted_wake_session_stays(self):
        session = await self.live_start()
        await main.handle_live_session_active(DEV, session.hub_call_id)
        session.started_at = time.time() - config.LIVE_ACTIVE_TIMEOUT_S - 1
        await self.run_monitor_once()
        self.assertIs(main._live_sessions.get(DEV), session)


class ThroughTheWebSocket(_Base):
    async def drive(self, *messages):
        ws = FakeWebSocket(incoming=messages)
        wsh._persistent_connections.pop(DEV, None)
        await wsh.ws_audio_endpoint(ws, DEV, config.DEVICE_API_TOKEN, main._process_ws_audio)
        return ws

    async def test_welcome_carries_the_flag(self):
        ws = await self.drive()
        self.assertEqual(ws.sent[0]["type"], "welcome")
        self.assertIs(ws.sent[0]["live_on_wake"], True)
        self.assertIs(ws.sent[0]["wake_voice_gate"], False)
        config.WAKE_VOICE_GATE = True
        ws = await self.drive()
        self.assertIs(ws.sent[0]["wake_voice_gate"], True)

    async def test_welcome_flag_off_by_default(self):
        config.VOICE_LIVE_ON_WAKE = False
        ws = await self.drive()
        self.assertIs(ws.sent[0]["live_on_wake"], False)
        config.VOICE_LIVE_ON_WAKE = True
        self.cfg[DEV]["live_on_wake"] = False
        ws = await self.drive()
        self.assertIs(ws.sent[0]["live_on_wake"], False)

    async def test_live_start_and_active_through_the_ws(self):
        ws = await self.drive({"type": "live_start", "req_id": "w1", "trigger": "wake",
                               "preroll": preroll()})
        self.assertEqual(ws.types(), ["live_session_start"])
        self.assertEqual(ws.sent[1]["req_id"], "w1")
        call_id = ws.sent[1]["call_id"]
        await self.drive({"type": "live_session_active", "call_id": call_id})
        await self.settle()
        self.assertEqual(self.mutes, [DEV])

    async def test_audio_start_right_after_a_failed_handshake(self):
        """The peer reports the failure and opens the classic turn: no message but ready."""
        session = await self.live_start()

        class _Session:  # the real one needs the Silero VAD model, not installed in tests
            session_id = "s1"
            _closed = False
        saved = wsh.PersistentDeviceConnection.start_audio_session
        wsh.PersistentDeviceConnection.start_audio_session = lambda self, codec="opus": _Session()
        try:
            ws = await self.drive({"type": "live_session_end", "call_id": session.hub_call_id,
                                   "reason": "hub_rejected", "detail": "provider_unavailable"},
                                  {"type": "audio_start"})
        finally:
            wsh.PersistentDeviceConnection.start_audio_session = saved
        self.assertEqual(ws.types(), ["ready"])
        self.assertNotIn(DEV, main._live_sessions)
        self.assertTrue(live_wake.breaker.is_open())

    async def test_live_cancel_through_the_ws(self):
        session = await self.live_start(req_id="w2")
        ws = await self.drive({"type": "live_cancel", "req_id": "w2"})
        self.assertEqual(ws.types(), [])
        self.assertNotIn(DEV, main._live_sessions)
        self.assertIsNotNone(session)


# ---------------------------------------------------------------------------
class VoiceCommandWithCallId(_Base):
    def fake_logic(self):
        seen = []

        async def logic(text, context):
            seen.append(dict(context))
            context["_capture"]["responses"].append({"text": "Fatto.", "sound": None})
        saved = main.process_jarvis_logic
        main.process_jarvis_logic = logic
        self.addCleanup(setattr, main, "process_jarvis_logic", saved)
        return seen

    async def test_unknown_speaker_runs_in_the_room_of_the_session(self):
        self.scores = {MARCO: 0.5, ADA: 0.4}
        session = await self.live_start()
        seen = self.fake_logic()
        out = await main.tool_voice_command(main.VoiceCommandRequest(
            text="accendi la luce", speaker="Famiglia", call_id=session.hub_call_id), None)
        self.assertTrue(out["handled"], out)
        ctx = seen[0]
        self.assertEqual((ctx["room"], ctx["location"]), ("Studio", LOC))
        self.assertEqual((ctx["speaker_id"], ctx["speaker_identified"]), (None, False))
        self.assertEqual(ctx["source"], "voice_live")  # same channel levels as today

    async def test_identified_session_uses_its_speaker(self):
        session = await self.live_start()
        seen = self.fake_logic()
        await main.tool_voice_command(main.VoiceCommandRequest(
            text="accendi la luce", speaker="Ada", call_id=session.hub_call_id), None)
        self.assertEqual((seen[0]["speaker_id"], seen[0]["speaker_name"]), (MARCO, "Marco"))

    async def test_unknown_call_id_is_not_handled(self):
        seen = self.fake_logic()
        out = await main.tool_voice_command(main.VoiceCommandRequest(
            text="accendi la luce", call_id="nope"), None)
        self.assertFalse(out["handled"])
        self.assertEqual(seen, [])

    async def test_without_call_id_an_unknown_speaker_is_still_refused(self):
        out = await main.tool_voice_command(main.VoiceCommandRequest(text="accendi"), None)
        self.assertFalse(out["handled"])


class IdentifyEndpoint(_Base):
    async def anon_session(self):
        self.scores = {MARCO: 0.5, ADA: 0.4}
        session = await self.live_start()
        self.assertFalse(session.identified)
        return session

    async def identify(self, call_id, claimed=None, seconds=2.0):
        req = main.LiveIdentifyRequest(audio_b64=pcm_b64(seconds), claimed_name=claimed)
        return await main.live_identify(call_id, req, None)

    async def test_token(self):
        with self.assertRaises(HTTPException) as e:
            await main.verify_live_identify_token("Bearer wrong")
        self.assertEqual(e.exception.status_code, 403)
        with self.assertRaises(HTTPException) as e:
            await main.verify_live_identify_token(f"Bearer {config.AI_AGENT_TOKEN}")
        self.assertEqual(e.exception.status_code, 403, "the AI Agent token is not accepted")
        with self.assertRaises(HTTPException) as e:
            await main.verify_live_identify_token(None)
        self.assertEqual(e.exception.status_code, 401)
        self.assertIsNone(await main.verify_live_identify_token("Bearer hub-identify-token"))
        config.VOICE_LIVE_IDENTIFY_TOKEN = ""
        with self.assertRaises(HTTPException) as e:
            await main.verify_live_identify_token("Bearer hub-identify-token")
        self.assertEqual(e.exception.status_code, 503)
        config.VOICE_LIVE_IDENTIFY_TOKEN = config.AI_AGENT_TOKEN
        with self.assertRaises(HTTPException) as e:
            await main.verify_live_identify_token(f"Bearer {config.AI_AGENT_TOKEN}")
        self.assertEqual(e.exception.status_code, 503)

    async def test_non_ascii_token_is_refused_not_a_crash(self):
        with self.assertRaises(HTTPException) as e:
            await main.verify_live_identify_token("Bearer h\u00e9")
        self.assertEqual(e.exception.status_code, 403)

    async def test_concurrent_attempts_never_switch_person(self):
        session = await self.anon_session()
        gate = asyncio.Event()
        real = live_wake.score_clip

        async def slow_score(pcm, cap_s):
            await gate.wait()
            return await real(pcm, cap_s)
        live_wake.score_clip = slow_score
        self.addCleanup(setattr, live_wake, "score_clip", real)
        self.scores = {MARCO: 0.9, ADA: 0.5}
        first = asyncio.ensure_future(self.identify(session.hub_call_id))
        second = asyncio.ensure_future(self.identify(session.hub_call_id))
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        ok = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, HTTPException)]
        self.assertEqual(len(ok), 1)
        self.assertEqual([r.status_code for r in refused], [409])
        self.assertEqual((session.user_id, session.identified), (MARCO, True))

    async def test_unknown_call_id(self):
        with self.assertRaises(HTTPException) as e:
            await self.identify("nope")
        self.assertEqual(e.exception.status_code, 404)

    async def test_voice_confirmed_switches_the_session(self):
        session = await self.anon_session()
        self.locations.clear()
        self.scores = {MARCO: 0.86, ADA: 0.55}
        out = await self.identify(session.hub_call_id)
        self.assertEqual((out["identified"], out["user_id"], out["name"], out["profile"]),
                         (True, MARCO, "Marco", "hermes-marco"))
        self.assertEqual(out["attempts_left"], 1)
        self.assertEqual((session.user_id, session.speaker_name, session.identified), (MARCO, "Marco", True))
        self.assertEqual(self.locations, [(MARCO, LOC, "voice")])

    async def test_claimed_name_not_confirmed_by_the_voice(self):
        session = await self.anon_session()
        self.scores = {MARCO: 0.86, ADA: 0.55}
        out = await self.identify(session.hub_call_id, claimed="Ada")
        self.assertFalse(out["identified"])
        self.assertEqual((out["name"], out["profile"]), ("Famiglia", "hermes-shared"))
        self.assertFalse(session.identified)

    async def test_claimed_name_does_not_lower_the_threshold(self):
        session = await self.anon_session()
        self.scores = {MARCO: 0.72, ADA: 0.40}
        out = await self.identify(session.hub_call_id, claimed="Marco")
        self.assertFalse(out["identified"])

    async def test_third_attempt_is_refused(self):
        session = await self.anon_session()
        await self.identify(session.hub_call_id)
        await self.identify(session.hub_call_id)
        with self.assertRaises(HTTPException) as e:
            await self.identify(session.hub_call_id)
        self.assertEqual(e.exception.status_code, 429)

    async def test_an_identified_session_cannot_change_person(self):
        session = await self.live_start()
        self.assertTrue(session.identified)
        with self.assertRaises(HTTPException) as e:
            await self.identify(session.hub_call_id)
        self.assertEqual(e.exception.status_code, 409)

    async def test_voice_command_follows_the_new_identity(self):
        session = await self.anon_session()
        self.scores = {MARCO: 0.9, ADA: 0.5}
        await self.identify(session.hub_call_id)
        self.assertEqual(main._live_session_speaker_ctx(session)["speaker_name"], "Marco")


class SpeakerIdTool(_Base):
    async def test_pcm_int16_through_identify_speaker_read_only(self):
        calls = []

        def identify_speaker(audio, rate=16000):
            calls.append((len(audio), rate, type(audio)))
            return voice_recognition.SpeakerMatch(MARCO, "Marco", 0.83, True)
        rec = voice_recognition.voice_recognizer
        saved = rec.identify_speaker
        rec.identify_speaker = identify_speaker
        try:
            session = await self.live_start(trigger="tap")  # anonymous session
            res = await tools_api.tool_speaker_id(tools_api.SpeakerIdRequest(audio_base64=pcm_b64(2.0)), None)
        finally:
            rec.identify_speaker = saved
        self.assertEqual(calls, [(64000, 16000, bytes)])
        self.assertEqual((res.identified, res.user_id, res.user_name), (True, str(MARCO), "Marco"))
        self.assertFalse(session.identified, "speaker_id never touches a session")

    async def test_other_sample_rates_are_refused(self):
        res = await tools_api.tool_speaker_id(
            tools_api.SpeakerIdRequest(audio_base64=pcm_b64(1.0), sample_rate=8000), None)
        self.assertFalse(res.identified)


class WakePreroll(_Base):
    async def test_shadow_answers_keep_at_once_and_logs_the_scores(self):
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "req_id": "p1",
                                             "preroll": preroll(), "score": 0.91})
        gate = self.ws.of("wake_gate")
        self.assertEqual(gate, [{"type": "wake_gate", "req_id": "p1", "action": "keep",
                                 "reason": "gate_off", "best": 0.0}])
        self.assertEqual(len(self.score_calls), 1)
        cmp = live_wake.compare_with_phrase(DEV, {"speaker_id": MARCO, "speaker_identified": True,
                                                  "speaker_confidence": 0.8})
        self.assertTrue(cmp["agree"])
        self.assertEqual(cmp["preroll_decision"], MARCO)
        self.assertEqual(cmp["score"], 0.91)

    async def test_shadow_only_message_gets_no_answer(self):
        """wakeword-server shadow: wake_preroll{gate:false}, nobody waits for a verdict."""
        config.WAKE_VOICE_GATE = True
        self.scores = {MARCO: 0.1}
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "req_id": "s1", "gate": False,
                                             "codec": "pcm_s16le", "rate": 16000, "b64": pcm_b64()})
        self.assertEqual(self.ws.of("wake_gate"), [])
        self.assertEqual(len(self.score_calls), 1, "the scores are still logged")

    async def test_flat_b64_is_accepted(self):
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "b64": pcm_b64()})
        self.assertEqual(len(self.score_calls), 1)

    async def test_shadow_off_and_gate_off_computes_nothing(self):
        config.WAKE_PREROLL_SHADOW = False
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "preroll": preroll()})
        self.assertEqual(self.score_calls, [])
        self.assertEqual(self.ws.of("wake_gate")[0]["action"], "keep")

    async def test_gate_drops_a_voice_that_matches_nobody(self):
        config.WAKE_VOICE_GATE = True
        self.scores = {MARCO: 0.4, ADA: 0.3}
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "req_id": "p2", "preroll": preroll()})
        self.assertEqual(self.ws.of("wake_gate")[0], {"type": "wake_gate", "req_id": "p2",
                                                       "action": "drop", "reason": "no_match", "best": 0.4})

    async def test_gate_keeps_a_family_voice(self):
        config.WAKE_VOICE_GATE = True
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "preroll": preroll()})
        self.assertEqual(self.ws.of("wake_gate")[0]["action"], "keep")

    async def test_gate_fails_open(self):
        config.WAKE_VOICE_GATE = True
        self.scores = None  # no enrolled speakers
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "preroll": preroll()})
        self.assertEqual(self.ws.of("wake_gate")[0]["reason"], "no_models")
        self.assertEqual(self.ws.of("wake_gate")[0]["action"], "keep")
        self.ws.sent.clear()
        await main.handle_wake_preroll(DEV, {"type": "wake_preroll"})  # no audio
        self.assertEqual(self.ws.of("wake_gate")[0]["action"], "keep")

    async def test_audio_is_never_stored_without_the_corpus(self):
        writes = []
        saved = voice_corpus._write
        voice_corpus._write = lambda rec, audio: writes.append(rec)
        try:
            await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "preroll": preroll()})
            await self.live_start()
            await asyncio.sleep(0.05)
            self.assertEqual(writes, [])
            config.VOICE_CORPUS_ENABLED = True
            await main.handle_wake_preroll(DEV, {"type": "wake_preroll", "preroll": preroll()})
            for _ in range(20):
                if writes:
                    break
                await asyncio.sleep(0.02)
        finally:
            voice_corpus._write = saved
        self.assertEqual([w["kind"] for w in writes], ["wake_preroll"])


# ---------------------------------------------------------------------------
class Announcements(_Base):
    async def active_session(self):
        session = await self.live_start()
        await main.handle_live_session_active(DEV, session.hub_call_id)
        await self.settle()
        self.ws.sent.clear()
        return session

    def notify(self, device_id, devices):
        saved = [(database, n, getattr(database, n)) for n in (
            "get_voice_device", "get_all_voice_devices", "get_global_preference", "log_event")]
        database.get_voice_device = lambda d: next((x for x in devices if x.device_id == d), None)
        database.get_all_voice_devices = lambda: list(devices)
        database.get_global_preference = lambda _k, default=None: default
        database.log_event = lambda *a, **k: None
        for obj, name, value in saved:
            self.addCleanup(setattr, obj, name, value)
        req = tools_api.NotifyDeviceRequest(text="Il timer e' scaduto", device_id=device_id, urgent=True)
        return tools_api.tool_notify_device(req, None)

    def dev(self, device_id, name="Studio", internal=True):
        return NS(device_id=device_id, friendly_name=name, location_id=LOC, use_internal_speaker=internal,
                  output_speaker=None if internal else ECHO, enabled=True, device_type="AtomS3R")

    async def test_notify_device_in_session_goes_to_gemini(self):
        session = await self.active_session()
        res = await self.notify(DEV, [self.dev(DEV)])
        self.assertTrue(res.delivered)
        self.assertEqual(res.audio_path, "live_session")
        self.assertEqual(self.ws.sent, [{"type": "announce", "call_id": session.hub_call_id,
                                         "text": "Il timer e' scaduto", "urgent": True}])

    async def test_notify_another_device_of_the_same_room_goes_to_the_session(self):
        session = await self.active_session()
        res = await self.notify(DEV2, [self.dev(DEV), self.dev(DEV2)])
        self.assertTrue(res.delivered)
        self.assertEqual(self.ws.of("announce")[0]["call_id"], session.hub_call_id)
        self.assertEqual(self.ws2.sent, [])

    async def test_session_not_yet_active_keeps_today_refusal(self):
        await self.live_start()
        self.ws.sent.clear()
        res = await self.notify(DEV, [self.dev(DEV)])
        self.assertFalse(res.delivered)
        self.assertIn("sessione live", res.reason)
        self.assertEqual(self.ws.sent, [])

    async def test_speak_on_a_speaker_muted_by_the_session_is_announced(self):
        session = await self.active_session()
        live_room_mute._holds[(LOC, ECHO)] = {"location_id": LOC, "entity_id": ECHO, "method": "mute",
                                               "prior_volume": 0.4, "holders": [DEV], "since": time.time()}
        ok, detail = await integrations.speak("Allarme in cucina", ECHO, LOC, urgent=True)
        self.assertEqual((ok, detail), (True, "live_session"))
        self.assertEqual(self.ha_calls, [])
        self.assertEqual(self.ws.of("announce"), [{"type": "announce", "call_id": session.hub_call_id,
                                                   "text": "Allarme in cucina", "urgent": True}])

    def hold_echo(self):
        live_room_mute._holds[(LOC, ECHO)] = {"location_id": LOC, "entity_id": ECHO, "method": "mute",
                                               "prior_volume": 0.4, "holders": [DEV], "since": time.time()}

    async def test_a_classic_answer_on_the_muted_speaker_is_not_injected(self):
        # Answers of classic turns (another device of the room, streamed chunks) are not
        # announcements: they must not enter the session of whoever opened it (D6)
        await self.active_session()
        self.hold_echo()
        ok, _ = await integrations.speak("La risposta per Ada", ECHO, LOC)
        self.assertTrue(ok)
        self.assertEqual(len(self.ha_calls), 1)
        self.assertEqual(self.ws.of("announce"), [])

    async def test_an_announcement_with_sound_is_announced(self):
        session = await self.active_session()
        self.hold_echo()
        ok, detail = await integrations.speak_with_sound("Annuncio", ECHO, "positive", LOC, announce=True)
        self.assertEqual((ok, detail), (True, "live_session"))
        self.assertEqual(self.ha_calls, [])
        self.assertEqual(self.ws.of("announce"), [{"type": "announce", "call_id": session.hub_call_id,
                                                   "text": "Annuncio", "urgent": False}])

    async def test_speak_in_another_room_is_unchanged(self):
        await self.active_session()
        ok, _ = await integrations.speak("Ciao", "media_player.echo_cucina", LOC)
        self.assertTrue(ok)
        self.assertEqual(len(self.ha_calls), 1)
        self.assertEqual(self.ws.of("announce"), [])


class LiveOnWakeColumn(unittest.TestCase):
    def test_column_defaults_off_and_is_exposed(self):
        dev_id = "AA00BB11CC22"
        database.upsert_voice_device(dev_id, friendly_name="Prova", location_id=LOC, output_speaker=ECHO)
        try:
            self.assertFalse(database.get_voice_device(dev_id).live_on_wake)
            self.assertFalse(device_api.get_device_speaker_config(dev_id)["live_on_wake"])
            self.assertTrue(database.set_device_live_on_wake(dev_id, True))
            self.assertTrue(database.get_voice_device(dev_id).live_on_wake)
            self.assertTrue(database.get_voice_device(dev_id).to_dict()["live_on_wake"])
            self.assertTrue(device_api.get_device_speaker_config(dev_id)["live_on_wake"])
        finally:
            database.delete_voice_device(dev_id)


if __name__ == "__main__":
    unittest.main()
