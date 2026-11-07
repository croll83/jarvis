"""Scenari deterministici e casa dei device personali (incidente del 03/11/2026).

Dal watch, configurato su Milano mentre Marco era a Napoli, "avvia lo scenario
esco" e' diventato "accendi la luce della Sala" ed e' stato eseguito a Milano;
"esco" da solo, a Napoli, sarebbe diventato "Rientro a casa".

  docker run --rm --network none -v $PWD/jarvis-orchestrator/tests:/app/tests:ro \
    jarvis-orchestrator:stt-test python -m unittest -v tests.test_scenarios_location
"""
import asyncio
import os
import unittest

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")
os.environ.setdefault("MEM0_BASE_URL", "http://127.0.0.1:1")

import database  # noqa: E402
database.init_db()
import main  # noqa: E402
import router_model as rm  # noqa: E402
from types import SimpleNamespace  # noqa: E402


class ScenarioRecognition(unittest.TestCase):
    SI = {
        "Avvia lo scenario esco": "esco", "Avia esco.": "esco", "esco": "esco", "Jarvis, esco": "esco",
        "fai partire lo scenario esco": "esco",
        "Cervis avvia rientro": "rientro_a_casa", "Darvis segui rientro.": "rientro_a_casa",
        "Rientro a casa": "rientro_a_casa", "avvia rientro a casa": "rientro_a_casa",
        "sono rientrato": "rientro_a_casa",
        "buongiorno": "buongiorno", "Jarvis buonanotte": "buonanotte",
        "attiva la modalità buonanotte": "buonanotte",
    }
    NO = ["domani esco", "a che ora esco", "buongiorno, che tempo fa?", "spegni le luci, esco",
          "accendi la luce della sala", "quando esco ricordami le chiavi", "esco tra dieci minuti",
          "il rientro è previsto alle 8", "accendi rientro", ""]

    def test_requests_are_recognised(self):
        for testo, chiave in self.SI.items():
            self.assertEqual(rm.scenario_richiesto(testo), chiave, testo)

    def test_other_sentences_are_left_to_the_router(self):
        for testo in self.NO:
            self.assertIsNone(rm.scenario_richiesto(testo), testo)

    def test_every_scenario_has_an_entity(self):
        for chiave, (eid, nome, modi) in rm.SCENARI.items():
            self.assertTrue(eid.startswith("script."), chiave)
            self.assertTrue(nome and modi)


class PersonalDeviceLocation(unittest.TestCase):
    def setUp(self):
        self._ul, self._loc = main.get_user_location, main.get_location

    def tearDown(self):
        main.get_user_location, main.get_location = self._ul, self._loc

    def _with(self, user_loc, existing=("wagmi", "albani20")):
        main.get_user_location = lambda uid: (SimpleNamespace(location_id=user_loc) if user_loc else None)
        main.get_location = lambda lid: (SimpleNamespace(id=lid, enabled=True) if lid in existing else None)

    def test_personal_device_follows_its_owner(self):
        self._with("wagmi")
        cfg = {"location_id": "albani20", "forced_user_id": 1}
        self.assertEqual(main._voice_device_location(cfg), "wagmi")

    def test_fixed_device_stays_in_its_house(self):
        self._with("wagmi")
        self.assertEqual(main._voice_device_location({"location_id": "albani20"}), "albani20")

    def test_unknown_owner_location_keeps_the_device_house(self):
        self._with(None)
        cfg = {"location_id": "albani20", "forced_user_id": 1}
        self.assertEqual(main._voice_device_location(cfg), "albani20")
        self._with("casa_cancellata")
        self.assertEqual(main._voice_device_location(cfg), "albani20")


class ScenarioExecution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls, self.said = [], []
        self.saved = {n: getattr(main, n) for n in ("deliver_final_response", "watch_scenario_run",
                                                     "save_chat_message", "process_jarvis_logic")}
        self._ha = (main.multi_ha.get_state, main.multi_ha.call_service)

        async def deliver(text, context, sound_type=None):
            self.said.append(text)

        async def watch(*_a, **_k):
            return None

        async def call(loc, domain, service, data):
            self.calls.append((loc, domain, service, data.get("entity_id")))
            return True, "ok"
        main.deliver_final_response = deliver
        main.watch_scenario_run = watch
        main.save_chat_message = lambda *a, **k: None
        main.multi_ha.call_service = call
        self.scripts = {"wagmi": {"script.esco", "script.rientro_a_casa", "script.buongiorno", "script.buonanotte"},
                        "albani20": set()}

        async def state(loc, eid):
            return {"state": "off"} if eid in self.scripts.get(loc, set()) else None
        main.multi_ha.get_state = state

    async def asyncTearDown(self):
        for n, f in self.saved.items():
            setattr(main, n, f)
        main.multi_ha.get_state, main.multi_ha.call_service = self._ha

    async def _say(self, text, location):
        ctx = {"source": "AndroidWear", "room": "Unknown", "location": location, "device_id": "T",
               "speaker_id": 1, "speaker_name": "Marco", "is_admin": True, "speaker_identified": True}
        await main.process_jarvis_logic(text, ctx)
        await asyncio.sleep(0.05)  # lo script parte in un task

    async def test_scenario_runs_the_right_script_in_the_right_house(self):
        await self._say("Avvia lo scenario esco", "wagmi")
        self.assertEqual(self.calls, [("wagmi", "script", "turn_on", "script.esco")])
        self.assertEqual(self.said, ["Avvio lo scenario Esco."])

    async def test_esco_alone_is_not_rientro(self):
        await self._say("esco", "wagmi")
        self.assertEqual(self.calls, [("wagmi", "script", "turn_on", "script.esco")])

    async def test_missing_scenario_runs_nothing(self):
        # il caso del 03/11: a Milano lo scenario non c'e' — niente luce della Sala
        await self._say("Avvia lo scenario esco", "albani20")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.said), 1)
        self.assertIn("non c'è", self.said[0])

    async def test_buonanotte_keeps_its_greeting(self):
        await self._say("jarvis buonanotte", "wagmi")
        self.assertEqual(self.calls, [("wagmi", "script", "turn_on", "script.buonanotte")])
        self.assertEqual(self.said, ["Buonanotte! Avvio lo scenario."])


    async def test_scenario_runs_even_with_the_router_offline(self):
        # e' una regola: non deve dipendere dal modello
        st = main.service_status
        saved = st.is_critical_online
        st.is_critical_online = lambda: False
        try:
            await self._say("buongiorno", "wagmi")
        finally:
            st.is_critical_online = saved
        self.assertEqual(self.calls, [("wagmi", "script", "turn_on", "script.buongiorno")])


if __name__ == "__main__":
    unittest.main()
