"""D1 end to end: the REAL forward_to_ai_agent (main.py) against a fake Hermes multiplexer.

Runs in the orchestrator image with --network none (only loopback):
  docker run --rm --network none -v $PWD/jarvis-orchestrator/tests:/app/tests:ro \
    jarvis-orchestrator:mux-test python -m unittest -v tests.test_forward_e2e

Covers what the pure selector tests cannot (REVIEW v3 N6): the Telegram producer context as
main.py builds it, URL + token chosen together on the wire, X-Hermes-Session-Id, SSE assembly
and streaming TTS, a speaker change inside one conversation, a missing credential and a 401 —
both answered locally, never re-routed to another profile or to legacy.
"""
import asyncio
import json
import os
import socket
import unittest

TOKENS = {"hermes-marco": "tok-marco", "hermes-ada": "tok-ada", "hermes-shared": "tok-shared"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
LEGACY_PORT = free_port()
os.environ.update({
    "AI_AGENT_ROUTING_MODE": "multiplex", "AI_AGENT_MUX_URL": f"http://127.0.0.1:{PORT}",
    "AI_AGENT_URL": f"http://127.0.0.1:{LEGACY_PORT}", "AI_AGENT_TOKEN": "tok-legacy",
    "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:1/0"),
    "MEM0_BASE_URL": os.environ.get("MEM0_BASE_URL", "http://127.0.0.1:1"),
    **{f"AI_AGENT_TOKEN_{p.upper().replace('-', '_')}": t for p, t in TOKENS.items()},
})

from aiohttp import web  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402

LOCAL = "LOCAL-FALLBACK"


class FakeHermes:
    """Serves /p/<profile>/v1/chat/completions (and legacy /v1/...) with SSE; records every call."""

    def __init__(self):
        self.calls = []
        self.status = {}

    async def handle(self, request):
        body = await request.json()
        system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
        self.calls.append({"path": request.path, "auth": request.headers.get("Authorization"),
                           "session": request.headers.get("X-Hermes-Session-Id"),
                           "speaker": system.split("speaker: ", 1)[1].split("\n", 1)[0] if "speaker: " in system else None})
        code = self.status.get(request.path, 200)
        if code != 200:
            return web.Response(status=code, text="unauthorized")
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for piece in ("Ciao dal profilo. ", "Seconda frase completa."):
            await resp.write(f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n".encode())
        await resp.write(b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        return resp


class ForwardE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeHermes()
        app = web.Application()
        app.router.add_post("/{tail:.*}", self.fake.handle)
        self.runners = []
        for port in (PORT, LEGACY_PORT):
            r = web.AppRunner(app)
            await r.setup()
            await web.TCPSite(r, "127.0.0.1", port).start()
            self.runners.append(r)

        async def quick(text, context):
            return LOCAL
        self._orig_quick = main.get_quick_response
        main.get_quick_response = quick
        config.AI_AGENT_ROUTING_MODE = "multiplex"

    async def asyncTearDown(self):
        main.get_quick_response = self._orig_quick
        for r in self.runners:
            await r.cleanup()

    @staticmethod
    def telegram_context(user_id, name):
        # exactly the keys main.py's Telegram text producer builds (after the N6 fix)
        return {"source": "Telegram", "chat_id": "123", "location": "wagmi", "room": "Unknown",
                "speaker_id": user_id, "speaker_name": name, "is_admin": True, "telegram_id": 42,
                "speaker_identified": True, "identification_method": "telegram"}

    async def test_telegram_marco_reaches_his_profile_with_his_token(self):
        text, _ = await main.forward_to_ai_agent("ciao", self.telegram_context(1, "Marco"), session_user="marco")
        self.assertEqual(text, "Ciao dal profilo. Seconda frase completa.")
        call = self.fake.calls[-1]
        self.assertEqual(call["path"], "/p/hermes-marco/v1/chat/completions")
        self.assertEqual(call["auth"], "Bearer tok-marco")
        self.assertEqual(call["session"], "marco")
        self.assertIn('"identified": true', call["speaker"])

    async def test_unidentified_goes_to_shared(self):
        ctx = {"source": "AtomS3R", "speaker_name": "Marco", "speaker_identified": False}
        await main.forward_to_ai_agent("ciao", ctx)
        self.assertEqual(self.fake.calls[-1]["path"], "/p/hermes-shared/v1/chat/completions")
        self.assertEqual(self.fake.calls[-1]["auth"], "Bearer tok-shared")

    async def test_speaker_change_in_one_conversation_switches_profile_and_token(self):
        await main.forward_to_ai_agent("uno", self.telegram_context(1, "Marco"), session_user="casa")
        await main.forward_to_ai_agent("due", self.telegram_context(2, "Ada"), session_user="casa")
        self.assertEqual([(c["path"], c["auth"], c["session"]) for c in self.fake.calls[-2:]],
                         [("/p/hermes-marco/v1/chat/completions", "Bearer tok-marco", "casa"),
                          ("/p/hermes-ada/v1/chat/completions", "Bearer tok-ada", "casa")])

    async def test_voice_streaming_delivers_sentences_to_tts(self):
        chunks = []

        async def tts(chunk, is_first):
            chunks.append(chunk)
        ctx = {"source": "AtomS3R", "speaker_name": "Ada", "speaker_identified": True}
        text, _ = await main.forward_to_ai_agent("luce", ctx, stream_tts_callback=tts)
        self.assertEqual(self.fake.calls[-1]["path"], "/p/hermes-ada/v1/chat/completions")
        self.assertTrue(chunks and "".join(chunks).replace(" ", "") == text.replace(" ", ""), chunks)

    async def test_missing_credential_says_unavailable_without_any_request(self):
        saved = os.environ.pop("AI_AGENT_TOKEN_HERMES_ADA")
        try:
            before = len(self.fake.calls)
            text, _ = await main.forward_to_ai_agent("ciao", self.telegram_context(2, "Ada"))
        finally:
            os.environ["AI_AGENT_TOKEN_HERMES_ADA"] = saved
        self.assertEqual(text, main.AI_AGENT_UNAVAILABLE_MESSAGE)
        self.assertEqual(len(self.fake.calls), before)

    async def test_401_is_not_rerouted(self):
        self.fake.status["/p/hermes-ada/v1/chat/completions"] = 401
        before = len(self.fake.calls)
        text, _ = await main.forward_to_ai_agent("ciao", self.telegram_context(2, "Ada"))
        self.assertEqual(text, LOCAL)
        self.assertEqual([c["path"] for c in self.fake.calls[before:]], ["/p/hermes-ada/v1/chat/completions"])

    async def test_legacy_mode_is_unchanged(self):
        config.AI_AGENT_ROUTING_MODE = "legacy"
        await main.forward_to_ai_agent("ciao", self.telegram_context(1, "Marco"))
        self.assertEqual(self.fake.calls[-1]["path"], "/v1/chat/completions")
        self.assertEqual(self.fake.calls[-1]["auth"], "Bearer tok-legacy")


class ProducerContract(unittest.TestCase):
    """Every context main.py builds with a resolved speaker must say whether it is identified."""

    def test_contexts_with_a_speaker_declare_identification(self):
        import ast
        from pathlib import Path
        tree = ast.parse(Path(main.__file__).read_text())
        missing = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
                # request contexts carry a source; follow-up bookkeeping records do not go to Hermes
                if "speaker_id" in keys and "speaker_identified" not in keys and "session_user" not in keys:
                    missing.append(node.lineno)
        self.assertEqual(missing, [], "contexts without speaker_identified at lines " + str(missing))


if __name__ == "__main__":
    unittest.main()
