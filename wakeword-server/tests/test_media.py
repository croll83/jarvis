"""Unita': framing ipc, resampler 24->16 kHz, AEC."""
import asyncio
import importlib.util
import os

import numpy as np
import pytest

import aec
import hubrelay
from livesession import Resampler24kTo16k, SlotPicker, parse_aligned, parse_level, vu_level

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
    for i in range(0, n - 319, 320):  # 20 ms come i frame dell'Atom, orologio simulato
        ec.render(far[i:i + 320].tobytes(), t=i / 16000)
        out += ec.process(near[i:i + 320].tobytes(), now=i / 16000 + 0.02)
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
    ec.render(bytes(16000 * 2 * 5))
    assert ec.render_backlog_bytes == 16000 * 2 * aec._RING_KEEP_S  # timeline tenuta al massimo 3 s
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


class _Spy:
    name = "spy"

    def __init__(self):
        self.fars = []

    def process(self, near, far):
        self.fars.append(far)
        return near

    def close(self):
        pass


def _chunk(v):
    return bytes([v, 0]) * aec.CHUNK_SAMPLES


def test_reference_is_aligned_by_time_not_by_arrival(monkeypatch):
    monkeypatch.setattr(aec, "_READ_LAG_MS", 0)
    # riferimento programmato a t=1.00 s; il mic arriva a raffiche irregolari ma i suoi campioni
    # restano contati: il chunk catturato a t=1.00 incontra il riferimento di t=1.00
    spy = _Spy()
    e = aec.EchoCanceller(spy, render_delay_ms=0, auto_delay=False)
    e.process(bytes(aec._CHUNK_BYTES), now=0.99)          # ancora: questo chunk = 0.98-0.99
    e.render(_chunk(7), t=1.00)
    e.render(_chunk(8), t=1.01)
    # raffica in ritardo: 30 ms di mic arrivano tutti insieme a t=1.05
    e.process(bytes(aec._CHUNK_BYTES * 3), now=1.05)
    assert spy.fars == [aec._SILENCE, aec._SILENCE, _chunk(7), _chunk(8)]


def test_fixed_delay_line_lags_the_reference(monkeypatch):
    monkeypatch.setattr(aec, "_READ_LAG_MS", 0)
    spy = _Spy()
    e = aec.EchoCanceller(spy, render_delay_ms=20, auto_delay=False)
    e.process(bytes(aec._CHUNK_BYTES), now=0.01)           # mic ancorato: chunk 0.00-0.01
    e.render(_chunk(1) + _chunk(2), t=0.01)
    e.process(bytes(aec._CHUNK_BYTES * 4), now=0.05)
    assert spy.fars[1:] == [aec._SILENCE, aec._SILENCE, _chunk(1), _chunk(2)]


def test_auto_delay_finds_a_long_echo_and_cancels_it():
    pytest.importorskip("livekit.rtc")
    rng = np.random.default_rng(1)
    e = aec.EchoCanceller(aec._WebRtcBackend(), render_delay_ms=0, auto_delay=True)
    seconds, d = 20, 16 * 700                               # eco a 700 ms: oltre il limite dell'AEC3
    # parlato a raffiche (come frasi): rumore modulato
    env = np.repeat(rng.random(seconds * 10) > 0.3, 1600).astype(float)
    far = (rng.standard_normal(16000 * seconds) * 3000 * env).astype(np.int16)
    echo = np.concatenate([np.zeros(d, np.int16), (far[:-d] * 0.5).astype(np.int16)])
    out = bytearray()
    for i in range(0, len(far) - 319, 320):
        e.render(far[i:i + 320].tobytes(), t=i / 16000)
        out += e.process(echo[i:i + 320].tobytes(), now=i / 16000 + 0.02)
    assert 450 <= e.render_delay_ms <= 650
    o = np.frombuffer(bytes(out), np.int16).astype(float)
    n = echo.astype(float)[:len(o)]
    tail = slice(len(o) - 16000 * 5, len(o))
    erle = 10 * np.log10(np.mean(n[tail] ** 2) / max(np.mean(o[tail] ** 2), 1e-9))
    assert erle > 25


def test_production_order_mic_before_reference_still_cancels():
    # Come in produzione: a ogni passo da 20 ms il mic arriva PRIMA che il pacer mandi il frame;
    # a inizio frase il pacer manda 5 frame di pre-buffer a raffica; l'Atom suona con 60 ms di
    # cuscinetto e il mic risale con 30 ms di rete. Prima della correzione il riferimento finiva
    # sempre dietro la lettura del mic e l'AEC riceveva solo zeri.
    pytest.importorskip("livekit.rtc")
    rng = np.random.default_rng(2)
    fs, frame = 16000, 320
    secs, start_s = 12, 1.0
    n_frames = secs * fs // frame
    j0 = int(start_s * fs / frame)
    far = np.zeros(secs * fs, np.int16)
    talk = (rng.standard_normal(fs * 8) * 3000).astype(np.int16)
    far[j0 * frame:j0 * frame + len(talk)] = talk
    send = {}
    for j in range(j0, j0 + len(talk) // frame):
        send[j] = start_s if j < j0 + 5 else start_s + 0.02 * (j - j0 - 4)
    played = np.zeros(secs * fs + fs, np.float64)
    for j in send:
        p = int(round((start_s + 0.06 + 0.02 * (j - j0)) * fs))
        played[p:p + frame] += far[j * frame:(j + 1) * frame] * 0.5
    mic = played.astype(np.int16)
    up = int(0.03 * fs)
    e = aec.EchoCanceller(aec._WebRtcBackend(), render_delay_ms=0, auto_delay=True)
    out, sent = bytearray(), set()
    for n in range(1, n_frames):
        T = n * 0.02
        a = int(round((T - 0.02) * fs)) - up
        b = a + frame
        chunk = mic[a:b] if a >= 0 else np.zeros(frame, np.int16)
        out += e.process(chunk.tobytes(), now=T)
        for j, s_t in send.items():
            if j not in sent and s_t <= T:
                e.render(far[j * frame:(j + 1) * frame].tobytes(), t=s_t)
                sent.add(j)
    o = np.frombuffer(bytes(out), np.int16).astype(float)
    m = mic.astype(float)
    # mic allineato all'uscita: uscita n corrisponde al mic catturato fino a (n*0.02 - up)
    seg_o = o[fs * 6:fs * 8]
    seg_m = m[fs * 6 - up:fs * 8 - up]
    erle = 10 * np.log10(np.mean(seg_m ** 2) / max(np.mean(seg_o ** 2), 1e-9))
    assert erle > 20


# --- riferimento allineato dal device (loopback ES8311) ----------------------

def test_parse_aligned_packets_and_opus_frames():
    pairs = np.array([[1, -2], [3, -4]], dtype="<i2")
    seq, got, muted = parse_aligned(bytes([0xA5, 0x01, 0x34, 0x12]) + pairs.tobytes())
    assert seq == 0x1234 and got.tolist() == [[1, -2], [3, -4]] and not muted
    assert parse_aligned(bytes([0xA5, 0x81, 0, 0]) + pairs.tobytes())[2] is True
    import opuslib
    enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    assert parse_aligned(enc.encode(_tone(440, 320, 16000).tobytes(), 320)) is None
    assert parse_aligned(bytes([0xA5, 0x01, 0, 0, 1, 2])) is None  # coppia monca
    assert parse_aligned(bytes([0xA5, 0x02, 0, 0]) + pairs.tobytes()) is None  # formato ignoto


def test_slot_picker_finds_the_digitally_silent_loopback():
    rng = np.random.default_rng(4)
    mic = (rng.standard_normal((320, 1)) * 30).astype(np.int16)
    silent = np.zeros((320, 1), np.int16)
    loud = (rng.standard_normal((320, 1)) * 3000).astype(np.int16)
    for ref_slot in (0, 1):
        p = SlotPicker()
        for k in range(30):
            ref = silent if k % 2 else loud
            p.feed(np.hstack((ref, mic)) if ref_slot == 0 else np.hstack((mic, ref)))
        assert p.locked and p.ref == ref_slot


def test_aligned_reference_cancels_from_the_start():
    # Riferimento letto nello stesso frame del mic: l'eco segue di 2 ms, e' piu' forte del
    # riferimento (+60 dB di catena mic) e si cancella senza stime di ritardo ne' render().
    pytest.importorskip("livekit.rtc")
    rng = np.random.default_rng(5)
    fs, secs, delay = 16000, 6, 32
    far = (rng.standard_normal(fs * secs) * 1500 * (np.sin(np.arange(fs * secs) / fs * 2 * np.pi * 1.5) > 0))
    far = far.astype(np.int16)
    echo = np.zeros(fs * secs)
    echo[delay:] = far[:-delay] * 2.0
    mic = np.clip(echo + rng.standard_normal(fs * secs) * 20, -32768, 32767).astype(np.int16)
    ec = aec.EchoCanceller(aec._WebRtcBackend())
    ec._gate = None  # qui si misura l'AEC, non il cancello
    ec.render(bytes(640))  # ignorato dal primo pacchetto allineato in poi
    out = bytearray()
    for i in range(0, fs * secs - 319, 320):
        out += ec.process_aligned(mic[i:i + 320].tobytes(), far[i:i + 320].tobytes())
    ec.close()
    assert ec.aligned and ec.render_backlog_bytes == 0
    o = np.frombuffer(bytes(out), np.int16).astype(float)
    m = mic[:len(o)].astype(float)
    for a, b in ((fs * 1, fs * 3), (fs * 3, fs * 6)):
        erle = 10 * np.log10(np.mean(m[a:b] ** 2) / max(np.mean(o[a:b] ** 2), 1e-9))
        assert erle > 20, (a, erle)


class _Pass:
    name = "pass"

    def process(self, near, far):
        return near

    def close(self):
        pass


def _level(pcm):
    x = np.frombuffer(pcm, np.int16).astype(float)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def test_gate_silences_residual_echo_while_the_reference_plays():
    ec = aec.EchoCanceller(_Pass())
    far = _tone(300, 320, 16000, amp=8000.0)          # Jarvis a ~ -12 dBFS
    residual = _tone(1000, 320, 16000, amp=300.0)     # residuo ~ -40 dBFS: sotto la soglia
    out = ec.process_aligned(residual.tobytes(), far.tobytes())
    assert _level(out) == 0 and ec.gate_stats == (2, 0)
    # Jarvis tace: il cancello resta chiuso per la coda (300 ms) e poi riapre
    silence = np.zeros(320, np.int16).tobytes()
    outs = [ec.process_aligned(residual.tobytes(), silence) for _ in range(18)]
    assert _level(outs[0]) == 0 and _level(outs[-1]) > 200


def test_gate_lets_a_loud_barge_in_through_and_holds_it_open():
    ec = aec.EchoCanceller(_Pass())
    far = _tone(300, 320, 16000, amp=3000.0)
    loud = _tone(1000, 320, 16000, amp=12000.0)       # piu' forte del riferimento: barge-in
    quiet = _tone(1000, 320, 16000, amp=200.0)
    assert _level(ec.process_aligned(loud.tobytes(), far.tobytes())) > 5000
    # il seguito della frase passa anche se piu' debole, per _GATE_OPEN_HOLD_MS
    assert _level(ec.process_aligned(quiet.tobytes(), far.tobytes())) > 100


def test_fixed_gain_applies_after_the_gate_decision():
    ec = aec.EchoCanceller(_Pass())
    ec.set_aligned_gain_db(18)
    x = _tone(1000, 320, 16000, amp=1000.0)
    out = ec.process_aligned(x.tobytes(), np.zeros(320, np.int16).tobytes())
    assert abs(_level(out) / _level(x.tobytes()) - 10 ** (18 / 20)) < 0.05


def test_parse_level_packets():
    seq, rms, opus, muted = parse_level(bytes([0xA5, 0x83, 1, 0, 0x10, 0x27, 0, 0]) + b"\x08xyz")
    assert (seq, rms, opus, muted) == (1, [10000, 0], b"\x08xyz", True)
    assert parse_level(bytes([0xA5, 0x01, 0, 0, 1, 2, 3, 4])) is None  # PCM, non livelli
    assert parse_aligned(bytes([0xA5, 0x03, 0, 0, 1, 2, 3, 4])) is None
