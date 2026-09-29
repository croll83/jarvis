"""Test di hermes_notify (logica pura) e della regola lessicale del router."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hermes_notify as hn  # noqa: E402
import router_model as rm  # noqa: E402


def dev(device_id, name, loc, internal, speaker, enabled=True, dtype="AtomS3R"):
    return NS(device_id=device_id, friendly_name=name, location_id=loc, use_internal_speaker=internal,
              output_speaker=speaker, enabled=enabled, device_type=dtype)


DEVS = [
    dev("AA0000000001", "Garage", "albani20", False, "media_player.echo_dot_garage"),
    dev("AA0000000002", "Casa", "albani20", True, "tts.speak"),
    dev("AA0000000003", "Casa", "wagmi", True, "tts.speak"),
    dev("AA0000000004", "Telefono Test", "albani20", True, "tts.speak", dtype="AndroidPhone"),
    dev("AAAAAAAAAAAA", "Spento", "albani20", False, "media_player.x", enabled=False),
    dev("BBBBBBBBBBBB", None, "albani20", False, None),
]
BY_ID = {d.device_id: d for d in DEVS}


def resolve(**kw):
    kw.setdefault("connected_ids", ["aa0000000002"])
    return hn.resolve_target(get_device=BY_ID.get, all_devices=lambda: DEVS, **kw)


class Resolve(unittest.TestCase):
    def test_by_mac_any_format(self):
        t = resolve(device_id="aa:00:00:00:00:01")
        self.assertEqual((t.device_id, t.audio_path, t.output_speaker),
                         ("AA0000000001", "media_player", "media_player.echo_dot_garage"))

    def test_internal_speaker_and_connection(self):
        t = resolve(device_id="AA0000000002")
        self.assertEqual((t.audio_path, t.connected), ("internal", True))
        self.assertIsNone(hn.precheck(t))
        t = resolve(device_id="AA0000000003")
        self.assertEqual(hn.precheck(t), "device non connesso")

    def test_media_player_needs_no_websocket(self):
        self.assertIsNone(hn.precheck(resolve(device_id="AA0000000001", connected_ids=[])))

    def test_by_name_needs_location_when_ambiguous(self):
        with self.assertRaises(hn.NotifyError) as e:
            resolve(name="casa")
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(resolve(name="Casa", location_id="wagmi").device_id, "AA0000000003")

    def test_unknown_unconfigured_disabled(self):
        for kw in ({"device_id": "123456789ABC"}, {"device_id": "AAAAAAAAAAAA"},
                   {"device_id": "BBBBBBBBBBBB"}, {"name": "Spento"}, {"name": "nessuno"}):
            with self.assertRaises(hn.NotifyError) as e:
                resolve(**kw)
            self.assertEqual(e.exception.status, 404, kw)

    def test_bad_input(self):
        for kw in ({"device_id": "xyz"}, {}, {"name": "  "}):
            with self.assertRaises(hn.NotifyError) as e:
                resolve(**kw)
            self.assertEqual(e.exception.status, 400, kw)


class Text(unittest.TestCase):
    def test_question_mark_never_reopens_mic(self):
        self.assertEqual(hn.clean_text("  Marco,\n sono passati 10 minuti?? "), "Marco, sono passati 10 minuti.")

    def test_limit_and_empty(self):
        self.assertLessEqual(len(hn.clean_text("parola " * 200)), hn.MAX_TEXT_CHARS + 1)
        with self.assertRaises(hn.NotifyError):
            hn.clean_text("   ")

    def test_sound(self):
        self.assertIsNone(hn.clean_sound(None))
        self.assertEqual(hn.clean_sound("positive"), "positive")
        with self.assertRaises(hn.NotifyError):
            hn.clean_sound("boom")


class Quiet(unittest.TestCase):
    def test_silent_hours_over_midnight(self):
        self.assertTrue(hn.in_silent_hours(23, 23, 7))
        self.assertTrue(hn.in_silent_hours(3, 23, 7))
        self.assertFalse(hn.in_silent_hours(7, 23, 7))
        self.assertTrue(hn.in_silent_hours(14, 13, 15))

    def test_urgent_always_speaks(self):
        self.assertIsNone(hn.quiet_reason(dnd=True, silent_hours=True, urgent=True))
        self.assertEqual(hn.quiet_reason(dnd=True, silent_hours=False, urgent=False), "non disturbare attivo")
        self.assertEqual(hn.quiet_reason(dnd=False, silent_hours=True, urgent=False), "ore silenziose")
        self.assertIsNone(hn.quiet_reason(dnd=False, silent_hours=False, urgent=False))


class RouterRule(unittest.TestCase):
    AGENT = [
        "imposta un timer di 10 minuti",
        "Jarvis timer 5 minuti",
        "mettimi un timer da un quarto d'ora",
        "svegliami domani alle 7",
        "metti la sveglia alle 6:30",
        "ricordami di chiamare Ada tra un'ora",
        "avvisami quando bitcoin arriva a 85000 dollari",
        "monitora la repo croll83/jarvis e avvisami se aprono una PR",
        "monitorami il prezzo di ethereum",
        "avvia un monitoraggio sulla pagina di openai",
        "tieni d'occhio la pagina di openai",
        "fai partire un conto alla rovescia di 3 minuti",
        "notificami su telegram tra 20 minuti",
    ]
    NOT_AGENT = [
        "accendi la luce della cucina",
        "metti musica sulla sveglia",
        "alza il volume della sveglia",
        "alza il volume della sveglia a 30",
        "spegni la sveglia ora",
        "metti la radio alla sveglia",
        "spegni la radiosveglia",
        "accendi il monitor",
        "spegni il monitor del pc",
        "spegni la sveglia",
        "che ore sono",
        "apri il garage",
    ]

    def test_goes_to_agent(self):
        for t in self.AGENT:
            self.assertTrue(rm.serve_strumento_esterno(t), t)
            self.assertTrue(rm.e_avviso(t), t)

    def test_domotics_untouched(self):
        for t in self.NOT_AGENT:
            self.assertFalse(rm.serve_strumento_esterno(t), t)
            self.assertFalse(rm.e_avviso(t), t)

    def test_other_agent_requests_are_not_alerts(self):
        for t in ("leggi le mie email", "analizza i consumi di ieri", "prenota un volo per Roma"):
            self.assertTrue(rm.serve_strumento_esterno(t), t)
            self.assertFalse(rm.e_avviso(t), t)


if __name__ == "__main__":
    unittest.main()
