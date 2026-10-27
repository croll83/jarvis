"""Unita': framing ipc, resampler 24->16 kHz, AEC."""
import asyncio
import importlib.util
import os

import numpy as np
import pytest

import aec
import hubrelay
from livesession import Resampler24kTo16k, vu_level

HUB_IPC = ("/home/jarvis/sviluppo/hermes-jarvis/services/voice-live-hub/"
           "voice_live_hub/ipc.py")


async def test_ipc_roundtrip():
    reader = asyncio.StreamReader()
    reader.feed_data(hubrelay.encode_json({"type": "offer", "call_id": "ab"}))
    reader.feed_data(hubrelay.encode_audio("ab", b"\x01\x00\x02\x00"))
    reader.feed_eof()
    assert await hubrelay.read_frame(reader) == (hubrelay.JSON, {"type": "offer", "call_id": "ab"})
    assert await hubrelay.read_frame(reader) == (hubrelay.AUDIO, ("ab", b"\x01\x00\x02\x00"))
    assert await hubrelay.read_frame(reader) is None


@pytest.mark.skipif(not os.path.exists(HUB_IPC), reason="hub repo not checked out")
async def test_ipc_matches_hub_copy():
    spec = importlib.util.spec_from_file_location("hub_ipc", HUB_IPC)
    hub_ipc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub_ipc)
    msg = {"type": "clear", "call_id": "c0ffee01"}
    assert hubrelay.encode_json(msg) == hub_ipc.encode_json(msg)
    assert hubrelay.encode_audio("c0ffee01", b"xyzw") == hub_ipc.encode_audio("c0ffee01", b"xyzw")
    reader = asyncio.StreamReader()
    reader.feed_data(hub_ipc.encode_audio("c0ffee01", b"\x00\x01"))
    reader.feed_eof()
    assert await hubrelay.read_frame(reader) == (hubrelay.AUDIO, ("c0ffee01", b"\x00\x01"))


def _tone(freq, n, rate, amp=8000.0):
    return (amp * np.sin(2 * np.pi * freq * np.arange(n) / rate)).astype(np.int16)


def test_resampler_rate_and_frequency():
    r = Resampler24kTo16k()
    out = r.process(_tone(1000, 24000, 24000))
    assert len(out) == 16000
    spec = np.abs(np.fft.rfft(out[800:].astype(np.float64)))
    assert abs(np.argmax(spec) * 16000 / len(out[800:]) - 1000) < 5
    rms = np.sqrt(np.mean(out[800:].astype(np.float64) ** 2))
    assert abs(rms - 8000 / np.sqrt(2)) < 150


def test_resampler_streaming_equals_one_shot():
    x = _tone(700, 24000, 24000)
    whole = Resampler24kTo16k().process(x)
    r = Resampler24kTo16k()
    sizes = [1, 479, 480, 7, 960, 2401]
    parts, i, k = [], 0, 0
    while i < len(x):
        n = sizes[k % len(sizes)]
        parts.append(r.process(x[i:i + n]))
        i += n
        k += 1
    chunked = np.concatenate(parts)
    assert len(chunked) == len(whole)
    assert np.max(np.abs(chunked.astype(int) - whole.astype(int))) <= 1


def test_resampler_rejects_aliasing():
    # 10 kHz a 24 kHz sopra il Nyquist di 16 kHz: va tagliato, non ripiegato a 6 kHz
    out = Resampler24kTo16k().process(_tone(10000, 24000, 24000))
    rms = np.sqrt(np.mean(out[800:].astype(np.float64) ** 2))
    assert rms < 100


def test_vu_level():
    assert vu_level(0) == 0
    assert vu_level(32768) == 255
    assert vu_level(32.768) == 0  # -60 dBFS
    assert 120 < vu_level(1036) < 135  # ~-30 dBFS


def _echo_scenario(ec, seconds=8, delay=1600, gain=0.5):
    """Far-end a raffiche (parlato finto), eco ritardata nel mic; ritorna ERLE in dB."""
    rng = np.random.default_rng(1)
    sr = 16000
    n = sr * seconds
    far = rng.standard_normal(n) * 3000 * ((np.sin(np.arange(n) / sr * 2 * np.pi * 1.5) > 0) * 1.0)
    far = far.astype(np.int16)
    near = np.zeros(n, dtype=np.int16)
    near[delay:] = (far[:-delay] * gain).astype(np.int16)
    out = bytearray()
    for i in range(0, n - 319, 320):  # 20 ms come i frame dell'Atom
        ec.render(far[i:i + 320].tobytes())
        out += ec.process(near[i:i + 320].tobytes())
    o = np.frombuffer(bytes(out), dtype=np.int16).astype(np.float64)
    half = len(o) // 2
    e_in = np.mean(near[half:len(o)].astype(np.float64) ** 2)
    e_out = max(np.mean(o[half:] ** 2), 1e-9)
    return 10 * np.log10(e_in / e_out), len(o)


def test_aec_webrtc_cancels_echo():
    pytest.importorskip("livekit.rtc")
    ec = aec.create_echo_canceller("webrtc")
    assert ec.backend_name == "webrtc"
    erle, n = _echo_scenario(ec)
    ec.close()
    assert n == 16000 * 8
    assert erle > 20


def test_aec_speex_cancels_echo():
    try:
        ec = aec.create_echo_canceller("speex")
    except Exception:
        pytest.skip("libspeexdsp missing")
    if ec.backend_name != "speex":
        pytest.skip("libspeexdsp missing")
    erle, _ = _echo_scenario(ec)
    ec.close()
    assert erle > 20


def test_aec_off_is_passthrough_and_chunks():
    ec = aec.create_echo_canceller("off")
    assert ec.backend_name == "off"
    x = _tone(440, 500, 16000)
    out = ec.process(x.tobytes())
    assert out == x[:480].tobytes()  # 3 blocchi da 10 ms, il resto in coda
    assert ec.process(b"") == b""
    out2 = ec.process(_tone(440, 140, 16000).tobytes())
    assert len(out2) == 320


def test_aec_reset_render_and_cap():
    try:
        ec = aec.create_echo_canceller("speex")
    except Exception:
        pytest.skip("libspeexdsp missing")
    if ec.backend_name == "off":
        pytest.skip("no AEC backend")
    ec.render(bytes(640))
    assert ec.render_backlog_bytes == 640
    ec.reset_render()
    assert ec.render_backlog_bytes == 0
    ec.render(bytes(16000 * 2 * 3))
    assert ec.render_backlog_bytes == 16000 * 2  # al massimo 1 s
    ec.close()


def test_unknown_backend_falls_back(caplog):
    ec = aec.create_echo_canceller("bogus")
    assert ec.backend_name in ("webrtc", "speex", "off")
    ec.close()


async def test_session_start_after_close_has_no_pacer():
    from livesession import HubLiveSession

    class _Relay:
        async def send_json(self, m):
            pass

        async def close(self):
            pass

    async def _noop(_):
        pass

    s = HubLiveSession("X", "c1", _Relay(), _noop, _noop, "off")
    await s.close(send_ended=False)
    s.start()
    assert s._pacer is None


def test_render_delay_line_lags_the_reference_and_survives_silence_and_clear():
    import aec

    class _Spy:
        name = "spy"

        def __init__(self):
            self.fars = []

        def process(self, near, far):
            self.fars.append(far)
            return near

        def close(self):
            pass

    spy = _Spy()
    e = aec.EchoCanceller(spy, render_delay_ms=20)  # 2 chunk da 10 ms
    chunk = lambda v: bytes([v, 0]) * aec.CHUNK_SAMPLES
    e.render(chunk(1) + chunk(2))
    e.process(bytes(aec._CHUNK_BYTES * 4))  # 40 ms di mic: zeri, zeri, 1, 2
    assert spy.fars[:2] == [aec._SILENCE, aec._SILENCE]
    assert spy.fars[2:4] == [chunk(1), chunk(2)]
    # dopo un silenzio la linea resta lunga uguale (avanza col mic, non si svuota)
    e.process(bytes(aec._CHUNK_BYTES * 3))
    e.render(chunk(3))
    e.process(bytes(aec._CHUNK_BYTES * 3))
    assert spy.fars[-3:] == [aec._SILENCE, aec._SILENCE, chunk(3)]
    # barge-in: riferimento e linea azzerati
    e.render(chunk(4))
    e.reset_render()
    spy.fars.clear()
    e.process(bytes(aec._CHUNK_BYTES * 3))
    assert spy.fars == [aec._SILENCE] * 3
