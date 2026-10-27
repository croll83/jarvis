"""Fixture comuni: wake word finta, finto relay verso l'orchestrator, finto device, finto hub."""
import asyncio
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Ambiente ermetico: niente orchestrator vero, niente token, hub spento di default
os.environ["ORCHESTRATOR_WS_URL"] = ""
os.environ["ORCHESTRATOR_URL"] = ""
os.environ["DEVICE_API_TOKEN"] = ""
os.environ["HUB_HOST"] = ""
os.environ["HUB_PORT"] = "0"


class FakeWakeWordEngine:
    """openWakeWord non serve qui (e tflite non esiste per Python 3.12)."""

    def __init__(self, device_id, *a, **k):
        self.device_id = device_id
        self.threshold = 0.5
        self.muted_for = 0.0
        self.processed = 0

    def init(self):
        pass

    def set_threshold(self, t):
        self.threshold = t

    def mute(self, duration_s):
        self.muted_for = duration_s

    def reset(self):
        pass

    def process_audio(self, pcm):
        self.processed += 1
        return False

    def close(self):
        pass


sys.modules["wakeword"] = types.SimpleNamespace(DeviceWakeWordEngine=FakeWakeWordEngine)

import server  # noqa: E402
import hubrelay  # noqa: E402


class FakeOrchRelay:
    """Al posto di OrchestratorRelay: registra quello che va all'orchestrator."""

    instances = []

    def __init__(self, device_id, on_vps_message):
        self.device_id = device_id
        self.on_vps_message = on_vps_message
        self.connected = False
        self.sent = []
        self.sent_bin = []
        FakeOrchRelay.instances.append(self)

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_binary(self, data):
        self.sent_bin.append(data)

    async def close(self):
        self.connected = False

    async def orch(self, msg):
        """Simula un messaggio dall'orchestrator."""
        if isinstance(msg, dict):
            msg = json.dumps(msg)
        await self.on_vps_message(msg)

    def ends(self):
        return [m for m in self.sent if m.get("type") == "live_session_end"]


class FakeDeviceWS:
    def __init__(self):
        self.json = []
        self.binary = []

    async def send_text(self, text):
        self.json.append(json.loads(text))

    async def send_bytes(self, data):
        self.binary.append(data)

    def types(self):
        return [m.get("type") for m in self.json]


class FakeHub:
    """Hub finto che parla il framing ipc. mode: accept | reject | silent | close | truncated."""

    def __init__(self, mode="accept"):
        self.mode = mode
        self.port = 0
        self.connections = 0
        self.offer = None
        self.json = []
        self.audio = bytearray()
        self.audio_frames = 0
        self.audio_call_ids = set()
        self.eof = asyncio.Event()
        self.connected = asyncio.Event()
        self._writer = None
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._writer:
            self._writer.close()
        self._server.close()

    async def _on_client(self, reader, writer):
        self.connections += 1
        self._writer = writer
        self.connected.set()
        try:
            frame = await hubrelay.read_frame(reader)
            if frame is None:
                return
            self.offer = frame[1]
            if self.mode == "accept":
                writer.write(hubrelay.encode_json({"type": "accept", "call_id": self.offer["call_id"]}))
            elif self.mode == "reject":
                writer.write(hubrelay.encode_json({"type": "rejected", "reason": "too many calls"}))
                await writer.drain()
                writer.close()
                return
            elif self.mode == "close":
                # come il hub vero su call_id duplicato: chiude senza mandare niente
                writer.close()
                return
            elif self.mode == "truncated":
                # header JSON da 100 byte, 7 byte di payload e poi chiusura
                writer.write(b"\x01\x00\x00\x00\x64" + b"{\"type\"")
                await writer.drain()
                writer.close()
                return
            while (frame := await hubrelay.read_frame(reader)) is not None:
                kind, data = frame
                if kind == hubrelay.AUDIO:
                    call_id, pcm = data
                    self.audio_call_ids.add(call_id)
                    self.audio += pcm
                    self.audio_frames += 1
                else:
                    self.json.append(data)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.eof.set()

    def send_audio(self, pcm):
        self._writer.write(hubrelay.encode_audio(self.offer["call_id"], pcm))

    def send_json(self, msg):
        self._writer.write(hubrelay.encode_json(msg))

    def close(self):
        self._writer.close()


@pytest.fixture
def fake_relay(monkeypatch):
    FakeOrchRelay.instances.clear()
    monkeypatch.setattr(server, "OrchestratorRelay", FakeOrchRelay)
    return FakeOrchRelay


@pytest.fixture
async def hub():
    h = await FakeHub().start()
    yield h
    await h.stop()


@pytest.fixture
def hub_on(monkeypatch, hub):
    monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "HUB_PORT", hub.port)
    monkeypatch.setattr(server, "AEC_BACKEND", "off")
    return hub


@pytest.fixture
async def conn(fake_relay):
    c = server.DeviceConnection("AA:BB:CC", FakeDeviceWS())
    await server._open_relay(c)
    c.state = server.DeviceState.BUSY  # "avvia una sessione live" arriva a fine turno
    yield c
    await server._abandon_hub_start(c)
    await server._end_hub_session(c, send_ended=True, reopen_relay=False)


def hub_start_msg(call_id="c0ffee01", **over):
    msg = {
        "type": "live_session_start", "backend": "hub", "call_id": call_id,
        "profile": "hermes-marco", "name": "Marco", "voice": "Charon",
        "location_id": "napoli", "room": "studio", "deliver": ["orchestrator"],
        "hub": {"host": "127.0.0.1", "port": 8790},
    }
    msg.update(over)
    return msg


async def orch_hub_start(conn, msg=None, **over):
    """live_session_start "hub" dall'orchestrator e attesa della fine dell'handshake."""
    await conn.relay.orch(msg if msg is not None else hub_start_msg(**over))
    task = conn.hub_start_task
    if task is not None:
        await asyncio.wait({task})


async def wait_for(cond, timeout=3.0, step=0.01):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    return cond()
