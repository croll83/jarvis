"""wait_for_tts_completion: un player che resta "playing" (Bose che suona la TV) non deve far
aspettare 90 s se la lunghezza del testo e' nota."""
import asyncio
import unittest
from unittest import mock

import main


class TtsCompletionCap(unittest.TestCase):
    def _run(self, states, text_length):
        it = iter(states)

        async def get_state(location_id, entity_id):
            return {"state": next(it, states[-1])}

        clock = {"t": 0.0}

        async def fake_sleep(s):
            clock["t"] += s

        with mock.patch.object(main.multi_ha, "get_state", side_effect=get_state), \
                mock.patch.object(main.asyncio, "sleep", side_effect=fake_sleep), \
                mock.patch.object(main.time, "time", side_effect=lambda: clock["t"]):
            return asyncio.run(main.wait_for_tts_completion("media_player.bose", "loc", text_length=text_length))

    def test_always_playing_is_capped_by_the_estimate(self):
        waited = self._run(["playing"], text_length=100)
        estimated = max(3.0, (0.063051 * 100 - 0.071) * 1.10 + 1.982)
        self.assertLessEqual(waited, estimated * 1.5 + 3.0 + 0.3)
        self.assertGreater(waited, estimated)

    def test_real_stop_is_still_detected(self):
        waited = self._run(["playing"] * 10 + ["idle", "idle"], text_length=100)
        self.assertLess(waited, 3.0)


class LiveActivationKeywords(unittest.TestCase):
    def test_mangled_verb_in_a_short_phrase_still_activates(self):
        self.assertTrue(main._is_live_session_activation("Abbiamo una sessione live."))
        self.assertTrue(main._is_live_session_activation("Avvia una sessione live"))

    def test_long_sentence_without_verb_does_not(self):
        self.assertFalse(main._is_live_session_activation(
            "ieri sera abbiamo parlato a lungo della sessione live con Giorgio"))


if __name__ == "__main__":
    unittest.main()
