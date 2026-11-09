"""Fase 3 (design 19, 19.12): pre-roll, archivio clip, cancello a voce e live dalla wake word o
dal tap. Finto Atom/NabuVoice, finto orchestrator (relay), finto hub ipc."""
import asyncio
import base64
import json
import os

import numpy as np
import opuslib
import pytest

import preroll
import server
from conftest import FakeDeviceWS, FakeHub, FakeOrchRelay, wait_for

REQ = "r-device-1"


def tone(freq, seconds, amp=8000, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def opus_frames(pcm16k):
    enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    return [enc.encode(pcm16k[i:i + 320].tobytes(), 320) for i in range(0, len(pcm16k) - 319, 320)]


def decoded(frames):
    dec = opuslib.Decoder(16000, 1)
    return b"".join(dec.decode(f, 320) for f in frames)


def hub_live_start(req_id, call_id="c0ffee02", **over):
    msg = {"type": "live_session_start", "backend": "hub", "call_id": call_id, "req_id": req_id,
           "origin": "wake", "identified": True, "profile": "hermes-marco", "name": "Marco",
           "voice": "Charon", "location_id": "napoli", "room": "studio",
           "offer_extra": {"identified": True, "origin": "wake"}}
    msg.update(over)
    return msg


def types(msgs):
    return [m.get("type") for m in msgs]


@pytest.fixture
def flags(monkeypatch):
    monkeypatch.setattr(server, "LIVE_ON_WAKE", True)
    monkeypatch.setattr(server, "WAKE_VOICE_GATE", "auto")
    monkeypatch.setattr(server, "WAKE_PREROLL_SHADOW", True)
    server._multiroom._last_wake_time = 0.0
    server._multiroom._last_wake_device = ""


@pytest.fixture
async def atom(fake_relay, flags):
    """Atom 5.3 con cap live_wake, abilitato dal welcome; IDLE, relay chiuso."""
    c = server.DeviceConnection("AA:BB:CC", FakeDeviceWS())
    c.firmware_version = "5.3.0-ws"
    c.device_type = "AtomS3R"
    c.caps = {"aec_ref", "live_wake"}
    c.live_on_wake = True
    c._fire = False

    def process_audio(pcm):
        c.wakeword.processed += 1
        fire, c._fire = c._fire, False
        return fire
    c.wakeword.process_audio = process_audio
    c.wakeword.last_score = 0.912
    yield c
    await server._teardown_hub(c)
    await server._end_hub_session(c, send_ended=True, reopen_relay=False)


@pytest.fixture
async def live_hub(monkeypatch):
    hub = await FakeHub().start()
    monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "HUB_PORT", hub.port)
    monkeypatch.setattr(server, "AEC_BACKEND", "off")
    yield hub
    await hub.stop()


async def feed(conn, frames):
    for f in frames:
        await server._handle_binary(conn, f)


async def detect(conn, pre_frames=None):
    """Audio in IDLE e poi il frame che fa scattare la wake word."""
    if pre_frames is None:
        # stanza quieta e poi la "wake word"
        room = (np.random.default_rng(3).standard_normal(16000) * 30).astype(np.int16)
        pre_frames = opus_frames(np.concatenate((room, tone(300, 0.5))))
    pre = pre_frames
    await feed(conn, pre[:-1])
    conn._fire = True
    await server._handle_binary(conn, pre[-1])
    return pre


def relay(conn):
    return FakeOrchRelay.instances[-1]


def sent(conn, mtype):
    return [m for m in relay(conn).sent if m.get("type") == mtype]


async def to_orch_wait(conn, live_hub=None, ack="ok"):
    """Wake -> wake_detected{live} -> ack -> live_start all'orchestrator. Ritorna il req_id."""
    await detect(conn)
    wd = conn.ws.json[-1]
    assert wd["type"] == "wake_detected" and wd["live"] is True and wd["req_id"]
    assert conn.state == server.DeviceState.LIVE_PENDING
    await server._handle_text(conn, json.dumps({"type": "live_wake_ack", "req_id": wd["req_id"],
                                                "status": ack}))
    return wd["req_id"]


# --- pre-roll, ring, archivio ------------------------------------------------

def test_ring_keeps_the_last_seconds_in_order():
    ring = preroll.PcmRing(0.1)  # 1600 campioni
    for k in range(10):
        ring.append(np.full(320, k, np.int16), now=1.0 + k * 0.02)
    snap = np.frombuffer(ring.snapshot(), np.int16)
    assert len(snap) == 1600
    assert list(snap[::320]) == [5, 6, 7, 8, 9]
    # buco di oltre 0,5 s (il device e' stato fuori da IDLE): il vecchio si butta
    ring.append(np.full(320, 42, np.int16), now=5.0)
    assert list(np.frombuffer(ring.snapshot(), np.int16)) == [42] * 320


async def test_ring_fed_during_multiroom_cooldown(atom):
    server._multiroom.on_wake("OTHER")
    frames = opus_frames(tone(300, 0.2))
    await feed(atom, frames)
    assert atom.wakeword.processed == 0  # wake word ferma per il cooldown
    assert len(atom.preroll_ring) == len(frames) * 320


def test_clip_archive_names_and_bounds(tmp_path):
    arch = preroll.ClipArchive(str(tmp_path), max_files=3, max_bytes=0)
    pcm = tone(300, 0.1).tobytes()
    paths = [arch.save("AA:BB:CC", 0.8 + k / 100, pcm, when=1_700_000_000 + k) for k in range(5)]
    left = sorted(os.listdir(tmp_path))
    assert left == sorted(os.path.basename(p) for p in paths[-3:])
    assert left[0] == "20231114T221322000Z_AA-BB-CC_s0.820.wav"
    arch2 = preroll.ClipArchive(str(tmp_path), max_files=0, max_bytes=2 * os.path.getsize(paths[-1]))
    arch2.prune()
    assert len(os.listdir(tmp_path)) == 2


async def test_every_detection_is_archived(atom, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WAKE_CLIP_ARCHIVE", True)
    monkeypatch.setattr(server, "WAKE_CLIP_DIR", str(tmp_path))
    atom.caps = {"aec_ref"}  # classico
    await detect(atom)
    assert await wait_for(lambda: os.listdir(tmp_path))
    name = os.listdir(tmp_path)[0]
    assert "_AA-BB-CC_s0.912.wav" in name


# --- classico invariato e shadow --------------------------------------------

async def test_classic_wake_with_shadow_preroll(atom):
    atom.caps = {"aec_ref"}  # firmware senza live_wake
    pre = await detect(atom)
    assert atom.ws.json == [{"type": "wake_detected"}]
    assert atom.state == server.DeviceState.WAKING
    assert atom.wakeword.muted_for == 60.0
    [msg] = relay(atom).sent
    assert msg["type"] == "wake_preroll" and msg["gate"] is False and msg["score"] == 0.912
    assert msg["codec"] == "pcm_s16le" and msg["rate"] == 16000
    # il pre-roll finisce sul frame del rilevamento
    assert base64.b64decode(msg["b64"]) == decoded(pre)
    # poi l'audio_start del device va dopo il pre-roll, come oggi
    await server._handle_text(atom, json.dumps({"type": "audio_start"}))
    assert types(relay(atom).sent) == ["wake_preroll", "audio_start"]


async def test_classic_wake_without_shadow_is_todays(atom, monkeypatch):
    monkeypatch.setattr(server, "WAKE_PREROLL_SHADOW", False)
    atom.live_on_wake = False
    await detect(atom)
    assert atom.ws.json == [{"type": "wake_detected"}]
    assert relay(atom).sent == []


@pytest.mark.parametrize("why", ["switch", "device", "cap", "hub"])
async def test_live_needs_switch_flag_cap_and_hub(atom, live_hub, monkeypatch, why):
    if why == "switch":
        monkeypatch.setattr(server, "LIVE_ON_WAKE", False)
    elif why == "device":
        atom.live_on_wake = False
    elif why == "cap":
        atom.caps = {"aec_ref"}  # 5.2.1: scambierebbe wake_detected{live} per una wake normale
    else:
        monkeypatch.setattr(server, "HUB_PORT", 0)
    await detect(atom)
    assert atom.ws.json == [{"type": "wake_detected"}]
    assert atom.state == server.DeviceState.WAKING


async def test_welcome_flags_cached_and_forwarded(atom):
    await server._open_relay(atom)
    atom.live_on_wake = False
    await relay(atom).orch({"type": "welcome", "server_time": 1, "live_on_wake": True,
                            "wake_voice_gate": True})
    assert atom.live_on_wake is True and atom.wake_voice_gate is True
    assert atom.ws.json[-1]["type"] == "welcome"


# --- cancello a voce ---------------------------------------------------------

@pytest.fixture
def gate_only(atom, monkeypatch):
    monkeypatch.setattr(server, "WAKE_VOICE_GATE", "1")
    atom.live_on_wake = False
    return atom


async def test_gate_drop_is_silent(gate_only):
    atom = gate_only
    await detect(atom)
    assert atom.ws.json == []
    assert await wait_for(lambda: FakeOrchRelay.instances and sent(atom, "wake_preroll"))
    [pre] = sent(atom, "wake_preroll")
    assert pre["gate"] is True
    await relay(atom).orch({"type": "wake_gate", "req_id": pre["req_id"], "action": "drop",
                            "reason": "no_match", "best": 0.41})
    assert await wait_for(lambda: atom.wake_pending is None)
    assert atom.ws.json == []  # niente wake_detected: il device non se ne accorge
    assert atom.state == server.DeviceState.IDLE
    assert not server._multiroom.is_cooled_down("OTHER")


async def test_gate_pass_goes_classic(gate_only):
    atom = gate_only
    await detect(atom)
    assert await wait_for(lambda: FakeOrchRelay.instances and sent(atom, "wake_preroll"))
    req = sent(atom, "wake_preroll")[0]["req_id"]
    await relay(atom).orch({"type": "wake_gate", "req_id": req, "action": "keep"})
    assert await wait_for(lambda: atom.ws.json == [{"type": "wake_detected"}])
    assert atom.state == server.DeviceState.WAKING
    # un verdetto tardivo non arriva al device
    await relay(atom).orch({"type": "wake_gate", "req_id": req, "action": "drop"})
    assert atom.ws.json == [{"type": "wake_detected"}]


async def test_gate_without_verdict_fails_open(gate_only, monkeypatch):
    monkeypatch.setattr(server, "WAKE_VOICE_GATE_TIMEOUT_S", 0.05)
    await detect(gate_only)
    assert await wait_for(lambda: gate_only.ws.json == [{"type": "wake_detected"}])


async def test_gate_auto_follows_the_welcome(atom, monkeypatch):
    atom.live_on_wake = False
    atom.wake_voice_gate = True  # dal welcome
    monkeypatch.setattr(server, "WAKE_VOICE_GATE_TIMEOUT_S", 5)
    await detect(atom)
    assert atom.ws.json == [] and atom.state == server.DeviceState.LIVE_PENDING
    monkeypatch.setattr(server, "WAKE_VOICE_GATE", "0")
    assert server._voice_gate_on(atom) is False


async def test_tap_during_gate_wait_is_todays_tap(gate_only, monkeypatch):
    monkeypatch.setattr(server, "WAKE_VOICE_GATE_TIMEOUT_S", 5)
    atom = gate_only
    await detect(atom)
    assert await wait_for(lambda: FakeOrchRelay.instances and sent(atom, "wake_preroll"))
    await server._handle_text(atom, json.dumps({"type": "audio_start"}))
    assert atom.state == server.DeviceState.STREAMING and atom.wake_pending is None
    assert types(relay(atom).sent) == ["wake_preroll", "audio_start"]
    await asyncio.sleep(0.05)
    assert atom.ws.json == []


# --- Atom: wake -> live ------------------------------------------------------

async def test_wake_ack_ok_accept_and_command_buffer(atom, live_hub):
    req = await to_orch_wait(atom)
    [ls] = sent(atom, "live_start")
    assert ls["req_id"] == req and ls["trigger"] == "wake" and ls["score"] == 0.912
    assert ls["preroll"]["codec"] == "pcm_s16le" and ls["preroll"]["rate"] == 16000
    assert len(base64.b64decode(ls["preroll"]["b64"])) > 0
    # il comando detto subito dopo la wake word: decodificato e messo da parte
    n = atom.wakeword.processed
    cmd = opus_frames(tone(500, 0.8, amp=6000))
    await feed(atom, cmd)
    assert atom.wakeword.processed == n  # in LIVE_PENDING niente wake word
    buf = bytes(atom.wake_pending.buffer)
    assert len(buf) == len(cmd) * 640
    await relay(atom).orch(hub_live_start(req))
    await asyncio.wait({atom.hub_start_task})
    assert atom.hub_session is not None and atom.wake_pending is None
    offer = live_hub.offer
    assert offer["identified"] is True and offer["origin"] == "wake"
    assert offer["command"] is True
    assert offer["offer_extra"] == {"identified": True, "origin": "wake", "command": True}
    assert offer["call_id"] == "c0ffee02" and offer["profile"] == "hermes-marco"
    # live_session_active all'accept, poi il duplex di oggi (level mode)
    assert sent(atom, "live_session_active") == [
        {"type": "live_session_active", "call_id": "c0ffee02", "req_id": req}]
    assert atom.ws.json[-1] == {"type": "live_session_start", "duplex": True, "aec_ref": "level"}
    assert sent(atom, "live_session_end") == []
    # il buffer arriva al hub per primo, identico
    assert await wait_for(lambda: len(live_hub.audio) >= len(buf))
    assert bytes(live_hub.audio[:len(buf)]) == buf
    assert atom.wakeword.muted_for == 3600


async def test_preroll_excludes_what_comes_after_detection(atom, live_hub):
    await detect(atom)
    snap = bytes(atom.wake_pending.preroll)
    await feed(atom, opus_frames(tone(2000, 0.3)))  # chirp e resto: nel buffer, non nel pre-roll
    await server._handle_text(atom, json.dumps({"type": "live_wake_ack",
                                                "req_id": atom.wake_pending.req_id, "status": "ok"}))
    assert base64.b64decode(sent(atom, "live_start")[0]["preroll"]["b64"]) == snap


async def test_silent_buffer_is_no_command(atom, live_hub):
    req = await to_orch_wait(atom)
    await feed(atom, opus_frames(np.zeros(9600, np.int16)))
    await relay(atom).orch(hub_live_start(req))
    await asyncio.wait({atom.hub_start_task})
    assert live_hub.offer["command"] is False


def test_vad_skips_the_chirp():
    floor = (np.random.default_rng(1).standard_normal(16000) * 20).astype(np.int16)
    chirp = tone(2000, 0.25, amp=12000)
    silence = (np.random.default_rng(2).standard_normal(8000) * 20).astype(np.int16)
    buf = np.concatenate((chirp, silence)).tobytes()
    assert preroll.speech_ms(buf, floor.tobytes()) >= 240
    assert preroll.speech_ms(buf, floor.tobytes(), skip=(0, len(chirp) * 2)) == 0
    speech = np.concatenate((chirp, tone(300, 0.4, amp=3000))).tobytes()
    assert preroll.speech_ms(speech, floor.tobytes(), skip=(0, len(chirp) * 2)) >= 380


@pytest.mark.parametrize("status", ["dnd", "busy"])
async def test_ack_dnd_or_busy_aborts(atom, live_hub, status):
    req = await to_orch_wait(atom, ack=status)
    assert atom.ws.json[-1] == {"type": "live_abort", "req_id": req}
    assert atom.state == server.DeviceState.IDLE and atom.wake_pending is None
    assert sent(atom, "live_start") == []


async def test_no_ack_falls_back_to_classic(atom, live_hub, monkeypatch):
    monkeypatch.setattr(server, "LIVE_ACK_TIMEOUT_S", 0.05)
    await detect(atom)
    assert await wait_for(lambda: atom.ws.json[-1] == {"type": "wake_detected"})
    assert atom.state == server.DeviceState.WAKING and atom.wakeword.muted_for == 60.0
    assert sent(atom, "live_cancel") == []


async def test_declined_goes_classic_duplicate_goes_to_rest(atom, live_hub):
    req = await to_orch_wait(atom)
    await relay(atom).orch({"type": "live_declined", "req_id": req, "reason": "hub_down"})
    assert atom.ws.json[-1] == {"type": "wake_detected"}
    assert atom.state == server.DeviceState.WAKING
    # seconda wake, stavolta doppione: a riposo senza classico
    atom.state = server.DeviceState.IDLE
    req2 = await to_orch_wait(atom)
    assert req2 != req
    await relay(atom).orch({"type": "live_declined", "req_id": req2, "reason": "duplicate"})
    assert atom.ws.json[-1] == {"type": "live_abort", "req_id": req2}
    assert atom.state == server.DeviceState.IDLE
    # cancello a voce dentro live_start: anche questo a riposo, in silenzio
    req3 = await to_orch_wait(atom)
    await relay(atom).orch({"type": "live_declined", "req_id": req3, "reason": "voice_gate"})
    assert atom.ws.json[-1] == {"type": "live_abort", "req_id": req3}
    assert all(m.get("type") != "live_declined" for m in atom.ws.json)


async def test_orchestrator_timeout_cancels_then_classic(atom, live_hub, monkeypatch):
    monkeypatch.setattr(server, "LIVE_START_TIMEOUT_S", 0.05)
    req = await to_orch_wait(atom)
    assert await wait_for(lambda: atom.ws.json[-1] == {"type": "wake_detected"})
    assert sent(atom, "live_cancel") == [{"type": "live_cancel", "req_id": req}]
    # risposta tardiva con il req_id vecchio: ignorata, il hub non si apre
    await relay(atom).orch(hub_live_start(req))
    await asyncio.sleep(0.05)
    assert live_hub.connections == 0 and atom.hub_session is None


async def test_orchestrator_unreachable_is_classic_at_once(atom, live_hub, monkeypatch):
    async def broken(self):
        raise OSError("tailnet down")
    monkeypatch.setattr(FakeOrchRelay, "connect", broken)
    await detect(atom)
    assert types(atom.ws.json) == ["wake_detected", "wake_detected"]
    assert atom.ws.json[0]["live"] is True and atom.ws.json[1] == {"type": "wake_detected"}
    assert atom.wake_pending is None


async def test_start_with_another_req_id_is_ignored(atom, live_hub):
    req = await to_orch_wait(atom)
    await relay(atom).orch(hub_live_start("someone-else"))
    assert live_hub.connections == 0 and atom.wake_pending is not None
    assert atom.wake_pending.req_id == req


async def test_wake_during_live_pending_is_ignored(atom, live_hub):
    await detect(atom)
    n = atom.wakeword.processed
    atom._fire = True
    await feed(atom, opus_frames(tone(300, 0.1)))
    assert atom.wakeword.processed == n
    assert types(atom.ws.json) == ["wake_detected"]


async def test_tap_in_pending_cancels_before_audio_start(atom, live_hub):
    req = await to_orch_wait(atom)
    await server._handle_text(atom, json.dumps({"type": "audio_start"}))
    assert types(relay(atom).sent) == ["live_start", "live_cancel", "audio_start"]
    assert relay(atom).sent[1] == {"type": "live_cancel", "req_id": req}
    assert atom.state == server.DeviceState.STREAMING and atom.wake_pending is None


@pytest.mark.parametrize("msg_type", ["speaker_stop", "audio_end"])
async def test_double_tap_or_long_press_in_pending(atom, live_hub, msg_type):
    await to_orch_wait(atom)
    rel = relay(atom)
    await server._handle_text(atom, json.dumps({"type": msg_type}))
    assert types(rel.sent) == ["live_start", "live_cancel", msg_type]
    assert atom.state == server.DeviceState.IDLE and atom.relay is None


async def test_tap_during_hub_handshake_is_live_cancel(atom, monkeypatch):
    hub = await FakeHub(mode="silent").start()
    try:
        monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
        monkeypatch.setattr(server, "HUB_PORT", hub.port)
        req = await to_orch_wait(atom)
        await relay(atom).orch(hub_live_start(req))
        assert await wait_for(lambda: hub.offer is not None)
        await server._handle_text(atom, json.dumps({"type": "audio_start"}))
        assert await wait_for(hub.eof.is_set)
        assert types(relay(atom).sent) == ["live_start", "live_cancel", "audio_start"]
        assert atom.hub_start_task is None and atom.hub_session is None
    finally:
        await hub.stop()


async def test_hub_rejected_reports_detail_then_classic(atom, monkeypatch):
    hub = await FakeHub(mode="reject").start()
    try:
        monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
        monkeypatch.setattr(server, "HUB_PORT", hub.port)
        req = await to_orch_wait(atom)
        await relay(atom).orch(hub_live_start(req))
        await asyncio.wait({atom.hub_start_task})
        assert sent(atom, "live_session_end") == [
            {"type": "live_session_end", "call_id": "c0ffee02", "reason": "hub_rejected",
             "detail": "too many calls"}]
        assert atom.ws.json[-1] == {"type": "wake_detected"}
        assert atom.state == server.DeviceState.WAKING and atom.wakeword.muted_for == 60.0
        assert sent(atom, "live_session_active") == []
    finally:
        await hub.stop()


async def test_origin_accept_timeout_is_the_live_one(atom, monkeypatch):
    hub = await FakeHub(mode="silent").start()
    try:
        monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
        monkeypatch.setattr(server, "HUB_PORT", hub.port)
        monkeypatch.setattr(server.HubRelay, "ACCEPT_TIMEOUT_S", 30)
        monkeypatch.setattr(server, "LIVE_ACCEPT_TIMEOUT_S", 0.2)
        req = await to_orch_wait(atom)
        await relay(atom).orch(hub_live_start(req))
        await asyncio.wait_for(asyncio.wait({atom.hub_start_task}), 3)
        assert sent(atom, "live_session_end")[0]["reason"] == "hub_unreachable"
        assert "detail" not in sent(atom, "live_session_end")[0]
        assert atom.ws.json[-1] == {"type": "wake_detected"}
    finally:
        await hub.stop()


async def test_orchestrator_end_during_pending_aborts(atom, live_hub):
    req = await to_orch_wait(atom)
    await relay(atom).orch({"type": "live_session_end"})
    assert atom.ws.json[-1] == {"type": "live_abort", "req_id": req}
    assert atom.state == server.DeviceState.IDLE


async def test_firmware_safety_timeout_in_pending(atom, live_hub):
    req = await to_orch_wait(atom)
    await server._handle_text(atom, json.dumps({"type": "live_session_end", "reason": "error"}))
    assert sent(atom, "live_cancel") == [{"type": "live_cancel", "req_id": req}]
    assert sent(atom, "live_session_end") == []
    assert atom.state == server.DeviceState.IDLE and atom.wake_pending is None


async def test_disconnect_in_pending_sends_live_cancel(atom, live_hub):
    req = await to_orch_wait(atom)
    await server._teardown_hub(atom)
    assert sent(atom, "live_cancel") == [{"type": "live_cancel", "req_id": req}]
    assert sent(atom, "live_session_end") == []


# --- richieste dal device: tap e NabuVoice ------------------------------------

async def test_atom_tap_live_request(atom, live_hub):
    await server._handle_text(atom, json.dumps({"type": "live_start", "req_id": REQ, "trigger": "tap"}))
    assert atom.state == server.DeviceState.LIVE_PENDING
    [ls] = sent(atom, "live_start")
    assert ls == {"type": "live_start", "req_id": REQ, "trigger": "tap"}
    quiet = (np.random.default_rng(4).standard_normal(4800) * 30).astype(np.int16)
    cmd = opus_frames(np.concatenate((quiet, tone(500, 0.6, amp=6000))))
    await feed(atom, cmd)
    buf = bytes(atom.wake_pending.buffer)
    await relay(atom).orch(hub_live_start(REQ, origin="tap", offer_extra={"origin": "tap", "identified": True}))
    await asyncio.wait({atom.hub_start_task})
    assert live_hub.offer["origin"] == "tap" and live_hub.offer["command"] is True
    assert await wait_for(lambda: bytes(live_hub.audio[:len(buf)]) == buf)


async def test_device_tap_declined_returns_live_declined(atom, live_hub):
    await server._handle_text(atom, json.dumps({"type": "live_start", "req_id": REQ, "trigger": "tap"}))
    await relay(atom).orch({"type": "live_declined", "req_id": REQ, "reason": "busy"})
    assert atom.ws.json[-1] == {"type": "live_declined", "req_id": REQ, "reason": "busy"}
    assert atom.state == server.DeviceState.IDLE


async def test_device_live_start_disabled(atom, live_hub, monkeypatch):
    monkeypatch.setattr(server, "LIVE_ON_WAKE", False)
    await server._handle_text(atom, json.dumps({"type": "live_start", "req_id": REQ, "trigger": "tap"}))
    assert atom.ws.json == [{"type": "live_declined", "req_id": REQ, "reason": "disabled"}]
    assert atom.state == server.DeviceState.IDLE and FakeOrchRelay.instances == []


@pytest.fixture
async def nabu(atom, monkeypatch):
    atom.firmware_version = "voicepe-2.0"
    atom.device_type = "NabuVoice"
    atom.caps = {"duplex", "aec_onboard", "live_wake"}

    async def no_server_aec(*a, **k):
        raise AssertionError("aec_onboard: niente AEC lato server")
    monkeypatch.setattr(server, "_create_aec", no_server_aec)
    return atom


async def test_nabu_live_start_recomposed_and_pcm_buffer(nabu, live_hub):
    room = (np.random.default_rng(5).standard_normal(16000) * 10).astype(np.int16)
    pre = np.concatenate((room, tone(400, 0.5))).tobytes()
    await server._handle_text(nabu, json.dumps({"type": "live_start", "req_id": REQ,
                                                "preroll_b64": base64.b64encode(pre).decode()}))
    [ls] = sent(nabu, "live_start")
    assert ls["trigger"] == "wake" and ls["req_id"] == REQ
    assert base64.b64decode(ls["preroll"]["b64"]) == pre
    # dopo il JSON solo PCM, anche di lunghezza dispari: buffer del comando, bit per bit
    cmd = tone(600, 0.5, amp=5000).tobytes()
    chunks = [cmd[i:i + 641] for i in range(0, len(cmd), 641)]
    await feed(nabu, chunks)
    assert nabu.wakeword.processed == 0
    await relay(nabu).orch(hub_live_start(REQ))
    await asyncio.wait({nabu.hub_start_task})
    assert nabu.ws.json[-1] == {"type": "live_session_start", "duplex": True}
    assert await wait_for(lambda: len(live_hub.audio) >= len(cmd))
    assert bytes(live_hub.audio[:len(cmd)]) == cmd
    assert live_hub.offer["command"] is True


async def test_nabu_declined_goes_classic_and_drops_pcm_tail(nabu, live_hub):
    await server._handle_text(nabu, json.dumps({"type": "live_start", "req_id": REQ,
                                                "preroll_b64": base64.b64encode(b"\0\0" * 100).decode()}))
    await relay(nabu).orch({"type": "live_declined", "req_id": REQ, "reason": "hub_off"})
    assert nabu.ws.json[-1] == {"type": "live_declined", "req_id": REQ, "reason": "hub_off"}
    assert nabu.state == server.DeviceState.IDLE and nabu.pcm_tail_until > 0
    await server._handle_binary(nabu, b"\x01\x02" * 320)  # PCM ancora in volo
    assert nabu.wakeword.processed == 0


async def test_nabu_server_side_wake_stays_classic(nabu, live_hub):
    await detect(nabu)
    assert nabu.ws.json == [{"type": "wake_detected"}]


# --- annunci e offer_extra ----------------------------------------------------

async def test_announce_goes_to_the_hub_not_to_the_device(atom, live_hub):
    req = await to_orch_wait(atom)
    await relay(atom).orch(hub_live_start(req))
    await asyncio.wait({atom.hub_start_task})
    n = len(atom.ws.json)
    await relay(atom).orch({"type": "announce", "call_id": "c0ffee02", "text": "Il bucato e' finito",
                            "urgent": True})
    assert await wait_for(lambda: any(m.get("type") == "announce" for m in live_hub.json))
    assert [m for m in live_hub.json if m.get("type") == "announce"] == [
        {"type": "announce", "call_id": "c0ffee02", "text": "Il bucato e' finito", "urgent": True}]
    assert len(atom.ws.json) == n or all(m.get("type") == "vu" for m in atom.ws.json[n:])


async def test_announce_without_hub_session_is_dropped(atom):
    await server._open_relay(atom)
    await relay(atom).orch({"type": "announce", "call_id": "x", "text": "t", "urgent": False})
    assert atom.ws.json == []


async def test_offer_extra_copied_on_todays_path(conn, hub_on):
    # live_session_start senza req_id ("avvia una sessione live"): offer_extra copiato, niente
    # command; live_session_active senza req_id (il peer inoltra gli annunci, 19.12 D6)
    from conftest import hub_start_msg, orch_hub_start
    await orch_hub_start(conn, hub_start_msg(offer_extra={"identified": False, "type": "nope"}))
    assert hub_on.offer["identified"] is False and hub_on.offer["type"] == "offer"
    assert hub_on.offer["offer_extra"] == {"identified": False, "type": "nope"}
    assert "command" not in hub_on.offer and "origin" not in hub_on.offer
    assert [m for m in conn.relay.sent if m.get("type") == "live_session_active"] == [
        {"type": "live_session_active", "call_id": "c0ffee01"}]


# --- revisione: relay condiviso, toggle live_on_wake, rifiuti della NabuVoice, offer ------

async def test_tap_while_the_gate_is_opening_the_relay(gate_only, monkeypatch):
    """Il tap annulla il task del cancello mentre apre il relay: l'apertura condivisa deve
    continuare per l'audio_start, non tirare un CancelledError nel loop WS del device."""
    monkeypatch.setattr(server, "WAKE_VOICE_GATE_TIMEOUT_S", 5)
    atom = gate_only

    async def slow(self):
        await asyncio.sleep(0.1)
        self.connected = True
    monkeypatch.setattr(FakeOrchRelay, "connect", slow)
    await detect(atom)
    await asyncio.sleep(0.01)
    assert atom.relay_opening is not None and not atom.relay_opening.done()
    await server._handle_text(atom, json.dumps({"type": "audio_start"}))
    assert atom.state == server.DeviceState.STREAMING and atom.wake_pending is None
    assert len(FakeOrchRelay.instances) == 1  # un solo relay
    assert types(relay(atom).sent) == ["audio_start"]
    assert atom.relay_opening is None


async def test_relay_opened_after_disconnect_is_closed(atom, monkeypatch):
    async def slow(self):
        await asyncio.sleep(0.05)
        self.connected = True
    monkeypatch.setattr(FakeOrchRelay, "connect", slow)
    task = asyncio.ensure_future(server._open_relay(atom))
    await asyncio.sleep(0.01)
    task.cancel()  # chi l'ha chiesta e' sparito...
    atom.close()   # ...e anche il device
    await asyncio.sleep(0.1)
    assert FakeOrchRelay.instances[-1].connected is False and atom.relay is None


async def test_live_on_wake_toggle_updates_the_cache(atom):
    await server._open_relay(atom)
    await relay(atom).orch({"type": "live_on_wake", "live_on_wake": False})
    assert atom.live_on_wake is False
    await detect(atom)
    assert atom.ws.json[-1] == {"type": "wake_detected"}  # classico, niente round trip


async def test_nabu_disabled_decline_drops_the_pcm_that_follows(nabu, monkeypatch):
    monkeypatch.setattr(server, "LIVE_ON_WAKE", False)
    await server._handle_text(nabu, json.dumps({"type": "live_start", "req_id": REQ,
                                                "preroll_b64": base64.b64encode(b"\0\0" * 10).decode()}))
    assert nabu.ws.json == [{"type": "live_declined", "req_id": REQ, "reason": "disabled"}]
    assert nabu.pcm_tail_until > 0
    await server._handle_binary(nabu, b"\x01\x02" * 320)
    assert nabu.wakeword.processed == 0


async def test_offer_extra_does_not_override_the_offer(conn, hub_on):
    from conftest import hub_start_msg, orch_hub_start
    await orch_hub_start(conn, hub_start_msg(offer_extra={"identified": True, "profile": "hermes-ada",
                                                          "name": "Ada"}))
    assert hub_on.offer["profile"] == "hermes-marco" and hub_on.offer["name"] == "Marco"
    assert hub_on.offer["offer_extra"]["profile"] == "hermes-ada"  # copiato com'e'
    assert hub_on.offer["identified"] is True
