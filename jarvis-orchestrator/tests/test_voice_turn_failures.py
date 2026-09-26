"""Rami di fallimento del canale voce: il turno si chiude SEMPRE verso il device.

Sintomo di origine (26/09): audiofront cade a meta' richiesta, transcribe_audio
restituisce stringa vuota, l'orchestrator esce senza rispondere e il watch resta
in attesa finche' non si disconnette. Qui si prova ogni ramo contro un audiofront
finto su loopback e una connessione WS finta che registra cosa arriva al device.

Gira nell'immagine dell'orchestrator senza rete (solo loopback):
  docker run --rm --network none -v $PWD/jarvis-orchestrator/tests:/app/tests:ro \
    jarvis-orchestrator:stt-test python -m unittest -v tests.test_voice_turn_failures
"""
import asyncio
import os
import socket
import time
import unittest


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


STT_PORT = free_port()      # audiofront finto (HTTP)
DROP_PORT = free_port()     # accetta e chiude senza rispondere: "Server disconnected"
CLOSED_PORT = free_port()   # nessuno in ascolto: connessione rifiutata
os.environ.update({
    "STT_URL": f"http://127.0.0.1:{STT_PORT}",
    "STT_DIARIZE": "false",
    "STT_ENGINE": "parakeet",
    "GROQ_API_KEY": "",
    "AI_BACKEND": "local",
    "STT_RETRY_BACKOFF_S": "0.05",
    "STT_CONNECT_TIMEOUT_S": "1",
    "SKIP_PRE_ROUTE": "true",
    "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:1/0"),
    "MEM0_BASE_URL": os.environ.get("MEM0_BASE_URL", "http://127.0.0.1:1"),
})

from aiohttp import web  # noqa: E402

import config  # noqa: E402
import integrations  # noqa: E402
import main  # noqa: E402
import ws_audio_handler as wsh  # noqa: E402
import database  # noqa: E402

# Il DB vive nel container usa-e-getta dei test: tabelle vuote ma presenti, come
# al primo avvio (la pipeline legge le preferenze globali a ogni turno).
database.init_db()
from integrations import (STT_OK, STT_NO_SPEECH, STT_WRONG_LANGUAGE,  # noqa: E402
                          STT_UNAVAILABLE)

DEV = "TESTDEV000001"
PCM = (b"\x10\x00\xf0\xff" * 8000)  # 1 s di "audio": il contenuto non conta, lo trascrive il finto


class FakeAudiofront:
    """Risponde secondo una coda di modalita'; conta le richieste ricevute."""

    def __init__(self):
        self.modes = []
        self.calls = 0

    async def handle(self, request):
        await request.read()
        self.calls += 1
        mode = self.modes.pop(0) if self.modes else "ok"
        if mode == "ok":
            return web.json_response({"text": "accendi la luce della cucina"})
        if mode == "empty":
            return web.json_response({"text": ""})
        if mode == "cyrillic":
            return web.json_response({"text": "привет как дела сегодня"})
        if mode == "diar":  # come audiofront con ?diarize=true e due voci
            return web.json_response({
                "text": "accendi la luce della cucina",
                "segments": [{"speaker": "speaker_0", "start": 0.1, "end": 1.4, "text": "accendi la luce della cucina"},
                             {"speaker": "speaker_1", "start": 1.5, "end": 2.2, "text": ""}],
                "text_attribution": "majority_speaker"})
        return web.Response(status=int(mode), text="giu'")


class Dropper:
    """Legge la richiesta e chiude la connessione senza risposta."""

    def __init__(self):
        self.calls = 0

    async def handle(self, reader, writer):
        self.calls += 1
        await reader.read(1024)
        writer.close()


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)

    def types(self):
        return [m.get("type") for m in self.sent]


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeAudiofront()
        app = web.Application()
        app.router.add_post("/v1/audio/transcriptions", self.fake.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", STT_PORT).start()
        self.dropper = Dropper()
        self.drop_srv = await asyncio.start_server(self.dropper.handle, "127.0.0.1", DROP_PORT)
        self.url_ok = f"http://127.0.0.1:{STT_PORT}/v1/audio/transcriptions"
        config.STT_TRANSCRIBE_URL = self.url_ok
        config.STT_RETRY_ENABLED = True

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.drop_srv.close()
        await self.drop_srv.wait_closed()
        config.STT_TRANSCRIBE_URL = self.url_ok


class TranscribeOutcomes(_Base):
    async def test_ok(self):
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_OK)
        self.assertEqual(r.text, "accendi la luce della cucina")

    async def test_empty_is_no_speech_not_failure(self):
        self.fake.modes = ["empty"]
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_NO_SPEECH)
        self.assertEqual(self.fake.calls, 1)

    async def test_cyrillic_is_wrong_language(self):
        self.fake.modes = ["cyrillic"]
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_WRONG_LANGUAGE)

    async def test_server_disconnected_retried_once_then_unavailable(self):
        config.STT_TRANSCRIBE_URL = f"http://127.0.0.1:{DROP_PORT}/v1/audio/transcriptions"
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_UNAVAILABLE)
        self.assertIn("ServerDisconnected", r.detail)
        self.assertEqual(self.dropper.calls, 2)  # un solo nuovo tentativo

    async def test_connection_refused_is_unavailable(self):
        config.STT_TRANSCRIBE_URL = f"http://127.0.0.1:{CLOSED_PORT}/v1/audio/transcriptions"
        t0 = time.monotonic()
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_UNAVAILABLE)
        self.assertLess(time.monotonic() - t0, 3.0)  # veloce: nessuna attesa del timeout STT

    async def test_503_retried_and_recovers(self):
        self.fake.modes = ["503", "ok"]
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_OK)
        self.assertEqual(self.fake.calls, 2)

    async def test_400_not_retried(self):
        self.fake.modes = ["400"]
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_UNAVAILABLE)
        self.assertEqual(self.fake.calls, 1)

    async def test_retry_can_be_disabled(self):
        config.STT_RETRY_ENABLED = False
        self.fake.modes = ["503"]
        r = await integrations.transcribe_audio(PCM)
        self.assertEqual(r.status, STT_UNAVAILABLE)
        self.assertEqual(self.fake.calls, 1)


class WsTurnAlwaysCloses(_Base):
    """Il turno WS: messaggio giusto per ogni motivo, e mai un device appeso."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ws = FakeWebSocket()
        self.conn = wsh.PersistentDeviceConnection(DEV, self.ws, main._process_ws_audio)
        wsh._persistent_connections[DEV] = self.conn
        self.device_type = "AndroidWear"
        self.delivered = []
        self.routed = []
        self._saved = {n: getattr(main, n) for n in (
            "get_device_speaker_config", "build_speaker_context", "restore_speaker",
            "deliver_final_response", "normalize_stt_text", "process_jarvis_logic",
            "set_user_location")}

        async def fake_deliver(text, context, sound_type=None):
            # come la voce interna del device: parla, poi tts_done
            self.delivered.append(text)
            await wsh.notify_tts_done(context["device_id"])

        async def fake_route(text, context):
            self.routed.append(text)
            await wsh.notify_tts_done(context["device_id"])

        async def restore(_dev):
            return {}

        async def ident(t):
            return t

        main.get_device_speaker_config = lambda _d: {
            "device_type": self.device_type, "location_id": "test", "friendly_name": "Test"}
        main.build_speaker_context = lambda *_a, **_k: {"speaker_id": None, "speaker_name": "Anonimo",
                                                        "speaker_identified": False}
        main.restore_speaker = restore
        main.deliver_final_response = fake_deliver
        main.normalize_stt_text = ident
        main.process_jarvis_logic = fake_route
        main.set_user_location = lambda *_a, **_k: None
        from voice_recognition import voice_recognizer
        self._enroll = voice_recognizer.get_all_active_enrollments
        voice_recognizer.get_all_active_enrollments = lambda: []

    async def asyncTearDown(self):
        for n, f in self._saved.items():
            setattr(main, n, f)
        from voice_recognition import voice_recognizer
        voice_recognizer.get_all_active_enrollments = self._enroll
        wsh._persistent_connections.pop(DEV, None)
        wsh._open_turns.pop(DEV, None)
        main._pending_tts_tasks.pop(DEV, None)
        main._ai_agent_followup.pop(DEV, None)
        main._live_sessions.pop(DEV, None)
        await super().asyncTearDown()

    async def turn(self):
        """Come WsAudioSession.deliver_speech: apre il turno e chiama la callback."""
        wsh.open_turn(DEV)
        await main._process_ws_audio(DEV, PCM)

    def assert_closed(self):
        self.assertFalse(wsh.is_turn_open(DEV), f"turno ancora aperto, device ha ricevuto {self.ws.types()}")
        self.assertTrue({"tts_done", "trigger_listen"} & set(self.ws.types()))

    async def test_happy_path_unchanged(self):
        await self.turn()
        self.assertEqual(self.routed, ["accendi la luce della cucina"])
        self.assertEqual(self.delivered, [])
        self.assert_closed()

    async def test_stt_down_tells_the_user(self):
        config.STT_TRANSCRIBE_URL = f"http://127.0.0.1:{DROP_PORT}/v1/audio/transcriptions"
        await self.turn()
        self.assertEqual(self.delivered, [main._VOICE_MSG_STT_UNAVAILABLE])
        self.assertEqual(self.routed, [])
        self.assert_closed()

    async def test_silence_on_watch_says_so_with_a_different_message(self):
        self.fake.modes = ["empty"]
        await self.turn()
        self.assertEqual(self.delivered, [main._VOICE_MSG_NO_SPEECH])
        self.assertNotEqual(main._VOICE_MSG_NO_SPEECH, main._VOICE_MSG_STT_UNAVAILABLE)
        self.assert_closed()

    async def test_silence_on_wake_word_device_closes_quietly(self):
        self.device_type = "AtomS3R"
        self.fake.modes = ["empty"]
        await self.turn()
        self.assertEqual(self.delivered, [])
        self.assert_closed()

    async def test_wrong_language_asks_to_repeat(self):
        self.fake.modes = ["cyrillic"]
        await self.turn()
        self.assertEqual(self.delivered, [main._VOICE_MSG_WRONG_LANGUAGE])
        self.assert_closed()

    async def test_safety_net_closes_when_a_branch_forgets(self):
        # risposta finita altrove (es. Telegram) senza tts_done al device
        async def forgetful(text, context, sound_type=None):
            self.delivered.append(text)
        main.deliver_final_response = forgetful
        self.fake.modes = ["empty"]
        with self.assertLogs("JARVIS_WS_AUDIO", level="WARNING") as log:
            await self.turn()
        self.assert_closed()
        self.assertTrue(any("lasciato aperto" in m for m in log.output))

    async def test_exception_in_pipeline_still_closes(self):
        def boom(_d):
            raise RuntimeError("DB giu'")
        main.get_device_speaker_config = boom
        with self.assertRaises(RuntimeError):
            await self.turn()
        self.assert_closed()

    async def test_pending_post_tts_is_left_alone(self):
        # speaker esterno: il post-TTS chiudera' a fine riproduzione, la rete non deve anticiparlo
        async def external(text, context, sound_type=None):
            self.routed.append(text)
            main._pending_tts_tasks[DEV] = asyncio.create_task(asyncio.sleep(30))
        main.process_jarvis_logic = external
        await self.turn()
        self.assertTrue(wsh.is_turn_open(DEV))
        self.assertNotIn("tts_done", self.ws.types())
        main._pending_tts_tasks[DEV].cancel()

    async def test_deliver_speech_exception_closes_turn(self):
        async def raising(_d, _pcm):
            raise RuntimeError("callback rotta")
        wsh.init_vad()
        sess = wsh.WsAudioSession(device_id=DEV, on_speech_complete=raising, codec="pcm")
        sess._audio_buffer = [__import__("numpy").zeros(1600, dtype="float32")]
        await sess.deliver_speech()
        self.assert_closed()

    async def test_agent_followup_with_stt_down_does_not_loop(self):
        main._ai_agent_followup[DEV] = {
            "timestamp": time.time(), "speaker_id": 1, "speaker_name": "Test", "source": "AndroidWear",
            "room": "Test", "location": "test", "device_config": None}
        main._ai_agent_followup_triggered.add(DEV)
        config.STT_TRANSCRIBE_URL = f"http://127.0.0.1:{DROP_PORT}/v1/audio/transcriptions"
        await self.turn()
        self.assertEqual(self.delivered, [main._VOICE_MSG_STT_UNAVAILABLE])
        self.assertNotIn(DEV, main._ai_agent_followup)
        self.assert_closed()

    async def test_live_session_stt_down_warns_then_gives_up(self):
        said, ended = [], []

        async def say(session, dev, msg):
            said.append(msg)
            await wsh.notify_tts_done(dev)

        async def end(dev, reason="?"):
            ended.append(reason)
            main._live_sessions.pop(dev, None)
            await wsh.notify_tts_done(dev)
        saved = (main._live_session_say_and_listen, main.end_live_session, main.save_chat_message)
        main._live_session_say_and_listen, main.end_live_session = say, end
        try:
            main._live_sessions[DEV] = main.LiveSession(
                device_id=DEV, user_id=None, speaker_name="Test", location_id="test", room="Test",
                media_player_id=None, use_internal_speaker=True, ai_agent_session_user="test")
            config.STT_TRANSCRIBE_URL = f"http://127.0.0.1:{CLOSED_PORT}/v1/audio/transcriptions"
            for _ in range(main._LIVE_MAX_STT_FAILURES):
                await self.turn()
                self.assert_closed()
            self.assertEqual(said, [main._VOICE_MSG_STT_UNAVAILABLE] * (main._LIVE_MAX_STT_FAILURES - 1))
            self.assertEqual(ended, ["stt_unavailable"])
        finally:
            main._live_session_say_and_listen, main.end_live_session, main.save_chat_message = saved



class VoiceCorpusRecording(WsTurnAlwaysCloses):
    """Registrazione dei turni per il banco di prova: WAV + JSON, a turno chiuso."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._corpus = (config.VOICE_CORPUS_ENABLED, config.VOICE_CORPUS_DIR)
        config.VOICE_CORPUS_ENABLED, config.VOICE_CORPUS_DIR = True, self.tmp

    async def asyncTearDown(self):
        config.VOICE_CORPUS_ENABLED, config.VOICE_CORPUS_DIR = self._corpus
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        await super().asyncTearDown()

    def saved(self):
        from pathlib import Path
        return sorted(Path(self.tmp).rglob("*.wav")), sorted(Path(self.tmp).rglob("*.json"))

    async def test_turn_saved_with_what_we_understood(self):
        import json, wave
        self.fake.modes = ["diar"]
        await self.turn()
        wavs, jsons = self.saved()
        self.assertEqual((len(wavs), len(jsons)), (1, 1))
        with wave.open(str(wavs[0])) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (16000, 1, 2))
            self.assertEqual(w.readframes(10**9), PCM)  # l'audio com'e' arrivato
        rec = json.loads(jsons[0].read_text())
        self.assertEqual(rec["device_id"], DEV)
        self.assertEqual(rec["path"], "command")
        self.assertEqual(rec["stt"]["status"], STT_OK)
        self.assertEqual(rec["stt"]["text"], "accendi la luce della cucina")
        self.assertEqual(rec["stt"]["speakers"], 2)  # la telemetria che serve: quante voci
        self.assertIn("speaker_identified", rec["speaker"])
        self.assertIsNone(rec["label"])
        self.assert_closed()

    async def test_failed_turn_is_recorded_too(self):
        import json
        config.STT_TRANSCRIBE_URL = f"http://127.0.0.1:{CLOSED_PORT}/v1/audio/transcriptions"
        await self.turn()
        _, jsons = self.saved()
        self.assertEqual(json.loads(jsons[0].read_text())["stt"]["status"], STT_UNAVAILABLE)

    async def test_disabled_writes_nothing(self):
        config.VOICE_CORPUS_ENABLED = False
        await self.turn()
        self.assertEqual(self.saved(), ([], []))

    async def test_size_cap_drops_oldest_first(self):
        import os, voice_corpus
        from pathlib import Path
        d = Path(self.tmp) / "2026-01-01"
        d.mkdir(parents=True)
        for i in range(4):  # 4 file da 1 MB, dal piu' vecchio al piu' nuovo
            f = d / f"{i}.wav"
            f.write_bytes(b"\0" * 1024 * 1024)
            os.utime(f, (1000 + i, 1000 + i))
        saved_cap = config.VOICE_CORPUS_MAX_MB
        config.VOICE_CORPUS_MAX_MB = 2
        try:
            voice_corpus._enforce_size_cap()
        finally:
            config.VOICE_CORPUS_MAX_MB = saved_cap
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["2.wav", "3.wav"])


class SpeakerFallbackChain(unittest.IsolatedAsyncioTestCase):
    """speak() segnala il fallimento col valore di ritorno: try_speak deve crederci."""

    async def asyncSetUp(self):
        self._speak, self._sws = main.speak, main.speak_with_sound

    async def asyncTearDown(self):
        main.speak, main.speak_with_sound = self._speak, self._sws

    async def test_failed_speak_is_not_success(self):
        async def failing(*_a, **_k):
            return False, "HA non raggiungibile"
        main.speak = main.speak_with_sound = failing
        self.assertFalse(await main.try_speak("ciao", "media_player.x", "test"))
        self.assertFalse(await main.try_speak("ciao", "media_player.x", "test", "negative"))

    async def test_successful_speak(self):
        async def fine(*_a, **_k):
            return True, "ok"
        main.speak = fine
        self.assertTrue(await main.try_speak("ciao", "media_player.x", "test"))


if __name__ == "__main__":
    unittest.main()
