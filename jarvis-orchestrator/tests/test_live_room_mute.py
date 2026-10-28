"""Room mute for the whole hub live session (live_room_mute.py).

While a device is in a live session on the Voice Live Hub its voice comes out of the device
itself: every media_player of its room is muted at the start and put back exactly as it was
at the end, on every end path. Muted where the entity supports it (supported_features & 8),
otherwise volume 0 and back; what was already muted is left alone (and not unmuted at the
end); unavailable/off entities and the Voice PE's own media_player are skipped; Home
Assistant errors never break the session. The wake-word speaker_suppress (10%, restore after
STT, 30 s safety) must never leave its 10% as the "prior" state, and its restore/safety
timer must not undo the live mute (and the suppress skips what the live mute holds).
Home Assistant is a fake that keeps the entity states and records the service calls.

Runs in the orchestrator image without network, like the other tests:
  docker run --rm --network none -v $PWD/jarvis-orchestrator/tests:/app/tests:ro \
    jarvis-orchestrator python -m unittest -v tests.test_live_room_mute
"""
import asyncio
import copy
import json
import os
import tempfile
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

import config  # noqa: E402
import database  # noqa: E402
import internal_tts  # noqa: E402
import live_room_mute as lrm  # noqa: E402
import main  # noqa: E402
import speaker_suppress as ss  # noqa: E402
import ws_audio_handler as wsh  # noqa: E402
from integrations import STT_NO_SPEECH, SttResult  # noqa: E402

database.init_db()

DEV = "AB12CD34EF56"
DEV2 = "AB12CD34EF57"
LOC = "albani20"
ECHO = "media_player.echo_studio"        # Alexa: mute + volume
BOSE = "media_player.bose_soundbar"      # volume only
TV = "media_player.tv_studio"            # already muted
SONOS = "media_player.sonos_studio"      # unavailable
VPE = "media_player.jarvis_voice_media_player"  # the Voice PE itself

MUTE_AND_VOLUME = 4 | 8 | 1
VOLUME_ONLY = 4 | 1


def ent(state="playing", volume=0.5, muted=False, features=MUTE_AND_VOLUME):
    attrs = {"supported_features": features, "volume_level": volume}
    if muted is not None:
        attrs["is_volume_muted"] = muted
    return {"state": state, "attributes": attrs}


class FakeHA:
    def __init__(self, states):
        self.states = copy.deepcopy(states)
        self.calls = []
        self.fail = set()          # service names or (service, entity_id) that fail
        self.fail_states = False
        self.log = None            # shared event log (order checks)

    async def get_states_bulk(self, location_id, entity_ids):
        if self.fail_states:
            raise RuntimeError("HA down")
        return {e: copy.deepcopy(self.states[e]) for e in entity_ids if e in self.states}

    async def call_service(self, location_id, domain, service, data):
        eid = data["entity_id"]
        value = data.get("is_volume_muted", data.get("volume_level"))
        self.calls.append((service, eid, value))
        if self.log is not None:
            self.log.append(("ha", service, eid, value))
        if service in self.fail or (service, eid) in self.fail:
            return False, "boom"
        attrs = self.states[eid]["attributes"]
        if service == "volume_mute":
            attrs["is_volume_muted"] = data["is_volume_muted"]
        elif service == "volume_set":
            attrs["volume_level"] = data["volume_level"]
        return True, "OK"

    def attrs(self, eid):
        return {k: v for k, v in self.states[eid]["attributes"].items() if k != "supported_features"}


class _Base(unittest.IsolatedAsyncioTestCase):
    room_players = [ECHO, BOSE, TV, SONOS, VPE]

    def initial_states(self):
        return {
            ECHO: ent(volume=0.45),
            BOSE: ent(volume=0.35, features=VOLUME_ONLY, muted=None),
            TV: ent(state="on", volume=0.2, muted=True),
            SONOS: ent(state="unavailable"),
            VPE: ent(state="idle", volume=0.6),
        }

    async def asyncSetUp(self):
        self.ha = FakeHA(self.initial_states())
        self.before = copy.deepcopy(self.ha.states)
        self.tmp = tempfile.mkdtemp(prefix="lrm-")
        self._saved = [(lrm, n, getattr(lrm, n)) for n in (
            "multi_ha", "_get_device_config", "_media_players", "RESTORE_RETRY_DELAY_S",
            "FAILED_RETRY_DELAY_S")]
        self._saved += [(ss, n, getattr(ss, n)) for n in (
            "multi_ha", "get_device_speaker_config", "_get_room_media_players", "SAFETY_TIMEOUT_SECONDS")]
        self._saved += [(config, n, getattr(config, n)) for n in (
            "VOICE_LIVE_MUTE_ENABLED", "VOICE_LIVE_MUTE_SCOPE", "VOICE_LIVE_MUTE_MAX_S",
            "VOICE_LIVE_MUTE_STATE_FILE", "VOICE_LIVE_MUTE_EXCLUDE_ENTITIES",
            "VOICE_LIVE_MUTE_EXCLUDE_PATTERNS")]
        cfg = {"location_id": LOC, "friendly_name": "Studio", "output_speaker": ECHO,
               "use_internal_speaker": False, "device_type": "AtomS3R"}
        lrm.multi_ha = self.ha
        ss.multi_ha = self.ha
        lrm._get_device_config = lambda _d: dict(cfg)
        ss.get_device_speaker_config = lambda _d: dict(cfg)
        lrm._media_players = lambda loc, room: list(self.room_players)
        ss._get_room_media_players = lambda loc, room: list(self.room_players)
        lrm.RESTORE_RETRY_DELAY_S = 0
        lrm.FAILED_RETRY_DELAY_S = 3600.0
        config.VOICE_LIVE_MUTE_ENABLED = True
        config.VOICE_LIVE_MUTE_SCOPE = "room"
        config.VOICE_LIVE_MUTE_MAX_S = 3600.0
        config.VOICE_LIVE_MUTE_STATE_FILE = os.path.join(self.tmp, "live_room_mute.json")
        config.VOICE_LIVE_MUTE_EXCLUDE_ENTITIES = set()
        config.VOICE_LIVE_MUTE_EXCLUDE_PATTERNS = ["jarvis_voice", "voice_pe", "home_assistant_voice"]

    async def asyncTearDown(self):
        for info in list(lrm._devices.values()):
            t = info.get("safety_task")
            t and t.cancel()
        if lrm._retry_task is not None:
            lrm._retry_task.cancel()
            lrm._retry_task = None
        lrm._devices.clear()
        lrm._holds.clear()
        lrm._failed.clear()
        for info in list(ss._suppressed_devices.values()):
            t = info.get("safety_task")
            t and t.cancel()
        ss._suppressed_devices.clear()
        ss._recent_restores.clear()
        ss._device_locks.clear()
        for obj, name, value in self._saved:
            setattr(obj, name, value)

    def state_file(self):
        return config.VOICE_LIVE_MUTE_STATE_FILE

    def assert_all_restored(self):
        for eid in self.room_players:
            self.assertEqual(self.ha.states[eid], self.before[eid], eid)


class MuteAndRestore(_Base):
    async def test_mute_then_exact_restore(self):
        res = await lrm.mute_room(DEV)
        self.assertEqual(res["status"], "muted")
        self.assertEqual(sorted(res["entities"]), [BOSE, ECHO])
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)
        self.assertEqual(self.ha.states[ECHO]["attributes"]["volume_level"], 0.45)  # volume untouched
        self.assertEqual(self.ha.states[BOSE]["attributes"]["volume_level"], 0)     # no mute: volume 0
        self.assertTrue(lrm.is_entity_held(LOC, ECHO))
        self.assertTrue(lrm.is_room_muted(DEV))

        await lrm.unmute_room(DEV)
        self.assert_all_restored()
        self.assertFalse(lrm.is_room_muted(DEV))
        self.assertFalse(lrm.is_entity_held(LOC, ECHO))

    async def test_already_muted_unavailable_and_own_speaker_are_never_touched(self):
        await lrm.mute_room(DEV)
        await lrm.unmute_room(DEV)
        touched = {eid for _s, eid, _v in self.ha.calls}
        self.assertEqual(touched, {ECHO, BOSE})
        self.assertIs(self.ha.states[TV]["attributes"]["is_volume_muted"], True)  # still muted

    async def test_off_and_missing_entities_are_skipped(self):
        self.ha.states[ECHO]["state"] = "off"
        del self.ha.states[BOSE]
        self.assertEqual((await lrm.mute_room(DEV))["status"], "skip")
        self.assertEqual(self.ha.calls, [])
        self.assertFalse(lrm.is_room_muted(DEV))

    async def test_mute_refused_falls_back_to_volume_zero(self):
        self.ha.fail.add(("volume_mute", ECHO))
        await lrm.mute_room(DEV)
        self.assertEqual(self.ha.states[ECHO]["attributes"]["volume_level"], 0)
        self.ha.fail.clear()
        await lrm.unmute_room(DEV)
        self.assertEqual(self.ha.states[ECHO]["attributes"]["volume_level"], 0.45)
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], False)

    async def test_volume_only_entity_already_at_zero_is_left_alone(self):
        self.ha.states[BOSE]["attributes"]["volume_level"] = 0
        self.before = copy.deepcopy(self.ha.states)
        await lrm.mute_room(DEV)
        await lrm.unmute_room(DEV)
        self.assertNotIn(BOSE, {eid for _s, eid, _v in self.ha.calls})
        self.assert_all_restored()

    async def test_idempotent(self):
        await lrm.mute_room(DEV)
        n = len(self.ha.calls)
        self.assertEqual((await lrm.mute_room(DEV))["status"], "already_muted")
        self.assertEqual(len(self.ha.calls), n)
        await lrm.unmute_room(DEV)
        n = len(self.ha.calls)
        self.assertEqual((await lrm.unmute_room(DEV))["status"], "skip")
        self.assertEqual(len(self.ha.calls), n)
        self.assert_all_restored()

    async def test_excluded_entity_list(self):
        config.VOICE_LIVE_MUTE_EXCLUDE_ENTITIES = {BOSE}
        res = await lrm.mute_room(DEV)
        self.assertEqual(res["entities"], [ECHO])

    async def test_disabled(self):
        config.VOICE_LIVE_MUTE_ENABLED = False
        self.assertEqual((await lrm.mute_room(DEV))["status"], "skip")
        self.assertEqual(self.ha.calls, [])

    async def test_location_scope(self):
        config.VOICE_LIVE_MUTE_SCOPE = "location"
        lrm._media_players = self._saved[2][2]  # the real ones, on the DB
        ss._get_room_media_players = next(v for o, n, v in self._saved
                                          if o is ss and n == "_get_room_media_players")
        conn = database._get_conn()
        try:
            conn.execute("DELETE FROM entity_maps WHERE location_id = ?", ("lrmtest",))
            for room, eid in (("Studio", ECHO), ("Cucina", BOSE)):
                conn.execute("INSERT INTO entity_maps (location_id, zone, area, room, entity_type, "
                             "entity_name, entity_id) VALUES (?, 'z', 'a', ?, 'media_player', ?, ?)",
                             ("lrmtest", room, eid, eid))
            conn.commit()
            self.assertEqual(sorted(lrm._media_players("lrmtest", "Studio")), [BOSE, ECHO])
            config.VOICE_LIVE_MUTE_SCOPE = "room"
            self.assertEqual(lrm._media_players("lrmtest", "Studio"), [ECHO])
        finally:
            conn.execute("DELETE FROM entity_maps WHERE location_id = ?", ("lrmtest",))
            conn.commit()
            conn.close()


class HomeAssistantErrors(_Base):
    async def test_state_fetch_error_does_not_raise(self):
        self.ha.fail_states = True
        self.assertEqual((await lrm.mute_room(DEV))["status"], "skip")

    async def test_service_errors_do_not_raise(self):
        self.ha.fail.update({"volume_mute", "volume_set"})
        self.assertEqual((await lrm.mute_room(DEV))["status"], "skip")
        self.assertFalse(lrm.is_room_muted(DEV))

    async def test_failed_restore_is_kept_and_retried_after_a_restart(self):
        await lrm.mute_room(DEV)
        self.ha.fail.add(("volume_mute", ECHO))
        res = await lrm.unmute_room(DEV)
        self.assertEqual(res["failed"], [ECHO])
        self.assertEqual(self.ha.calls.count(("volume_mute", ECHO, False)), 2)  # retried once
        saved = json.load(open(self.state_file()))
        self.assertEqual([h["entity_id"] for h in saved], [ECHO])
        # "restart": memory lost, HA back
        lrm._failed.clear()
        self.ha.fail.clear()
        await lrm.restore_leftovers(delay=0, retry_delay=0)
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], False)
        self.assertFalse(os.path.exists(self.state_file()))

    async def test_failed_restore_is_retried_in_background_without_a_restart(self):
        lrm.FAILED_RETRY_DELAY_S = 0.01
        await lrm.mute_room(DEV)
        self.ha.fail.add(("volume_mute", ECHO))
        await lrm.unmute_room(DEV)
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)
        self.ha.fail.clear()           # HA back
        await asyncio.wait_for(lrm._retry_task, 2)
        self.assert_all_restored()
        self.assertEqual(lrm._failed, {})
        self.assertFalse(os.path.exists(self.state_file()))

    async def test_failed_restore_is_adopted_by_the_next_session(self):
        await lrm.mute_room(DEV)
        self.ha.fail.add(("volume_mute", ECHO))
        await lrm.unmute_room(DEV)
        self.ha.fail.clear()
        await lrm.mute_room(DEV)   # Echo still muted by us: must not be seen as "already muted"
        await lrm.unmute_room(DEV)
        self.assert_all_restored()


class Persistence(_Base):
    async def test_state_file_follows_the_holds(self):
        await lrm.mute_room(DEV)
        saved = {h["entity_id"]: h for h in json.load(open(self.state_file()))}
        self.assertEqual(set(saved), {ECHO, BOSE})
        self.assertEqual(saved[BOSE]["method"], "volume")
        self.assertEqual(saved[BOSE]["prior_volume"], 0.35)
        await lrm.unmute_room(DEV)
        self.assertFalse(os.path.exists(self.state_file()))

    async def test_restart_restores_what_was_left_muted(self):
        await lrm.mute_room(DEV)
        for info in lrm._devices.values():
            info["safety_task"].cancel()
        lrm._devices.clear()   # the process dies: memory gone, file stays
        lrm._holds.clear()
        await lrm.restore_leftovers(delay=0, retry_delay=0)
        self.assert_all_restored()
        self.assertFalse(os.path.exists(self.state_file()))


    async def test_session_starting_before_the_leftover_restore_adopts_it(self):
        await lrm.mute_room(DEV)
        lrm._devices[DEV]["safety_task"].cancel()
        lrm._devices.clear()   # restart
        lrm._holds.clear()
        restore = asyncio.create_task(lrm.restore_leftovers(delay=0.05, retry_delay=0))
        await asyncio.sleep(0)  # file loaded, restore still waiting
        await lrm.mute_room(DEV)  # Echo is muted (by the old session): must be adopted
        await restore
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)  # session on
        await lrm.unmute_room(DEV)
        self.assert_all_restored()


class SharedAndSafety(_Base):
    async def test_two_sessions_in_the_same_room(self):
        await lrm.mute_room(DEV)
        res = await lrm.mute_room(DEV2)
        self.assertEqual(sorted(res["shared"]), [BOSE, ECHO])
        await lrm.unmute_room(DEV)
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)  # DEV2 still on
        await lrm.unmute_room(DEV2)
        self.assert_all_restored()

    async def test_safety_restore(self):
        config.VOICE_LIVE_MUTE_MAX_S = 0.05
        await lrm.mute_room(DEV)
        await asyncio.sleep(0.2)
        self.assertFalse(lrm.is_room_muted(DEV))
        self.assert_all_restored()

    async def test_release_all(self):
        await lrm.mute_room(DEV)
        await lrm.release_all()
        self.assert_all_restored()


class SuppressInterplay(_Base):
    async def test_live_mute_takes_over_an_active_suppress(self):
        """Wake word lowered the Echo and the Bose to 10%; the session starts before the restore."""
        self.assertEqual((await ss.suppress_speaker(DEV))["status"], "suppressed")
        self.assertEqual(self.ha.states[ECHO]["attributes"]["volume_level"], ss.SUPPRESS_VOLUME)
        safety = ss._suppressed_devices[DEV]["safety_task"]

        await lrm.mute_room(DEV)
        await asyncio.sleep(0)
        self.assertNotIn(DEV, ss._suppressed_devices)
        self.assertTrue(safety.cancelled() or safety.done())
        # Echo muted (its 10% is put back to the original only after the unmute); Bose at 0
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)
        self.assertEqual(self.ha.states[ECHO]["attributes"]["volume_level"], ss.SUPPRESS_VOLUME)
        self.assertEqual(self.ha.states[BOSE]["attributes"]["volume_level"], 0)

        # the post-STT restore must not make anything audible during the session
        n = len(self.ha.calls)
        self.assertEqual((await ss.restore_speaker(DEV))["status"], "skip")
        self.assertEqual(len(self.ha.calls), n)

        self.ha.calls.clear()
        await lrm.unmute_room(DEV)
        self.assert_all_restored()  # original volumes, never the 10%
        # unmute first, then the volume (Alexa-style mute = volume 0 + remembered volume)
        self.assertLess(self.ha.calls.index(("volume_mute", ECHO, False)),
                        self.ha.calls.index(("volume_set", ECHO, 0.45)))

    async def test_suppress_restored_just_before_with_stale_ha_state(self):
        """Restore done, but HA still reports 10%: the prior volume is the recorded original."""
        await ss.suppress_speaker(DEV)
        await ss.restore_speaker(DEV)
        self.ha.states[BOSE]["attributes"]["volume_level"] = ss.SUPPRESS_VOLUME  # lagging state
        await lrm.mute_room(DEV)
        await lrm.unmute_room(DEV)
        self.assertEqual(self.ha.states[BOSE]["attributes"]["volume_level"], 0.35)

    async def test_suppress_skips_what_the_live_mute_holds(self):
        await lrm.mute_room(DEV)
        n = len(self.ha.calls)
        # another device in the same room hears its wake word during the session
        await ss.suppress_speaker(DEV2)
        self.assertEqual({eid for _s, eid, _v in self.ha.calls[n:]} & {ECHO, BOSE}, set())
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)
        self.assertEqual(self.ha.states[BOSE]["attributes"]["volume_level"], 0)
        await ss.restore_speaker(DEV2)
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)
        self.assertEqual(self.ha.states[BOSE]["attributes"]["volume_level"], 0)
        await lrm.unmute_room(DEV)
        self.assert_all_restored()

    async def test_suppress_safety_timer_cannot_undo_the_live_mute(self):
        ss.SAFETY_TIMEOUT_SECONDS = 0.05
        await ss.suppress_speaker(DEV)
        await lrm.mute_room(DEV)
        await asyncio.sleep(0.2)
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)
        self.assertEqual(self.ha.states[BOSE]["attributes"]["volume_level"], 0)
        await lrm.unmute_room(DEV)
        self.assert_all_restored()

    async def test_partial_takeover_leaves_other_rooms_to_the_suppress(self):
        ss._suppressed_devices[DEV2] = {
            "speakers": [{"entity_id": ECHO, "original_volume": 0.45},
                         {"entity_id": "media_player.echo_cucina", "original_volume": 0.3}],
            "location_id": LOC, "room_name": "Cucina", "suppressed_at": time.time(),
            "safety_task": None}
        self.ha.states[ECHO]["attributes"]["volume_level"] = ss.SUPPRESS_VOLUME
        taken = await ss.release_for_live_mute(LOC, [ECHO, BOSE])
        self.assertEqual(taken, {ECHO: 0.45})
        self.assertEqual([s["entity_id"] for s in ss._suppressed_devices[DEV2]["speakers"]],
                         ["media_player.echo_cucina"])


# ---------------------------------------------------------------------------
# Orchestrator: every end path of a hub session unmutes the room
# ---------------------------------------------------------------------------

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


class HubSessionMutesTheRoom(_Base):
    room_players = [ECHO, BOSE, TV]

    def initial_states(self):
        s = super().initial_states()
        return {k: s[k] for k in self.room_players}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ws = FakeWebSocket()
        self.conn = wsh.PersistentDeviceConnection(DEV, self.ws, main._process_ws_audio)
        wsh._persistent_connections[DEV] = self.conn
        self.log = []
        self.ha.log = self.log
        self._saved += [(main, n, getattr(main, n)) for n in ("get_device_speaker_config", "speak")]
        self._saved += [(internal_tts, "speak_to_device", internal_tts.speak_to_device)]
        self._saved += [(config, n, getattr(config, n)) for n in (
            "VOICE_LIVE_HUB_PORT", "VOICE_LIVE_HUB_HOST", "VOICE_LIVE_HUB_DEVICE_TYPES",
            "VOICE_LIVE_HUB_EXCLUDE_DEVICES", "VOICE_LIVE_HUB_DISCONNECT_GRACE_S")]
        self.device_type = "AtomS3R"
        self.internal = False

        async def speak(text, *a, **k):
            self.log.append(("speak", text))

        async def speak_to_device(text, device_id):
            self.log.append(("speak_internal", text))
            return True, 0.1

        main.get_device_speaker_config = lambda _d: {
            "device_type": self.device_type, "location_id": LOC, "friendly_name": "Studio",
            "use_internal_speaker": self.internal, "output_speaker": ECHO}
        main.speak = speak
        internal_tts.speak_to_device = speak_to_device
        config.VOICE_LIVE_HUB_PORT = 8790
        config.VOICE_LIVE_HUB_HOST = "100.116.99.9"
        config.VOICE_LIVE_HUB_DEVICE_TYPES = {"AtomS3R", "NabuVoice", "AndroidPhone", "AndroidWear"}
        config.VOICE_LIVE_HUB_EXCLUDE_DEVICES = set()
        config.VOICE_LIVE_HUB_DISCONNECT_GRACE_S = 0.05

    async def asyncTearDown(self):
        for t in list(main._pending_tts_tasks.values()):
            t.cancel()
        main._pending_tts_tasks.pop(DEV, None)
        for t in list(main._room_mute_tasks.values()):
            t.cancel()
        main._room_mute_tasks.clear()
        wsh._persistent_connections.pop(DEV, None)
        wsh._open_turns.pop(DEV, None)
        main._live_sessions.pop(DEV, None)
        await super().asyncTearDown()

    async def start(self, **kw):
        session = await main.start_live_session(
            device_id=DEV, speaker_id="u1", speaker_name="Marco", location_id=LOC, room="Studio",
            media_player_id=None if self.internal else ECHO, use_internal_speaker=self.internal,
            greeting="Ciao Marco, sessione live attivata.", **kw)
        task = main._room_mute_tasks.get(DEV)
        if task:
            await task
        return session

    def assert_muted(self):
        self.assertTrue(lrm.is_room_muted(DEV))
        self.assertIs(self.ha.states[ECHO]["attributes"]["is_volume_muted"], True)
        self.assertEqual(self.ha.states[BOSE]["attributes"]["volume_level"], 0)

    # -- eligibility --------------------------------------------------------
    def test_eligible_without_the_internal_speaker(self):
        for device_type in ("AtomS3R", "NabuVoice", "AndroidPhone", "AndroidWear"):
            self.device_type = device_type
            self.assertEqual(main._live_session_backend(DEV, False), "hub", device_type)
            self.assertEqual(main._live_session_backend(DEV, True), "hub", device_type)

    def test_per_device_opt_out(self):
        config.VOICE_LIVE_HUB_EXCLUDE_DEVICES = {DEV}
        self.assertEqual(main._live_session_backend(DEV, False), "orchestrator")

    async def test_echo_device_opens_a_hub_session_and_mutes_the_room(self):
        session = await self.start()
        self.assertEqual(session.backend, "hub")
        self.assertEqual([m["type"] for m in self.ws.sent], ["live_session_start"])
        self.assertFalse([e for e in self.log if e[0].startswith("speak")])  # nothing on the Echo
        self.assert_muted()

    async def test_nabuvoice_opens_a_hub_session_and_mutes_the_room(self):
        self.device_type = "NabuVoice"
        session = await self.start()
        self.assertEqual(session.backend, "hub")
        self.assertEqual(self.ws.sent[0]["backend"], "hub")
        self.assert_muted()

    async def test_activation_without_any_speaker_is_allowed_on_the_hub(self):
        cfg = {"use_internal_speaker": False, "device_type": "AtomS3R", "output_speaker": None}
        saved = main.get_room_speakers
        main.get_room_speakers = lambda _loc: {}
        try:
            await main._activate_live_session(
                DEV, cfg, {"speaker_identified": True, "speaker_id": "u1", "speaker_name": "Marco"},
                LOC, "Studio")
        finally:
            main.get_room_speakers = saved
        self.assertEqual(main._live_sessions[DEV].backend, "hub")

    async def test_unknown_speaker_without_any_speaker_starts_anonymous_on_the_hub(self):
        """No speaker to ask "anonymous session?" on: start right away instead of hanging."""
        cfg = {"use_internal_speaker": False, "device_type": "AtomS3R", "output_speaker": None}
        saved = main.get_room_speakers
        main.get_room_speakers = lambda _loc: {}
        try:
            await main._activate_live_session(
                DEV, cfg, {"speaker_identified": False, "speaker_id": None, "speaker_name": "Sconosciuto"},
                LOC, "Studio")
        finally:
            main.get_room_speakers = saved
        self.assertNotIn(DEV, main._pending_live_sessions)
        self.assertEqual(main._live_sessions[DEV].backend, "hub")
        self.assertFalse([e for e in self.log if e[0].startswith("speak")])

    async def test_no_fallback_without_any_speaker(self):
        s = await main.start_live_session(
            device_id=DEV, speaker_id="u1", speaker_name="Marco", location_id=LOC, room="Studio",
            media_player_id=None, use_internal_speaker=False)
        await main._room_mute_tasks[DEV]
        await main.handle_device_live_session_end(DEV, s.hub_call_id, "hub_rejected")
        self.assertNotIn(DEV, main._live_sessions)
        self.assertFalse([e for e in self.log if e[0].startswith("speak")])
        self.assert_all_restored()

    async def test_replacing_hub_session_on_an_unreachable_device_unmutes(self):
        await self.start()
        self.assert_muted()
        wsh._persistent_connections.pop(DEV, None)
        await self.start()
        self.assertNotIn(DEV, main._live_sessions)
        self.assertFalse(lrm.is_room_muted(DEV))
        self.assert_all_restored()

    async def test_unreachable_device_mutes_nothing(self):
        wsh._persistent_connections.pop(DEV, None)
        await self.start()
        self.assertNotIn(DEV, main._live_sessions)
        self.assertEqual(self.ha.calls, [])

    async def test_hub_off_mutes_nothing(self):
        config.VOICE_LIVE_HUB_PORT = 0
        session = await self.start()
        self.assertEqual(session.backend, "orchestrator")
        self.assertEqual(self.ha.calls, [])
        self.assertIn(("speak", "Ciao Marco, sessione live attivata."), self.log)  # on the Echo, as today

    # -- end paths ------------------------------------------------------------
    async def test_orchestrator_side_end(self):
        await self.start()
        await main.end_live_session(DEV, reason="max_duration")
        self.assert_all_restored()

    async def test_button(self):
        s = await self.start()
        await main.handle_device_live_session_end(DEV, s.hub_call_id, "button")
        self.assert_all_restored()

    async def test_hub_hangup(self):
        s = await self.start()
        await main.handle_device_live_session_end(DEV, s.hub_call_id, "hub_hangup")
        self.assert_all_restored()

    async def test_error_on_a_disconnected_device(self):
        s = await self.start()
        wsh._persistent_connections.pop(DEV, None)
        await main.handle_device_live_session_end(DEV, s.hub_call_id, "error")
        self.assertNotIn(DEV, main._live_sessions)
        self.assert_all_restored()

    async def test_fallback_unmutes_before_the_greeting_on_the_echo(self):
        s = await self.start()
        self.log.clear()
        await main.handle_device_live_session_end(DEV, s.hub_call_id, "hub_rejected")
        self.assertEqual(main._live_sessions[DEV].backend, "orchestrator")
        self.assertEqual(main._live_sessions[DEV].media_player_id, ECHO)
        self.assert_all_restored()
        unmute_at = self.log.index(("ha", "volume_mute", ECHO, False))
        speak_at = self.log.index(("speak", "Ciao Marco, sessione live attivata."))
        self.assertLess(unmute_at, speak_at)

    async def test_speaker_stop_through_the_ws(self):
        await self.start()
        ws = FakeWebSocket(incoming=[{"type": "speaker_stop"}])
        wsh._persistent_connections.pop(DEV, None)
        await wsh.ws_audio_endpoint(ws, DEV, config.DEVICE_API_TOKEN, main._process_ws_audio)
        self.assertNotIn(DEV, main._live_sessions)
        self.assert_all_restored()

    async def test_device_reconnect(self):
        await self.start()
        ws = FakeWebSocket(incoming=[{"type": "hello", "fw": "1.1.0-nabuvoice"}])
        wsh._persistent_connections.pop(DEV, None)
        await wsh.ws_audio_endpoint(ws, DEV, config.DEVICE_API_TOKEN, main._process_ws_audio)
        self.assertNotIn(DEV, main._live_sessions)
        self.assert_all_restored()

    async def test_device_disconnect_after_the_grace(self):
        await self.start()
        # the device's own WS drops (no message, just the disconnect)
        ws = FakeWebSocket()
        wsh._persistent_connections.pop(DEV, None)
        await wsh.ws_audio_endpoint(ws, DEV, config.DEVICE_API_TOKEN, main._process_ws_audio)
        self.assertIn(DEV, main._live_sessions)   # grace not over yet
        for _ in range(50):
            if DEV not in main._live_sessions and not lrm.is_room_muted(DEV):
                break
            await asyncio.sleep(0.02)
        self.assertNotIn(DEV, main._live_sessions)
        self.assert_all_restored()

    async def test_device_back_within_the_grace_keeps_the_session(self):
        s = await self.start()
        task = asyncio.create_task(main.handle_hub_device_disconnect(DEV, s.hub_call_id))
        await task  # DEV still in _persistent_connections: it came back
        self.assertIs(main._live_sessions.get(DEV), s)
        self.assert_muted()

    async def test_audio_turn_during_the_session(self):
        async def transcribe(_audio, *a, **k):
            return SttResult(status=STT_NO_SPEECH, text="")
        saved = main.transcribe_audio
        main.transcribe_audio = transcribe
        try:
            await self.start()
            wsh.open_turn(DEV)
            await main._process_ws_audio(DEV, b"\x10\x00" * 1600)
        finally:
            main.transcribe_audio = saved
        self.assertNotIn(DEV, main._live_sessions)
        self.assert_all_restored()

    async def test_audio_start_during_the_session(self):
        await self.start()

        class _Session:
            session_id = "s1"
            _closed = False

            def close(self):
                self._closed = True
        saved = wsh.PersistentDeviceConnection.start_audio_session
        wsh.PersistentDeviceConnection.start_audio_session = lambda self, codec="opus": _Session()
        try:
            ws = FakeWebSocket(incoming=[{"type": "audio_start"}])
            wsh._persistent_connections.pop(DEV, None)
            await wsh.ws_audio_endpoint(ws, DEV, config.DEVICE_API_TOKEN, main._process_ws_audio)
        finally:
            wsh.PersistentDeviceConnection.start_audio_session = saved
        self.assertNotIn(DEV, main._live_sessions)
        self.assert_all_restored()

    async def test_session_ended_before_the_mute_ran(self):
        """The end arrives while the background mute has not run yet: no muted room left."""
        await main.start_live_session(
            device_id=DEV, speaker_id="u1", speaker_name="Marco", location_id=LOC, room="Studio",
            media_player_id=ECHO, use_internal_speaker=False)
        main._live_sessions.pop(DEV, None)          # gone without passing through end_live_session
        await main._room_mute_tasks[DEV]
        self.assertFalse(lrm.is_room_muted(DEV))
        self.assert_all_restored()

    async def test_end_racing_the_mute(self):
        await main.start_live_session(
            device_id=DEV, speaker_id="u1", speaker_name="Marco", location_id=LOC, room="Studio",
            media_player_id=ECHO, use_internal_speaker=False)
        await main.end_live_session(DEV, reason="button_stop")   # mute task still pending
        self.assertFalse(lrm.is_room_muted(DEV))
        self.assert_all_restored()

    async def test_ha_down_does_not_break_the_session(self):
        self.ha.fail_states = True
        s = await self.start()
        self.assertIs(main._live_sessions.get(DEV), s)
        await main.end_live_session(DEV, reason="button_stop")
        self.assertNotIn(DEV, main._live_sessions)

    async def test_late_reply_does_not_play_on_the_echo(self):
        await self.start()
        sent = []

        async def send_telegram(text, *a, **k):
            sent.append(text)
        saved = main.send_telegram
        main.send_telegram = send_telegram
        try:
            await main.deliver_final_response(
                "ecco", {"device_id": DEV, "source": "AtomS3R",
                         "device_config": main.get_device_speaker_config(DEV)})
        finally:
            main.send_telegram = saved
        self.assertEqual(sent, ["🎧 ecco"])
        self.assertFalse([e for e in self.log if e[0].startswith("speak")])


if __name__ == "__main__":
    unittest.main()
