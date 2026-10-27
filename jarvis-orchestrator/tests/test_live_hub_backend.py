"""Live session on the Voice Live Hub (Phase 2): the orchestrator only opens and closes it.

A device with a built-in speaker of an enabled type gets the enriched live_session_start
(backend "hub", call_id, profile, name, hub address) and nothing else: no greeting, no
trigger_listen, no turn loop. External-speaker devices and VOICE_LIVE_HUB_PORT=0 keep
today's session. The device ends a hub session with an inbound live_session_end; every end
also closes the turn with tts_done, so the peers go back to IDLE. When the hub refused or
was unreachable the session is converted in place to today's Hermes live session (no
live_session_end). An audio turn that reaches the orchestrator during a hub session means
the device left it: the session ends silently and the turn goes through the normal pipeline.
Announcements on the internal speaker are refused while a hub session is running.
The device is a fake WebSocket that records what reaches it; TTS is a fake.

Runs in the orchestrator image without network, like the other tests:
  docker run --rm --network none -v $PWD/jarvis-orchestrator/tests:/app/tests:ro \
    jarvis-orchestrator python -m unittest -v tests.test_live_hub_backend
"""
import asyncio
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

import config  # noqa: E402
import database  # noqa: E402
import internal_tts  # noqa: E402
import main  # noqa: E402
import tools_api  # noqa: E402
import ws_audio_handler as wsh  # noqa: E402
from integrations import STT_NO_SPEECH, STT_OK, SttResult  # noqa: E402

# Empty tables, as on first start: the normal pipeline reads the global preferences.
database.init_db()

DEV = "AB12CD34EF56"  # a valid MAC: notify_device normalises it
HUB_PORT = 8790


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
        return {"type": "websocket.disconnect"}

    def types(self):
        return [m.get("type") for m in self.sent if m.get("type") != "welcome"]


class _Base(unittest.IsolatedAsyncioTestCase):
    device_type = "AtomS3R"
    hub_port = HUB_PORT

    async def asyncSetUp(self):
        self.ws = FakeWebSocket()
        self.conn = wsh.PersistentDeviceConnection(DEV, self.ws, main._process_ws_audio)
        wsh._persistent_connections[DEV] = self.conn
        self.spoken = []
        self._saved = [(main, n, getattr(main, n)) for n in ("get_device_speaker_config", "speak")]
        self._saved += [(internal_tts, "speak_to_device", internal_tts.speak_to_device)]
        self._saved += [(config, n, getattr(config, n)) for n in (
            "VOICE_LIVE_HUB_PORT", "VOICE_LIVE_HUB_HOST", "VOICE_LIVE_HUB_DEVICE_TYPES",
            "VOICE_LIVE_HUB_MAX_S", "AI_AGENT_SPEAKER_PROFILES", "AI_AGENT_DEFAULT_PROFILE")]

        async def speak_to_device(text, device_id):
            self.spoken.append(text)
            return True, 0.1

        async def speak(text, *a, **k):
            self.spoken.append(text)

        main.get_device_speaker_config = lambda _d: {
            "device_type": self.device_type, "location_id": "albani20", "friendly_name": "Studio"}
        main.speak = speak
        internal_tts.speak_to_device = speak_to_device
        config.VOICE_LIVE_HUB_PORT = self.hub_port
        config.VOICE_LIVE_HUB_HOST = "100.116.99.9"
        config.VOICE_LIVE_HUB_DEVICE_TYPES = {"AtomS3R", "AndroidPhone", "AndroidWear"}
        config.VOICE_LIVE_HUB_MAX_S = 3600.0
        config.AI_AGENT_SPEAKER_PROFILES = {"marco": "hermes-marco", "ada": "hermes-ada"}
        config.AI_AGENT_DEFAULT_PROFILE = "hermes-shared"

    async def asyncTearDown(self):
        for obj, name, value in self._saved:
            setattr(obj, name, value)
        for t in list(main._pending_tts_tasks.values()):
            t.cancel()
        main._pending_tts_tasks.pop(DEV, None)
        wsh._persistent_connections.pop(DEV, None)
        wsh._open_turns.pop(DEV, None)
        main._live_sessions.pop(DEV, None)

    async def start(self, internal=True, speaker_id="u1", name="Marco", **kw):
        return await main.start_live_session(
            device_id=DEV, speaker_id=speaker_id, speaker_name=name, location_id="albani20",
            room="Studio", media_player_id=None if internal else "media_player.echo_studio",
            use_internal_speaker=internal, greeting="Ciao, sessione live attivata.", **kw)


class ForkDecision(_Base):
    def test_internal_speaker_of_enabled_type_goes_to_the_hub(self):
        for device_type in ("AtomS3R", "AndroidPhone", "AndroidWear"):
            self.device_type = device_type
            self.assertEqual(main._live_session_backend(DEV, True), "hub", device_type)

    def test_external_speaker_stays_on_todays_session(self):
        self.assertEqual(main._live_session_backend(DEV, False), "orchestrator")

    def test_device_type_not_enabled_stays_on_todays_session(self):
        self.device_type = "NabuVoice"
        self.assertEqual(main._live_session_backend(DEV, True), "orchestrator")
        main.get_device_speaker_config = lambda _d: None  # unconfigured device
        self.assertEqual(main._live_session_backend(DEV, True), "orchestrator")

    def test_port_zero_turns_the_feature_off(self):
        config.VOICE_LIVE_HUB_PORT = 0
        self.assertEqual(main._live_session_backend(DEV, True), "orchestrator")


class HubSessionStart(_Base):
    async def test_enriched_start_and_nothing_else(self):
        wsh.open_turn(DEV)  # the "avvia una sessione live" turn
        session = await self.start()

        self.assertEqual(session.backend, "hub")
        self.assertEqual(self.ws.types(), ["live_session_start"])
        msg = self.ws.sent[0]
        self.assertEqual(set(msg), {"type", "backend", "call_id", "profile", "name", "voice",
                                    "location_id", "room", "deliver", "hub"})
        self.assertEqual(msg["backend"], "hub")
        self.assertRegex(msg["call_id"], r"^[0-9a-f]{8}$")
        self.assertEqual(msg["call_id"], session.hub_call_id)
        self.assertEqual((msg["profile"], msg["name"]), ("hermes-marco", "Marco"))
        self.assertEqual((msg["location_id"], msg["room"]), ("albani20", "Studio"))
        self.assertIsInstance(msg["voice"], str)
        self.assertTrue(msg["deliver"] and all(isinstance(d, str) for d in msg["deliver"]))
        self.assertEqual(msg["hub"], {"host": "100.116.99.9", "port": HUB_PORT})
        # no greeting, no trigger_listen, and the activation turn is closed without tts_done
        self.assertEqual(self.spoken, [])
        self.assertFalse(wsh.is_turn_open(DEV))
        self.assertIs(main._live_sessions[DEV], session)
        self.assertEqual(session.max_duration_s, config.VOICE_LIVE_HUB_MAX_S)

    async def test_unidentified_speaker_is_famiglia_on_the_default_profile(self):
        await self.start(speaker_id=None, name="Anonimo")
        msg = self.ws.sent[0]
        self.assertEqual((msg["profile"], msg["name"]), ("hermes-shared", "Famiglia"))

    async def test_placeholder_name_with_a_speaker_id_is_still_famiglia(self):
        """Forced device whose user was deleted: speaker_id set, name "Sconosciuto"."""
        await self.start(speaker_id="u7", name="Sconosciuto")
        msg = self.ws.sent[0]
        self.assertEqual((msg["profile"], msg["name"]), ("hermes-shared", "Famiglia"))

    async def test_identified_speaker_without_a_profile_gets_the_default_profile(self):
        await self.start(speaker_id="u9", name="Giorgio")
        msg = self.ws.sent[0]
        self.assertEqual((msg["profile"], msg["name"]), ("hermes-shared", "Giorgio"))

    async def test_each_session_gets_a_new_call_id(self):
        first = (await self.start()).hub_call_id
        main._live_sessions.pop(DEV, None)
        self.assertNotEqual((await self.start()).hub_call_id, first)

    async def test_unreachable_device_leaves_no_session(self):
        wsh._persistent_connections.pop(DEV, None)
        await self.start()
        self.assertNotIn(DEV, main._live_sessions)

    async def test_pending_path_opens_a_hub_session_too(self):
        """Unrecognised speaker: question, "si'", then the session opens on the hub."""
        async def transcribe(_audio):
            return SttResult(status=STT_OK, text="sì")
        saved = main.transcribe_audio
        main.transcribe_audio = transcribe
        try:
            ctx = {"speaker_id": None, "speaker_name": "Anonimo", "speaker_identified": False}
            cfg = {"use_internal_speaker": True, "device_type": "AtomS3R"}
            await main._activate_live_session(DEV, cfg, ctx, "albani20", "Studio")
            self.assertIn(DEV, main._pending_live_sessions)
            self.ws.sent.clear()
            await main._handle_pending_live_session(DEV, b"\x10\x00" * 1600)
        finally:
            main.transcribe_audio = saved
            main._pending_live_sessions.pop(DEV, None)
        self.assertEqual(self.ws.types(), ["live_session_start"])
        self.assertEqual(self.ws.sent[0]["backend"], "hub")
        self.assertEqual(self.ws.sent[0]["name"], "Famiglia")


class FeatureOffIsUnchanged(_Base):
    hub_port = 0

    async def test_todays_start_greeting_and_listen(self):
        session = await self.start()
        self.assertEqual(session.backend, "orchestrator")
        self.assertIsNone(session.hub_call_id)
        self.assertEqual(self.ws.sent[0], {"type": "live_session_start"})
        self.assertEqual(self.spoken, ["Ciao, sessione live attivata."])
        self.assertEqual(self.ws.types()[-1], "trigger_listen")

    async def test_todays_end_says_goodbye(self):
        await self.start()
        self.ws.sent.clear()
        await main.end_live_session(DEV, reason="button_stop")
        self.assertEqual(self.ws.types(), ["live_session_end", "tts_done"])
        self.assertEqual(self.spoken[-1], "Sessione live terminata.")


class ExternalSpeakerIsUnchanged(_Base):
    async def test_external_speaker_with_the_hub_on_keeps_todays_session(self):
        session = await self.start(internal=False)
        self.assertEqual(session.backend, "orchestrator")
        self.assertEqual(self.ws.sent[0], {"type": "live_session_start"})
        self.assertEqual(self.spoken, ["Ciao, sessione live attivata."])


class HubSessionEnd(_Base):
    async def test_end_sends_live_session_end_then_tts_done_without_goodbye(self):
        await self.start()
        self.ws.sent.clear()
        await main.end_live_session(DEV, reason="button_stop")
        self.assertEqual(self.ws.types(), ["live_session_end", "tts_done"])
        self.assertEqual(self.spoken, [])
        self.assertNotIn(DEV, main._live_sessions)

    async def test_end_without_closing_the_turn(self):
        await self.start()
        self.ws.sent.clear()
        await main.end_live_session(DEV, reason="turn_during_hub", close_turn=False)
        self.assertEqual(self.ws.types(), ["live_session_end"])
        self.assertNotIn(DEV, main._live_sessions)

    async def test_audio_turn_during_a_hub_session_ends_it_and_takes_the_normal_path(self):
        """Desync: the device is no longer in the hub session, the command must still work."""
        stt_calls = []

        async def transcribe(_audio, *a, **k):
            stt_calls.append(len(_audio))
            return SttResult(status=STT_NO_SPEECH, text="")
        saved = main.transcribe_audio
        main.transcribe_audio = transcribe
        try:
            session = await self.start()
            self.ws.sent.clear()
            wsh.open_turn(DEV)
            await main._process_ws_audio(DEV, b"\x10\x00" * 1600)
        finally:
            main.transcribe_audio = saved
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(session.turn_count, 0)
        self.assertTrue(stt_calls, "the turn did not reach the normal pipeline")
        types = self.ws.types()
        # the session ends silently, without closing the turn; the normal pipeline closes it
        self.assertEqual(types[0], "live_session_end")
        self.assertNotIn("live_session_start", types)
        self.assertFalse(wsh.is_turn_open(DEV))

    async def test_speaker_stop_during_a_hub_session_ends_it_and_closes_the_turn(self):
        await self.start()
        ws = FakeWebSocket(incoming=[{"type": "speaker_stop"}])
        wsh._persistent_connections.pop(DEV, None)
        await wsh.ws_audio_endpoint(ws, DEV, config.DEVICE_API_TOKEN, main._process_ws_audio)
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(ws.types(), ["live_session_end", "tts_done"])
        self.assertEqual(self.spoken, [])


class NoAnnouncementsDuringAHubSession(_Base):
    async def test_hub_session_is_not_a_turn_loop_session(self):
        await self.start()
        self.assertTrue(main.has_hub_live_session(DEV))
        self.assertFalse(wsh._is_device_in_live_session(DEV))

    async def test_todays_session_still_counts_as_live(self):
        config.VOICE_LIVE_HUB_PORT = 0
        await self.start()
        self.assertFalse(main.has_hub_live_session(DEV))
        self.assertTrue(wsh._is_device_in_live_session(DEV))

    async def test_speak_to_device_refuses_without_touching_the_device(self):
        real_speak_to_device = next(v for o, n, v in self._saved
                                    if o is internal_tts and n == "speak_to_device")
        await self.start()
        self.ws.sent.clear()
        self.assertEqual(await real_speak_to_device("Il timer e' scaduto", DEV), (False, 0.0))
        self.assertEqual(self.ws.sent, [])

    async def test_notify_device_falls_back_to_text(self):
        dev = NS(device_id=DEV, friendly_name="Studio", location_id="albani20",
                 use_internal_speaker=True, output_speaker=None, enabled=True,
                 device_type="AtomS3R")
        saved = [(database, n, getattr(database, n)) for n in (
            "get_voice_device", "get_all_voice_devices", "get_global_preference", "log_event")]
        database.get_voice_device = lambda _d: dev if _d == DEV else None
        database.get_all_voice_devices = lambda: [dev]
        database.get_global_preference = lambda _k, default=None: default
        database.log_event = lambda *a, **k: None
        try:
            await self.start()
            self.ws.sent.clear()
            req = tools_api.NotifyDeviceRequest(text="Il timer e' scaduto", device_id=DEV,
                                                urgent=True)
            res = await tools_api.tool_notify_device(req, None)
        finally:
            for obj, name, value in saved:
                setattr(obj, name, value)
        self.assertFalse(res.delivered)
        self.assertIn("sessione live", res.reason)
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.ws.sent, [])


class InboundLiveSessionEnd(_Base):
    async def drive(self, *messages):
        """Run the real WS endpoint with these control messages, then a disconnect."""
        ws = FakeWebSocket(incoming=messages)
        wsh._persistent_connections.pop(DEV, None)
        await wsh.ws_audio_endpoint(ws, DEV, config.DEVICE_API_TOKEN, main._process_ws_audio)
        return ws

    async def test_button_ends_the_hub_session_without_goodbye(self):
        session = await self.start()
        ws = await self.drive({"type": "live_session_end", "call_id": session.hub_call_id,
                               "reason": "button"})
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(ws.types(), ["live_session_end", "tts_done"])
        self.assertEqual(self.spoken, [])

    async def test_hub_hangup_ends_the_session(self):
        session = await self.start()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, session.hub_call_id, "hub_hangup")
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.ws.types(), ["live_session_end", "tts_done"])
        self.assertEqual(self.spoken, [])

    async def test_audio_start_during_a_hub_session_ends_it_and_the_turn_goes_on(self):
        await self.start()

        class _Session:  # the real one needs the Silero VAD model, not installed in tests
            session_id = "s1"
            _closed = False

            def close(self):
                self._closed = True
        saved = wsh.PersistentDeviceConnection.start_audio_session
        wsh.PersistentDeviceConnection.start_audio_session = lambda self, codec="opus": _Session()
        try:
            ws = await self.drive({"type": "audio_start"})
        finally:
            wsh.PersistentDeviceConnection.start_audio_session = saved
        self.assertNotIn(DEV, main._live_sessions)
        types = ws.types()
        self.assertEqual(types[:2], ["live_session_end", "ready"])  # no tts_done: the turn is open
        self.assertEqual(self.spoken, [])

    async def test_late_reply_during_a_hub_session_goes_to_telegram(self):
        await self.start()
        self.ws.sent.clear()
        sent = []

        async def send_telegram(text, *a, **k):
            sent.append(text)
        saved = main.send_telegram
        main.send_telegram = send_telegram
        try:
            await main.deliver_final_response("ecco il risultato", {"device_id": DEV, "source": "AtomS3R"})
        finally:
            main.send_telegram = saved
        self.assertEqual(sent, ["🎧 ecco il risultato"])
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.ws.sent, [])  # no TTS frames, no tts_done


    async def test_stale_call_id_is_ignored(self):
        session = await self.start()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, "deadbeef", "button")
        self.assertIs(main._live_sessions.get(DEV), session)
        self.assertEqual(self.ws.sent, [])

    async def test_todays_session_is_not_touched(self):
        config.VOICE_LIVE_HUB_PORT = 0
        session = await self.start()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, None, "button")
        self.assertIs(main._live_sessions.get(DEV), session)
        self.assertEqual(self.ws.sent, [])

    async def assert_converted_in_place(self, hub):
        session = main._live_sessions.get(DEV)
        self.assertIsNotNone(session)
        self.assertEqual(session.backend, "orchestrator")
        self.assertIsNone(session.hub_call_id)
        self.assertEqual((session.user_id, session.speaker_name, session.room),
                         (hub.user_id, hub.speaker_name, hub.room))
        # no live_session_end and no tts_done: today's start, greeting, listen
        self.assertEqual(self.ws.types(), ["live_session_start", "trigger_listen"])
        self.assertEqual(self.ws.sent[0], {"type": "live_session_start"})

    async def test_hub_rejected_converts_to_todays_session(self):
        hub = await self.start()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, hub.hub_call_id, "hub_rejected")
        await self.assert_converted_in_place(hub)
        self.assertEqual(self.spoken, ["Ciao Marco, sessione live attivata."])

    async def test_hub_unreachable_through_the_ws_converts_to_todays_session(self):
        hub = await self.start()
        ws = await self.drive({"type": "live_session_end", "call_id": hub.hub_call_id,
                               "reason": "hub_unreachable"})
        session = main._live_sessions.get(DEV)
        self.assertEqual(session.backend, "orchestrator")
        self.assertEqual(ws.types(), ["live_session_start", "trigger_listen"])

    async def test_error_converts_to_todays_session(self):
        # The Atom could not enter duplex, or the app has no microphone, after the hub accepted
        hub = await self.start()
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, hub.hub_call_id, "error")
        await self.assert_converted_in_place(hub)
        self.assertEqual(self.spoken, ["Ciao Marco, sessione live attivata."])

    async def test_error_without_call_id_converts_to_todays_anonymous_session(self):
        hub = await self.start(speaker_id=None, name="Anonimo")
        self.ws.sent.clear()
        await main.handle_device_live_session_end(DEV, "", "error")
        await self.assert_converted_in_place(hub)
        self.assertEqual(self.spoken, ["Sessione live anonima attivata."])

    async def test_hub_unreachable_on_a_disconnected_device_does_not_fall_back(self):
        hub = await self.start()
        wsh._persistent_connections.pop(DEV, None)
        await main.handle_device_live_session_end(DEV, hub.hub_call_id, "hub_unreachable")
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.spoken, [])


class MonitorExemption(_Base):
    async def run_monitor_once(self):
        ended = []

        async def end(dev, reason="?"):
            ended.append((dev, reason))
            main._live_sessions.pop(dev, None)

        calls = []
        real_sleep = asyncio.sleep

        async def one_tick(_s):
            calls.append(_s)
            if len(calls) > 1:
                raise asyncio.CancelledError
            await real_sleep(0)

        saved = main.end_live_session
        main.end_live_session = end
        asyncio.sleep = one_tick
        try:
            with self.assertRaises(asyncio.CancelledError):
                await main.live_session_monitor()
        finally:
            asyncio.sleep = real_sleep
            main.end_live_session = saved
        return ended

    async def test_hub_session_is_exempt_from_inactivity(self):
        session = await self.start()
        session.last_activity = time.time() - 10 * session.max_inactivity_s
        session.started_at = time.time() - 1000  # past today's 900 s cap too
        self.assertEqual(await self.run_monitor_once(), [])
        self.assertIn(DEV, main._live_sessions)

    async def test_hub_session_is_capped_at_the_safety_limit(self):
        session = await self.start()
        session.started_at = time.time() - config.VOICE_LIVE_HUB_MAX_S - 1
        self.assertEqual(await self.run_monitor_once(), [(DEV, "max_duration")])

    async def test_todays_session_still_times_out_on_inactivity(self):
        config.VOICE_LIVE_HUB_PORT = 0
        session = await self.start()
        session.last_activity = time.time() - session.max_inactivity_s - 1
        self.assertEqual(await self.run_monitor_once(), [(DEV, "inactivity_timeout")])


if __name__ == "__main__":
    unittest.main()
