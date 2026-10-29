"""Sessione live hub sul wakeword-server: finto hub ipc, finto orchestrator, finto Atom."""
import asyncio
import json
import socket

import numpy as np
import opuslib
import pytest
import uvicorn
import websockets

import livesession
import server
from conftest import FakeHub, hub_start_msg, orch_hub_start, wait_for


def tone(freq, seconds, rate, amp=8000):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def peak_hz(pcm, rate):
    spec = np.abs(np.fft.rfft(pcm.astype(np.float64) * np.hanning(len(pcm))))
    return np.argmax(spec) * rate / len(pcm)


def opus_frames(pcm16k):
    enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    return [enc.encode(pcm16k[i:i + 320].tobytes(), 320) for i in range(0, len(pcm16k) - 319, 320)]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def start_session(conn, _hub, **over):
    await orch_hub_start(conn, **over)
    assert conn.hub_session is not None
    return conn.hub_session


# --- apertura -------------------------------------------------------------

async def test_offer_accept_enters_duplex(conn, hub_on):
    hub = hub_on
    await start_session(conn, hub)
    assert hub.offer == {
        "type": "offer", "call_id": "c0ffee01", "profile": "hermes-marco", "name": "Marco",
        "voice": "Charon", "location_id": "napoli", "room": "studio", "deliver": ["orchestrator"],
        # VAD meno nervoso: il residuo d'eco a inizio frase non deve sembrare l'utente
        "vad": {"startOfSpeechSensitivity": "START_SENSITIVITY_LOW", "prefixPaddingMs": 300},
    }
    assert conn.ws.json == [{"type": "live_session_start", "duplex": True}]
    assert conn.live_session is True
    assert conn.wakeword.muted_for == 3600
    assert conn.relay.ends() == []


async def test_empty_hub_host_is_feature_off(conn, hub, monkeypatch):
    # D5: HUB_HOST vuoto spegne la funzione anche con HUB_PORT valorizzato;
    # l'host del messaggio dell'orchestrator non viene mai usato
    monkeypatch.setattr(server, "HUB_HOST", "")
    monkeypatch.setattr(server, "HUB_PORT", hub.port)
    monkeypatch.setattr(server, "AEC_BACKEND", "off")
    await orch_hub_start(conn, hub={"host": "127.0.0.1", "port": hub.port})
    assert hub.connections == 0
    assert conn.hub_session is None
    assert conn.relay.ends() == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "hub_unreachable"}]


async def test_env_host_wins_over_message(conn, hub_on):
    # 192.0.2.1 (TEST-NET) non risponde: se venisse usato il test andrebbe in timeout
    await start_session(conn, hub_on, hub={"host": "192.0.2.1", "port": 9})
    assert hub_on.connections == 1


async def test_rejected_only_reports_and_orchestrator_converts(conn, monkeypatch):
    # D3: il peer riferisce e basta, niente modalita' live di oggi decisa da solo
    hub = await FakeHub(mode="reject").start()
    try:
        monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
        monkeypatch.setattr(server, "HUB_PORT", hub.port)
        await orch_hub_start(conn)
        assert conn.hub_session is None and conn.hub_start_task is None
        assert conn.relay.ends() == [
            {"type": "live_session_end", "call_id": "c0ffee01", "reason": "hub_rejected"}]
        assert conn.ws.json == []
        assert conn.live_session is False
        assert conn.state == server.DeviceState.IDLE
        assert conn.wakeword.muted_for == 5
        # l'orchestrator converte sul posto: il live_session_start di oggi fa il resto
        await conn.relay.orch({"type": "live_session_start"})
        await conn.relay.orch(b"greeting-opus")
        await conn.relay.orch({"type": "tts_done"})
        await conn.relay.orch({"type": "trigger_listen"})
        assert conn.ws.types() == ["live_session_start", "tts_done", "trigger_listen"]
        assert conn.ws.binary == [b"greeting-opus"]
        assert conn.live_session is True and conn.wakeword.muted_for == 3600
        assert conn.state == server.DeviceState.WAKING
        assert conn.relay.connected
    finally:
        await hub.stop()


async def test_hub_closing_before_accept_is_rejected(conn, monkeypatch):
    # D4: chiusura senza frame prima dell'accept (call_id duplicato) = hub_rejected
    hub = await FakeHub(mode="close").start()
    try:
        monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
        monkeypatch.setattr(server, "HUB_PORT", hub.port)
        await orch_hub_start(conn)
        assert conn.relay.ends()[0]["reason"] == "hub_rejected"
    finally:
        await hub.stop()


async def test_truncated_frame_during_handshake_is_unreachable(conn, monkeypatch):
    # IncompleteReadError a meta' frame: niente eccezione fuori, orchestrator avvisato
    hub = await FakeHub(mode="truncated").start()
    try:
        monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
        monkeypatch.setattr(server, "HUB_PORT", hub.port)
        await orch_hub_start(conn)
        assert conn.hub_session is None and conn.hub_start_task is None
        assert conn.relay.ends() == [
            {"type": "live_session_end", "call_id": "c0ffee01", "reason": "hub_unreachable"}]
        assert conn.relay.connected
    finally:
        await hub.stop()


async def test_bad_port_is_unreachable(conn, monkeypatch):
    monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "HUB_PORT", 70000)  # OverflowError in connect
    await orch_hub_start(conn)
    assert conn.relay.ends()[0]["reason"] == "hub_unreachable"


async def test_unexpected_error_still_reports(conn, hub_on, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("encoder")
    monkeypatch.setattr(server, "HubLiveSession", boom)
    await orch_hub_start(conn)
    assert conn.hub_session is None and conn.hub_start_task is None
    assert conn.relay.ends()[0]["reason"] == "hub_unreachable"
    assert await wait_for(hub_on.eof.is_set)
    assert hub_on.json == [{"type": "ended", "call_id": "c0ffee01"}]


async def test_fallback_then_orchestrator_ends_returns_idle(conn, monkeypatch):
    # D1+D3: l'orchestrator non ripiega e chiude con live_session_end + tts_done
    monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "HUB_PORT", free_port())
    await orch_hub_start(conn)
    assert conn.relay.ends()[0]["reason"] == "hub_unreachable"
    await conn.relay.orch({"type": "live_session_end"})
    await conn.relay.orch({"type": "tts_done"})
    assert conn.ws.types() == ["live_session_end", "tts_done"]
    assert conn.state == server.DeviceState.IDLE
    assert conn.live_session is False and conn.wakeword.muted_for == 0
    assert conn.relay is None  # turno chiuso come oggi, relay chiuso


async def test_unreachable(conn, monkeypatch):
    monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "HUB_PORT", free_port())
    await orch_hub_start(conn)
    assert conn.hub_session is None
    assert conn.relay.ends() == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "hub_unreachable"}]


async def test_silent_hub_times_out_as_unreachable(conn, monkeypatch):
    hub = await FakeHub(mode="silent").start()
    try:
        monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
        monkeypatch.setattr(server, "HUB_PORT", hub.port)
        monkeypatch.setattr(server.HubRelay, "ACCEPT_TIMEOUT_S", 0.2)
        await orch_hub_start(conn)
        assert conn.relay.ends()[0]["reason"] == "hub_unreachable"
        assert await wait_for(hub.eof.is_set)  # socket chiuso, non appeso
    finally:
        await hub.stop()


async def test_feature_off_never_connects(conn, hub):
    # HUB_PORT=0 (default) anche se il messaggio porta un indirizzo hub
    await orch_hub_start(conn, hub={"host": "127.0.0.1", "port": hub.port})
    assert hub.connections == 0
    assert conn.hub_session is None
    assert conn.relay.ends()[0]["reason"] == "hub_unreachable"


@pytest.mark.parametrize("missing", ["call_id", "profile", "name"])
async def test_missing_identity_is_error(conn, hub_on, missing):
    await orch_hub_start(conn, **{missing: ""})
    assert hub_on.connections == 0
    assert conn.hub_session is None
    end = conn.relay.ends()[0]
    assert end["reason"] == "error"
    assert end["call_id"] == ("" if missing == "call_id" else "c0ffee01")
    assert conn.ws.json == []
    assert conn.live_session is False


# --- comportamento di oggi invariato ----------------------------------------

async def test_old_live_session_unchanged(conn, hub_on, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HubLiveSession must not be created on the old path")
    monkeypatch.setattr(server, "HubLiveSession", boom)
    await conn.relay.orch({"type": "live_session_start"})
    assert conn.ws.json == [{"type": "live_session_start"}]
    assert conn.live_session is True and conn.hub_session is None
    await conn.relay.orch(b"tts-opus")
    assert conn.ws.binary == [b"tts-opus"]
    # mic in BUSY scartato come oggi
    for f in opus_frames(tone(440, 0.1, 16000)):
        await server._handle_binary(conn, f)
    assert conn.relay.sent_bin == [] and hub_on.connections == 0
    await conn.relay.orch({"type": "live_session_end"})
    assert conn.ws.json[-1] == {"type": "live_session_end"}
    assert conn.live_session is False
    assert conn.relay.ends() == []


# --- media ----------------------------------------------------------------

async def test_uplink_decoded_to_hub_in_busy_with_vu(conn, hub_on, monkeypatch):
    monkeypatch.setattr(livesession, "_VU_INTERVAL_S", 0.0)
    hub = hub_on
    await start_session(conn, hub)
    assert conn.state == server.DeviceState.BUSY
    frames = opus_frames(tone(440, 0.5, 16000))
    for f in frames:
        await server._handle_binary(conn, f)
    assert await wait_for(lambda: len(hub.audio) >= len(frames) * 640)
    assert len(hub.audio) == len(frames) * 640
    assert hub.audio_call_ids == {"c0ffee01"}
    pcm = np.frombuffer(bytes(hub.audio), dtype=np.int16)
    assert abs(peak_hz(pcm[1600:], 16000) - 440) < 20
    vus = [m for m in conn.ws.json if m.get("type") == "vu"]
    assert vus and all(0 <= m["mic"] <= 255 for m in vus)
    assert max(m["mic"] for m in vus) > 150  # tono a -12 dBFS
    assert conn.relay.sent_bin == []  # niente mic all'orchestrator


async def test_uplink_with_real_aec_backend(conn, hub_on, monkeypatch):
    pytest.importorskip("livekit.rtc")
    monkeypatch.setattr(server, "AEC_BACKEND", "webrtc")
    hub = hub_on
    session = await start_session(conn, hub)
    assert session.aec.backend_name == "webrtc"
    frames = opus_frames(tone(440, 0.5, 16000))
    for f in frames:
        await server._handle_binary(conn, f)
    assert await wait_for(lambda: len(hub.audio) >= len(frames) * 640)


def aligned_packets(mic, ref, swap=False):
    """Pacchetti come il firmware con cap aec_ref: 0xA5 0x01 seq, coppie [pari, dispari]."""
    out = []
    for k, i in enumerate(range(0, len(mic) - 319, 320)):
        pairs = np.stack((ref[i:i + 320], mic[i:i + 320]) if swap else (mic[i:i + 320], ref[i:i + 320]), axis=1)
        out.append(bytes([0xA5, 0x01, k & 0xFF, k >> 8]) + pairs.astype("<i2").tobytes())
    return out


async def test_aec_ref_cap_asks_the_device_for_the_reference(conn, hub_on, monkeypatch):
    monkeypatch.setattr(server, "HUB_ATOM_MIC_GAIN_DB", "18")
    monkeypatch.setattr(server, "HUB_AEC_REF_MODE", "pcm")
    conn.caps = {"aec_ref"}
    await start_session(conn, hub_on)
    assert conn.ws.json == [{"type": "live_session_start", "duplex": True, "aec_ref": True,
                             "mic_gain_db": 18}]
    assert conn.hub_session.aec._gain == pytest.approx(10 ** (12 / 20))  # 30 - 18 dB ridati dopo l'AEC


async def test_aligned_uplink_reaches_the_hub_with_the_mic_slot(conn, hub_on, monkeypatch):
    hub = hub_on
    conn.caps = {"aec_ref"}
    session = await start_session(conn, hub)
    rng = np.random.default_rng(3)
    mic = tone(440, 0.6, 16000) + (rng.standard_normal(9600) * 50).astype(np.int16)
    ref = np.zeros_like(mic)  # Atom muto: il loopback e' zero digitale
    packets = aligned_packets(mic, ref, swap=True)  # slot scambiati: il voto deve accorgersene
    for p in packets:
        await server._handle_binary(conn, p)
    assert await wait_for(lambda: len(hub.audio) >= len(packets) * 640)
    assert session.aec.aligned and session._slots.locked and session._slots.ref == 0
    pcm = np.frombuffer(bytes(hub.audio), dtype=np.int16)
    assert abs(peak_hz(pcm[3200:], 16000) - 440) < 20  # e' arrivato il mic, non il loopback
    assert session._seq_lost == 0


async def test_muted_packets_drop_the_reference_and_do_not_vote(conn, hub_on):
    conn.caps = {"aec_ref"}
    session = await start_session(conn, hub_on)
    seen = []
    session.aec.process_aligned = lambda near, far: seen.append(far) or near
    mic = (np.random.default_rng(6).standard_normal(320) * 50).astype(np.int16)
    loud = tone(300, 0.02, 16000)
    pkt = bytes([0xA5, 0x81, 0, 0]) + np.stack((mic, loud), axis=1).astype("<i2").tobytes()
    for _ in range(20):
        await server._handle_binary(conn, pkt)
    assert session._slots.votes == [0, 0]
    assert all(f == bytes(640) for f in seen)


def level_packets(mic, ref_rms, muted=False):
    """Pacchetti come il firmware con aec_ref "level": 0xA5 0x03 seq, 2 RMS u16, mic Opus."""
    out = []
    for k, f in enumerate(opus_frames(mic)):
        r = ref_rms[k] if k < len(ref_rms) else 0
        out.append(bytes([0xA5, 0x83 if muted else 0x03, k & 0xFF, k >> 8, r & 0xFF, r >> 8, r & 0xFF, r >> 8]) + f)
    return out


async def test_aec_ref_cap_gets_the_level_mode_by_default(conn, hub_on):
    conn.caps = {"aec_ref"}
    await start_session(conn, hub_on)
    assert conn.ws.json == [{"type": "live_session_start", "duplex": True, "aec_ref": "level"}]
    assert conn.hub_session.aec._gain == 1.0


async def test_level_uplink_is_silenced_while_the_speaker_plays(conn, hub_on):
    hub = hub_on
    conn.caps = {"aec_ref"}
    session = await start_session(conn, hub)
    mic = tone(440, 1.0, 16000)
    # primi 25 pacchetti (0,5 s) con lo speaker a ~ -12 dBFS, poi muto
    packets = level_packets(mic, [8000] * 25)
    for p in packets:
        await server._handle_binary(conn, p)
    assert await wait_for(lambda: len(hub.audio) >= len(packets) * 640)
    pcm = np.frombuffer(bytes(hub.audio), dtype=np.int16)
    assert not pcm[:8000].any()                  # Jarvis parla: a Gemini solo silenzio
    assert not pcm[8000:8000 + 4000].any()       # coda di 300 ms dopo l'ultimo audio
    assert abs(peak_hz(pcm[13000:], 16000) - 440) < 20  # poi il mic passa
    assert session._seq_lost == 0 and session._level_packets == len(packets)


async def test_level_uplink_muted_flag_opens_the_gate(conn, hub_on):
    hub = hub_on
    conn.caps = {"aec_ref"}
    await start_session(conn, hub)
    packets = level_packets(tone(440, 0.4, 16000), [8000] * 20, muted=True)
    for p in packets:
        await server._handle_binary(conn, p)
    assert await wait_for(lambda: len(hub.audio) >= len(packets) * 640)
    pcm = np.frombuffer(bytes(hub.audio), dtype=np.int16)
    assert np.abs(pcm[1600:]).max() > 1000  # DAC in mute: lo speaker tace, il mic passa


async def test_device_interrupt_clears_and_tells_the_hub(conn, hub_on):
    hub = hub_on
    session = await start_session(conn, hub)
    hub.send_audio(tone(1000, 2.0, 24000).tobytes())
    assert await wait_for(lambda: session.queued_frames > 10)
    await server._handle_text(conn, json.dumps({"type": "live_interrupt"}))
    assert session.queued_frames == 0
    assert conn.ws.json[-1] == {"type": "audio_clear"}
    assert await wait_for(lambda: any(m.get("type") == "interrupt" and m.get("call_id") == "c0ffee01"
                                      for m in hub.json))
    assert conn.hub_session is session  # la sessione continua
    assert conn.relay.ends() == []


@pytest.mark.parametrize("fw,dtype,caps", [("5.0.0-ws", "AtomS3R", set()),
                                            ("1.0.0-nabuvoice", "NabuVoice", set())])
async def test_firmware_without_duplex_falls_back(conn, hub_on, fw, dtype, caps):
    conn.firmware_version, conn.device_type, conn.caps = fw, dtype, caps
    await orch_hub_start(conn)
    assert conn.hub_session is None and hub_on.connections == 0
    assert [m.get("reason") for m in conn.relay.ends()] == ["error"]


async def test_onboard_aec_device_keeps_gemini_default_vad(conn, hub_on):
    conn.firmware_version, conn.device_type = "1.1.0-nabuvoice", "NabuVoice"
    conn.caps = {"duplex", "pcm_uplink", "aec_onboard"}
    await start_session(conn, hub_on)
    assert hub_on.offer["vad"] == {"prefixPaddingMs": 300}


async def test_onboard_aec_pcm_gets_the_fixed_gain_and_never_clips(conn, hub_on, monkeypatch):
    monkeypatch.setattr(server, "HUB_ONBOARD_GAIN_DB", 24.0)
    conn.firmware_version, conn.device_type = "1.1.0-nabuvoice", "NabuVoice"
    conn.caps = {"duplex", "pcm_uplink", "aec_onboard"}
    await start_session(conn, hub_on)
    quiet = tone(440, 0.2, 16000, amp=200)       # voce lontana ~ -44 dBFS
    loud = tone(440, 0.2, 16000, amp=8000)       # voce vicina: col guadagno sarebbe oltre il fondo scala
    await server._handle_binary(conn, quiet.tobytes())
    await server._handle_binary(conn, loud.tobytes())
    assert await wait_for(lambda: len(hub_on.audio) >= 2 * len(quiet) * 2)
    pcm = np.frombuffer(bytes(hub_on.audio), dtype=np.int16).astype(float)
    ratio = np.abs(pcm[:3200]).max() / np.abs(quiet).max()
    assert abs(20 * np.log10(ratio) - 24) < 0.5
    assert np.abs(pcm[3200:]).max() < 32767


async def test_downlink_resampled_paced_opus(conn, hub_on):
    hub = hub_on
    await start_session(conn, hub)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    hub.send_audio(tone(1000, 1.0, 24000).tobytes())  # a raffica, come Gemini
    assert await wait_for(lambda: len(conn.ws.binary) >= 50, timeout=4)
    elapsed = loop.time() - t0
    assert len(conn.ws.binary) == 50
    assert elapsed > 0.75  # in tempo reale (1 s meno il pre-buffer), non a raffica
    dec = opuslib.Decoder(16000, 1)
    pcm = np.frombuffer(b"".join(dec.decode(f, 320) for f in conn.ws.binary), dtype=np.int16)
    assert len(pcm) == 16000
    assert abs(peak_hz(pcm[1600:], 16000) - 1000) < 20
    rms = np.sqrt(np.mean(pcm[1600:].astype(np.float64) ** 2))
    assert 3500 < rms < 7000  # ampiezza 8000 -> rms ~5657


async def test_orchestrator_binary_not_forwarded_in_hub_session(conn, hub_on):
    await start_session(conn, hub_on)
    await conn.relay.orch(b"tts-from-orchestrator")
    assert conn.ws.binary == []


async def test_clear_drops_queued_downlink_but_keeps_the_aec_reference(conn, hub_on, monkeypatch):
    # speex (se c'e') perche' il passthrough non tiene render in coda
    monkeypatch.setattr(server, "AEC_BACKEND", "speex")
    hub = hub_on
    session = await start_session(conn, hub)
    hub.send_audio(tone(1000, 3.0, 24000).tobytes())
    assert await wait_for(lambda: len(conn.ws.binary) >= 10)
    if session.aec.backend_name != "off":
        assert session.aec.render_backlog_bytes > 0
    hub.send_json({"type": "clear", "call_id": "c0ffee01"})
    backlog = session.aec.render_backlog_bytes
    assert await wait_for(lambda: "audio_clear" in conn.ws.types())
    assert session.queued_frames == 0
    # L'audio gia' consegnato all'Atom puo' ancora suonare: il suo riferimento resta all'AEC
    if session.aec.backend_name != "off":
        assert session.aec.render_backlog_bytes > 0 or backlog == 0
    sent = len(conn.ws.binary)
    await asyncio.sleep(0.2)
    assert len(conn.ws.binary) <= sent + 1  # al massimo il frame gia' in uscita
    assert conn.hub_session is session  # il barge-in non chiude la sessione


# --- chiusura ---------------------------------------------------------------

async def test_hub_hangup(conn, hub_on):
    hub = hub_on
    await start_session(conn, hub)
    hub.send_json({"type": "hangup", "call_id": "c0ffee01"})
    assert await wait_for(lambda: conn.hub_session is None)
    assert conn.ws.json[-1] == {"type": "live_session_end"}
    assert conn.relay.ends() == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "hub_hangup"}]
    assert await wait_for(hub.eof.is_set)
    assert conn.state == server.DeviceState.IDLE and conn.live_session is False
    assert conn.wakeword.muted_for == 0
    assert {"type": "ended", "call_id": "c0ffee01"} not in hub.json


async def test_hub_drops_connection(conn, hub_on):
    hub = hub_on
    await start_session(conn, hub)
    hub.close()
    assert await wait_for(lambda: conn.hub_session is None)
    assert conn.ws.json[-1] == {"type": "live_session_end"}
    assert conn.relay.ends()[0]["reason"] == "error"


async def test_button_end(conn, hub_on):
    hub = hub_on
    await start_session(conn, hub)
    await server._handle_text(conn, json.dumps({"type": "live_session_end", "reason": "button"}))
    assert conn.hub_session is None
    assert await wait_for(hub.eof.is_set)
    assert hub.json == [{"type": "ended", "call_id": "c0ffee01"}]
    assert conn.relay.ends() == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "button"}]
    assert conn.relay.connected  # il canale di controllo resta aperto
    assert conn.state == server.DeviceState.IDLE


async def test_orchestrator_end(conn, hub_on):
    hub = hub_on
    await start_session(conn, hub)
    await conn.relay.orch({"type": "live_session_end"})
    assert conn.hub_session is None
    assert await wait_for(hub.eof.is_set)
    assert hub.json == [{"type": "ended", "call_id": "c0ffee01"}]
    assert conn.ws.json[-1] == {"type": "live_session_end"}
    assert conn.relay.ends() == []  # l'orchestrator lo sa gia'
    assert conn.live_session is False


async def test_audio_start_in_hub_session_is_desync(conn, hub_on):
    # D2 lato wakeword-server: un Atom che apre un turno non e' piu' in duplex. Il hub si
    # chiude in silenzio verso l'orchestrator e il turno prosegue come oggi
    hub = hub_on
    await start_session(conn, hub)
    await server._handle_text(conn, json.dumps({"type": "audio_start"}))
    assert conn.hub_session is None
    assert await wait_for(hub.eof.is_set)
    assert hub.json == [{"type": "ended", "call_id": "c0ffee01"}]
    assert conn.relay.ends() == []
    assert conn.relay.sent == [{"type": "audio_start"}]
    assert conn.state == server.DeviceState.STREAMING
    assert "live_session_end" not in conn.ws.types()


async def test_device_disconnect_tears_down(fake_relay, hub_on, monkeypatch):
    """Endpoint WS vero (uvicorn): chiusura del device -> ended al hub, socket chiuso."""
    hub = hub_on
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                       log_level="warning", lifespan="off"))
    task = asyncio.create_task(srv.serve())
    try:
        assert await wait_for(lambda: srv.started)
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/audio?device_id=aa:bb")
        await ws.send(json.dumps({"type": "hello", "fw": "atoms3r-test"}))
        assert await wait_for(lambda: "AA:BB" in server._connections and fake_relay.instances)
        conn = server._connections["AA:BB"]
        relay = fake_relay.instances[-1]
        await orch_hub_start(conn)
        assert conn.hub_session is not None
        await ws.close()
        assert await wait_for(hub.eof.is_set)
        assert hub.json == [{"type": "ended", "call_id": "c0ffee01"}]
        assert relay.ends()[0]["reason"] == "error"
        assert conn.hub_session is None
        assert await wait_for(lambda: "AA:BB" not in server._connections)
    finally:
        srv.should_exit = True
        await task


# --- fix round: decisioni condivise D1-D9 -----------------------------------

@pytest.mark.parametrize("reason,expected", [
    ("button", "button"), ("error", "error"), (None, "button"), ("weird", "button")])
async def test_device_end_reason_passthrough(conn, hub_on, reason, expected):
    # D4: reason dell'Atom in {button, error} passa tale e quale, altrimenti "button"
    await start_session(conn, hub_on)
    msg = {"type": "live_session_end"}
    if reason is not None:
        msg["reason"] = reason
    await server._handle_text(conn, json.dumps(msg))
    assert conn.relay.ends() == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": expected}]


async def test_audio_end_in_hub_session_is_button(conn, hub_on):
    await start_session(conn, hub_on)
    await server._handle_text(conn, json.dumps({"type": "audio_end"}))
    assert conn.relay.ends()[0]["reason"] == "button"
    assert {"type": "audio_end"} not in conn.relay.sent
    assert conn.relay.connected


async def test_speaker_stop_in_hub_session_ends_it(conn, hub_on):
    # D6: doppio tap = fine sessione "button"; speaker_stop non inoltrato, relay aperto
    hub = hub_on
    await start_session(conn, hub)
    await server._handle_text(conn, json.dumps({"type": "speaker_stop"}))
    assert conn.hub_session is None
    assert await wait_for(hub.eof.is_set)
    assert hub.json == [{"type": "ended", "call_id": "c0ffee01"}]
    assert conn.ws.json[-1] == {"type": "live_session_end"}
    assert conn.relay.sent == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "button"}]
    assert conn.relay.connected
    assert conn.state == server.DeviceState.IDLE


async def test_turn_messages_not_forwarded_in_hub_session(conn, hub_on):
    # D8: il loop a turni dell'orchestrator non arriva all'Atom in duplex
    session = await start_session(conn, hub_on)
    before = list(conn.ws.json)
    for t in ("ready", "speech_end", "tts_start", "tts_done", "trigger_listen"):
        await conn.relay.orch({"type": t})
    assert conn.ws.json == before
    assert conn.state == server.DeviceState.BUSY
    assert conn.hub_session is session and conn.relay.connected
    # messaggi fuori dal loop a turni passano come oggi
    await conn.relay.orch({"type": "config_update", "x": 1})
    assert conn.ws.json[-1] == {"type": "config_update", "x": 1}


async def test_orchestrator_end_then_tts_done_closes_turn(conn, hub_on):
    # D1: live_session_end e poi tts_done; il secondo non e' piu' filtrato
    hub = hub_on
    await start_session(conn, hub)
    await conn.relay.orch({"type": "live_session_end"})
    await conn.relay.orch({"type": "tts_done"})
    assert hub.json == [{"type": "ended", "call_id": "c0ffee01"}]
    assert conn.ws.types()[-2:] == ["live_session_end", "tts_done"]
    assert conn.state == server.DeviceState.IDLE and conn.wakeword.muted_for == 0
    assert conn.relay is None


async def test_button_end_then_orchestrator_echo(conn, hub_on):
    # tap, poi l'eco D1 dell'orchestrator: device in IDLE, relay chiuso dal tts_done
    await start_session(conn, hub_on)
    await server._handle_text(conn, json.dumps({"type": "live_session_end", "reason": "button"}))
    relay = conn.relay
    await relay.orch({"type": "live_session_end"})
    await relay.orch({"type": "tts_done"})
    assert relay.ends() == [{"type": "live_session_end", "call_id": "c0ffee01", "reason": "button"}]
    assert conn.ws.types()[-2:] == ["live_session_end", "tts_done"]
    assert conn.state == server.DeviceState.IDLE and conn.relay is None


@pytest.fixture
async def silent_hub(monkeypatch):
    """Hub che non risponde all'offer: l'handshake resta in corso."""
    hub = await FakeHub(mode="silent").start()
    monkeypatch.setattr(server, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "HUB_PORT", hub.port)
    monkeypatch.setattr(server.HubRelay, "ACCEPT_TIMEOUT_S", 30)
    yield hub
    await hub.stop()


async def pending_handshake(conn, hub):
    await conn.relay.orch(hub_start_msg())
    assert await wait_for(lambda: hub.offer is not None)
    task = conn.hub_start_task
    assert task is not None and not task.done()
    return task


@pytest.mark.parametrize("msg_type", ["audio_end", "speaker_stop"])
async def test_tap_during_handshake_reports_button(conn, silent_hub, msg_type):
    # D9: abbandono durante l'handshake -> live_session_end all'orchestrator prima del resto
    relay = conn.relay
    task = await pending_handshake(conn, silent_hub)
    await server._handle_text(conn, json.dumps({"type": msg_type}))
    assert task.done() and conn.hub_start_task is None and conn.hub_session is None
    assert await wait_for(silent_hub.eof.is_set)
    assert relay.sent == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "button"},
        {"type": msg_type}]
    assert conn.state == server.DeviceState.IDLE and conn.relay is None  # flusso di oggi


async def test_device_end_error_during_handshake(conn, silent_hub):
    await pending_handshake(conn, silent_hub)
    await server._handle_text(conn, json.dumps({"type": "live_session_end", "reason": "error"}))
    assert conn.hub_start_task is None
    assert await wait_for(silent_hub.eof.is_set)
    assert conn.relay.sent == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "error"}]


async def test_orchestrator_end_during_handshake(conn, silent_hub):
    # Il relay non e' bloccato dall'handshake: la fine dall'orchestrator lo annulla subito
    await pending_handshake(conn, silent_hub)
    await conn.relay.orch({"type": "live_session_end"})
    assert conn.hub_start_task is None and conn.hub_session is None
    assert await wait_for(silent_hub.eof.is_set)
    assert conn.relay.ends() == []
    assert conn.ws.json == [{"type": "live_session_end"}]
    assert silent_hub.json == []  # nessun accept, niente ended


async def test_audio_start_during_handshake_aborts_silently(conn, silent_hub):
    await pending_handshake(conn, silent_hub)
    await server._handle_text(conn, json.dumps({"type": "audio_start"}))
    assert conn.hub_start_task is None
    assert conn.relay.sent == [{"type": "audio_start"}]
    assert conn.state == server.DeviceState.STREAMING


async def test_new_start_replaces_pending_handshake(conn, silent_hub):
    first = await pending_handshake(conn, silent_hub)
    await conn.relay.orch(hub_start_msg(call_id="c0ffee02"))
    assert first.done()
    assert conn.hub_pending_call_id == "c0ffee02"
    # duplicato dello stesso call_id: ignorato
    second = conn.hub_start_task
    await conn.relay.orch(hub_start_msg(call_id="c0ffee02"))
    assert conn.hub_start_task is second
    assert conn.relay.ends() == []


async def _uvicorn(port):
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                       log_level="warning", lifespan="off"))
    task = asyncio.create_task(srv.serve())
    assert await wait_for(lambda: srv.started)
    return srv, task


async def test_device_disconnect_during_handshake(fake_relay, silent_hub):
    # D9: device sparito a meta' handshake -> "error" all'orchestrator, socket hub chiuso
    port = free_port()
    srv, task = await _uvicorn(port)
    try:
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/audio?device_id=aa:bb")
        await ws.send(json.dumps({"type": "hello", "fw": "atoms3r-test"}))
        assert await wait_for(lambda: "AA:BB" in server._connections and fake_relay.instances)
        conn = server._connections["AA:BB"]
        relay = fake_relay.instances[-1]
        await pending_handshake(conn, silent_hub)
        await ws.close()
        assert await wait_for(silent_hub.eof.is_set)
        assert await wait_for(lambda: relay.ends() != [])
        assert relay.ends() == [
            {"type": "live_session_end", "call_id": "c0ffee01", "reason": "error"}]
        assert conn.hub_start_task is None
    finally:
        srv.should_exit = True
        await task


async def test_reconnect_ends_old_hub_session(fake_relay, hub_on):
    # Stesso device_id che si riconnette col vecchio WS ancora su: la sessione hub del
    # vecchio si chiude subito, non quando il vecchio receive() va in errore
    hub = hub_on
    port = free_port()
    srv, task = await _uvicorn(port)
    try:
        ws1 = await websockets.connect(f"ws://127.0.0.1:{port}/ws/audio?device_id=aa:bb")
        await ws1.send(json.dumps({"type": "hello", "fw": "atoms3r-test"}))
        assert await wait_for(lambda: "AA:BB" in server._connections and fake_relay.instances)
        old = server._connections["AA:BB"]
        old_relay = fake_relay.instances[-1]
        await orch_hub_start(old)
        assert old.hub_session is not None
        ws2 = await websockets.connect(f"ws://127.0.0.1:{port}/ws/audio?device_id=aa:bb")
        assert await wait_for(lambda: server._connections.get("AA:BB") is not old)
        new = server._connections["AA:BB"]
        assert await wait_for(hub.eof.is_set)
        assert old.hub_session is None
        assert old_relay.ends() == [
            {"type": "live_session_end", "call_id": "c0ffee01", "reason": "error"}]
        assert new.hub_session is None
        await ws1.close()
        await ws2.close()
        assert await wait_for(lambda: "AA:BB" not in server._connections)
    finally:
        srv.should_exit = True
        await task


async def test_teardown_error_does_not_skip_cleanup(conn, hub_on, monkeypatch):
    session = await start_session(conn, hub_on)

    async def broken_close(send_ended):
        raise RuntimeError("ffi")
    monkeypatch.setattr(session, "close", broken_close)
    await server._teardown_hub(conn)  # non solleva
    assert conn.hub_session is None


async def test_trigger_listen_api_refused_in_hub_session(conn, hub_on, monkeypatch):
    await start_session(conn, hub_on)
    monkeypatch.setitem(server._connections, conn.device_id, conn)
    with pytest.raises(server.HTTPException) as e:
        await server.trigger_listen(conn.device_id)
    assert e.value.status_code == 409
    assert conn.state == server.DeviceState.BUSY


async def test_clear_restarts_prebuffer_burst(conn, hub_on):
    hub = hub_on
    session = await start_session(conn, hub)
    hub.send_audio(tone(1000, 2.0, 24000).tobytes())
    assert await wait_for(lambda: len(conn.ws.binary) >= 15)
    hub.send_json({"type": "clear", "call_id": "c0ffee01"})
    assert await wait_for(lambda: "audio_clear" in conn.ws.types(), step=0.001)
    sent = len(conn.ws.binary)
    hub.send_audio(tone(1000, 0.5, 24000).tobytes())
    # il device ha svuotato il buffer: si rimanda subito il pre-buffer (5 frame), senza
    # il reset si ripartirebbe al ritmo di prima (un frame ogni 20 ms)
    assert await wait_for(lambda: len(conn.ws.binary) >= sent + 5, timeout=0.035, step=0.002)
    assert session.queued_frames > 0


# --- NabuVoice: AEC sul device (cap aec_onboard), uplink PCM grezzo ----------

NABU_CAPS = {"duplex", "pcm_uplink", "aec_onboard"}


def pcm_frames(pcm16k, size=640):
    raw = pcm16k.astype("<i2").tobytes()
    return [raw[i:i + size] for i in range(0, len(raw), size)]


@pytest.fixture
def nabu(conn, monkeypatch):
    conn.caps = set(NABU_CAPS)
    conn.device_type = "NabuVoice"

    async def no_server_aec(*a, **k):
        raise AssertionError("aec_onboard: niente AEC lato server")
    monkeypatch.setattr(server, "_create_aec", no_server_aec)
    return conn


@pytest.mark.parametrize("extra_caps,ref_mode", [((), "level"), (("aec_ref",), "pcm"), (("aec_ref",), "level")])
async def test_aec_onboard_start_without_aec_ref_and_passthrough(nabu, hub_on, monkeypatch, extra_caps, ref_mode):
    monkeypatch.setattr(server, "AEC_BACKEND", "webrtc")  # ignorato per aec_onboard
    monkeypatch.setattr(server, "HUB_AEC_REF_MODE", ref_mode)
    monkeypatch.setattr(server, "HUB_ATOM_MIC_GAIN_DB", "18")
    nabu.caps |= set(extra_caps)
    session = await start_session(nabu, hub_on)
    assert nabu.ws.json == [{"type": "live_session_start", "duplex": True}]
    assert session.aec.backend_name == "off"
    assert session.aec._gain == 1.0 and not session.aec.aligned  # nessun guadagno ridato
    assert nabu.live_session is True and nabu.wakeword.muted_for == 3600


async def test_aec_onboard_pcm_uplink_bit_exact_with_vu(nabu, hub_on, monkeypatch):
    monkeypatch.setattr(livesession, "_VU_INTERVAL_S", 0.0)
    hub = hub_on
    await start_session(nabu, hub)
    rng = np.random.default_rng(11)
    pcm = tone(440, 0.5, 16000) + (rng.standard_normal(8000) * 30).astype(np.int16)
    frames = pcm_frames(pcm)
    # frame che sembrano pacchetti a livelli / allineati dell'Atom: per la Nabu sono PCM
    frames[3] = bytes([0xA5, 0x03]) + frames[3][2:]
    frames[4] = bytes([0xA5, 0x01]) + frames[4][2:]
    sent = b"".join(frames)
    for f in frames:
        await server._handle_binary(nabu, f)
    assert await wait_for(lambda: len(hub.audio) >= len(sent))
    assert bytes(hub.audio) == sent  # niente Opus, AEC, cancello o AGC: bit per bit
    assert hub.audio_call_ids == {"c0ffee01"}
    vus = [m for m in nabu.ws.json if m.get("type") == "vu"]
    assert vus and all(0 <= m["mic"] <= 255 for m in vus)
    assert max(m["mic"] for m in vus) > 150
    assert nabu.relay.sent_bin == []
    assert nabu.wakeword.processed == 0


async def test_aec_onboard_pcm_uplink_any_length_keeps_sample_alignment(nabu, hub_on):
    hub = hub_on
    session = await start_session(nabu, hub)
    raw = tone(300, 0.2, 16000).astype("<i2").tobytes()
    cuts = [0, 641, 1279, 1280, 2001, 3333, len(raw)]  # spezzature dispari comprese
    for a, b in zip(cuts, cuts[1:]):
        await session.uplink_pcm(raw[a:b])
    assert await wait_for(lambda: len(hub.audio) >= len(raw))
    assert bytes(hub.audio) == raw


async def test_aec_onboard_live_interrupt(nabu, hub_on):
    hub = hub_on
    session = await start_session(nabu, hub)
    hub.send_audio(tone(1000, 2.0, 24000).tobytes())
    assert await wait_for(lambda: session.queued_frames > 10)
    await server._handle_text(nabu, json.dumps({"type": "live_interrupt"}))
    assert session.queued_frames == 0
    assert nabu.ws.json[-1] == {"type": "audio_clear"}
    assert await wait_for(lambda: {"type": "interrupt", "call_id": "c0ffee01"} in hub.json)
    assert nabu.hub_session is session and nabu.relay.ends() == []


async def test_aec_onboard_hub_clear_sends_audio_clear(nabu, hub_on):
    hub = hub_on
    session = await start_session(nabu, hub)
    hub.send_audio(tone(1000, 2.0, 24000).tobytes())
    assert await wait_for(lambda: len(nabu.ws.binary) >= 10)
    # downlink: Opus 16 kHz da 20 ms come per l'Atom
    dec = opuslib.Decoder(16000, 1)
    assert len(dec.decode(nabu.ws.binary[0], 320)) == 640
    hub.send_json({"type": "clear", "call_id": "c0ffee01"})
    assert await wait_for(lambda: "audio_clear" in nabu.ws.types())
    assert session.queued_frames == 0 and nabu.hub_session is session


async def test_aec_onboard_pcm_tail_after_end_not_decoded(nabu, hub_on, monkeypatch):
    await start_session(nabu, hub_on)
    await server._handle_text(nabu, json.dumps({"type": "live_session_end", "reason": "button"}))
    assert nabu.hub_session is None and nabu.state == server.DeviceState.IDLE
    assert nabu.relay.ends() == [{"type": "live_session_end", "call_id": "c0ffee01", "reason": "button"}]
    decoded = []
    monkeypatch.setattr(nabu, "decode_opus", lambda d: decoded.append(d))
    for f in pcm_frames(tone(440, 0.1, 16000)):
        await server._handle_binary(nabu, f)
    assert decoded == [] and nabu.wakeword.processed == 0
    nabu.pcm_tail_until = 0.0  # passata la coda: la wake word lato server torna come oggi
    await server._handle_binary(nabu, b"\x08opus")
    assert decoded == [b"\x08opus"]


async def test_aec_onboard_hub_hangup_tells_device_and_drops_pcm_tail(nabu, hub_on, monkeypatch):
    """Fine dal hub: la Nabu riceve live_session_end e il PCM ancora in volo non va alla wake word."""
    hub = hub_on
    await start_session(nabu, hub)
    hub.send_json({"type": "hangup", "call_id": "c0ffee01"})
    assert await wait_for(lambda: nabu.hub_session is None)
    assert nabu.ws.json[-1] == {"type": "live_session_end"}
    assert nabu.relay.ends() == [
        {"type": "live_session_end", "call_id": "c0ffee01", "reason": "hub_hangup"}]
    assert nabu.pcm_tail_until > 0
    decoded = []
    monkeypatch.setattr(nabu, "decode_opus", lambda d: decoded.append(d))
    for f in pcm_frames(tone(440, 0.1, 16000)):
        await server._handle_binary(nabu, f)
    assert decoded == [] and nabu.wakeword.processed == 0


async def test_atom_end_has_no_pcm_tail(conn, hub_on):
    await start_session(conn, hub_on)
    await server._handle_text(conn, json.dumps({"type": "live_session_end", "reason": "button"}))
    assert conn.pcm_tail_until == 0.0
    for f in opus_frames(tone(440, 0.1, 16000)):
        await server._handle_binary(conn, f)
    assert conn.wakeword.processed > 0


async def test_nabuvoice_over_websocket(fake_relay, hub_on, monkeypatch):
    """Endpoint WS vero: hello NabuVoice con caps, handshake hub come l'Atom, PCM al hub."""
    monkeypatch.setattr(livesession, "_VU_INTERVAL_S", 0.0)
    hub = hub_on
    port = free_port()
    srv, task = await _uvicorn(port)
    try:
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/audio?device_id=nabu:01")
        await ws.send(json.dumps({"type": "hello", "fw": "1.1.0-nabuvoice",
                                  "caps": ["duplex", "pcm_uplink", "aec_onboard"]}))
        assert await wait_for(lambda: "NABU:01" in server._connections and fake_relay.instances)
        conn = server._connections["NABU:01"]
        assert conn.device_type == "NabuVoice" and conn.caps == NABU_CAPS
        assert json.loads(await ws.recv())["type"] == "welcome"
        conn.state = server.DeviceState.BUSY  # fine del turno "avvia una sessione live"
        await orch_hub_start(conn)
        assert conn.hub_session is not None
        assert json.loads(await ws.recv()) == {"type": "live_session_start", "duplex": True}
        raw = tone(440, 0.2, 16000).astype("<i2").tobytes()
        for f in pcm_frames(np.frombuffer(raw, dtype="<i2")):
            await ws.send(f)
        assert await wait_for(lambda: len(hub.audio) >= len(raw))
        assert bytes(hub.audio) == raw
        assert json.loads(await ws.recv())["type"] == "vu"
        await ws.send(json.dumps({"type": "live_session_end", "reason": "button"}))
        assert await wait_for(hub.eof.is_set)
        assert fake_relay.instances[-1].ends() == [
            {"type": "live_session_end", "call_id": "c0ffee01", "reason": "button"}]
        await ws.close()
        assert await wait_for(lambda: "NABU:01" not in server._connections)
    finally:
        srv.should_exit = True
        await task
