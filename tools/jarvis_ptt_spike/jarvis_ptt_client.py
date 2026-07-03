#!/usr/bin/env python3
"""
JARVIS PTT Spike Client — valida il protocollo /ws/audio dell'orchestrator
simulando il futuro client Galaxy Watch / telefono (modello "tap sulla testa").

Riproduce fedelmente la UX target:
  - TAP  (ENTER da idle)      → apre la connessione se serve (on-demand) + audio_start
  - parli                     → streaming PCM 16 kHz mono verso l'orchestrator
  - fine turno:
       * silenzio (VAD server) → l'orchestrator manda speech_end da solo, OPPURE
       * SECONDO TAP (ENTER)   → manda audio_flush ("chiudi e processa SUBITO")
  - risposta                  → decodifica i frame Opus TTS e li riproduce sullo speaker
  - multiturn / live session  → su trigger_listen riattiva il mic da solo (nessun tap)
  - connessione on-demand     → resta "calda" per --ttl secondi di inattività, poi chiude
                                (prima chiamata più lenta, successive sprint)

Uplink: PCM grezzo (codec="pcm") → nessun encoding Opus lato client.
Downlink: Opus 16 kHz mono → decodificato con opuslib e riprodotto.

USO
    export JARVIS_WS_URL=ws://<orchestrator-tailscale-ip>:<porta>
    export JARVIS_DEVICE_TOKEN=<DEVICE_API_TOKEN>      # se configurato sul server
    python jarvis_ptt_client.py                        # mic live (default)
    python jarvis_ptt_client.py --wav comando.wav      # invia un WAV 16k mono invece del mic

Comandi runtime (da tastiera):
    ENTER  → TAP (start turno / secondo tap = flush / barge-in durante la risposta)
    q ENTER→ esci

Dipendenze:  pip install websockets opuslib numpy sounddevice
             (sounddevice richiede PortAudio: apt install libportaudio2)

NB: questo device va poi configurato in dashboard con use_internal_speaker=true,
    altrimenti la TTS viene instradata su uno speaker HA e qui non arriva nulla.
"""

import argparse
import asyncio
import contextlib
import json
import os
import queue
import sys
import time
import wave

import numpy as np

try:
    import websockets
except ImportError:
    sys.exit("Manca 'websockets': pip install websockets")

try:
    import opuslib
except ImportError:
    sys.exit("Manca 'opuslib': pip install opuslib (serve per decodificare la TTS)")

# sounddevice è opzionale solo in --wav + --no-play, ma di norma serve
try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None


# ── Parametri audio (devono combaciare con l'orchestrator) ────────────────────
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320          # 20 ms @ 16 kHz  (PCM: 640 byte/frame)
FRAME_BYTES = FRAME_SAMPLES * 2
OPUS_MAX_SAMPLES = 960       # buffer massimo per la decode (fino a 60 ms)


# ── Stato "testa robot" (solo per feedback a schermo) ─────────────────────────
class Head:
    IDLE = "🤖 idle      "
    LISTEN = "👂 listening "
    THINK = "🤔 thinking  "
    SPEAK = "🗣️  speaking  "

    @staticmethod
    def show(state, extra=""):
        sys.stdout.write(f"\r{state} {extra:<50}")
        sys.stdout.flush()


class JarvisPTTClient:
    def __init__(self, url, device_id, token, ttl, fw, wav_path=None, play=True):
        self.base_url = url.rstrip("/")
        self.device_id = device_id.upper().strip()
        self.token = token
        self.ttl = ttl
        self.fw = fw
        self.wav_path = wav_path
        self.play = play and sd is not None

        self.ws = None
        self.connected = False
        self._recv_task = None
        self._quit = False

        # Fasi: idle → listening → thinking → speaking → idle
        self.phase = "idle"
        self.streaming = False        # True mentre inviamo audio all'orchestrator
        self._last_activity = time.monotonic()
        self._live_session = False

        # Coda audio in uscita (mic/wav → ws) e coda in ingresso (TTS → speaker)
        self._out_q: asyncio.Queue = asyncio.Queue()
        self._pb_q: queue.Queue = queue.Queue()   # thread-safe (letta dal callback PortAudio)
        self._pb_residual = np.zeros(0, dtype=np.int16)

        self._opus_dec = opuslib.Decoder(SAMPLE_RATE, 1)
        self._loop = None
        self._in_stream = None
        self._out_stream = None

    # ─────────────────────────────────────────────────────────────────────────
    # Connessione on-demand
    # ─────────────────────────────────────────────────────────────────────────
    async def ensure_connected(self):
        if self.ws is not None and self.connected:
            return
        url = f"{self.base_url}/ws/audio?device_id={self.device_id}"
        if self.token:
            url += f"&token={self.token}"
        t0 = time.time()
        Head.show(Head.IDLE, "connessione…")
        self.ws = await websockets.connect(
            url, max_size=2 ** 20, ping_interval=20, ping_timeout=10, close_timeout=5,
        )
        self.connected = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        await self._send_json({"type": "hello", "fw": self.fw})
        # Piccola attesa per welcome/config_update
        await asyncio.sleep(0.2)
        Head.show(Head.IDLE, f"connesso in {(time.time() - t0) * 1000:.0f}ms")

    async def close_connection(self, reason=""):
        self.streaming = False
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        self.ws = None
        self.connected = False
        self.phase = "idle"
        if reason:
            Head.show(Head.IDLE, f"disconnesso ({reason})")

    # ─────────────────────────────────────────────────────────────────────────
    # Invio
    # ─────────────────────────────────────────────────────────────────────────
    async def _send_json(self, obj):
        if self.ws and self.connected:
            with contextlib.suppress(Exception):
                await self.ws.send(json.dumps(obj))

    def _touch(self):
        self._last_activity = time.monotonic()

    # ─────────────────────────────────────────────────────────────────────────
    # TAP: il gesto unico dell'utente (sulla testa del robot)
    # ─────────────────────────────────────────────────────────────────────────
    async def on_tap(self):
        self._touch()
        if self.phase == "idle":
            await self.start_turn()
        elif self.phase == "listening":
            # Secondo tap → chiudi e processa subito
            Head.show(Head.THINK, "flush (secondo tap)")
            await self._send_json({"type": "audio_flush"})
            self.streaming = False
        elif self.phase in ("thinking", "speaking"):
            # Tap durante la risposta = barge-in / stop
            Head.show(Head.IDLE, "stop (barge-in)")
            await self._send_json({"type": "speaker_stop"})
            self.streaming = False
            self.phase = "idle"

    async def start_turn(self):
        await self.ensure_connected()
        self.phase = "listening"
        # codec=pcm → l'orchestrator si aspetta frame da 640 byte
        await self._send_json({"type": "audio_start", "codec": "pcm"})
        # lo streaming parte solo su 'ready' (vedi _on_text)

    # ─────────────────────────────────────────────────────────────────────────
    # Ricezione dall'orchestrator
    # ─────────────────────────────────────────────────────────────────────────
    async def _recv_loop(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, (bytes, bytearray)):
                    self._on_binary(bytes(msg))
                else:
                    await self._on_text(msg)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            Head.show(Head.IDLE, f"recv error: {e}")
        finally:
            self.connected = False
            self.ws = None

    async def _on_text(self, raw):
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = m.get("type", "")
        self._touch()

        if t == "welcome":
            pass
        elif t == "config_update":
            spk = m.get("speaker_type")
            if spk and spk != "internal":
                Head.show(Head.IDLE, f"⚠️  speaker_type={spk} (imposta use_internal_speaker=true!)")
        elif t == "ready":
            # Server pronto → inizia a inviare audio
            self.streaming = True
            self.phase = "listening"
            Head.show(Head.LISTEN, "parla pure…")
            if self.wav_path:
                asyncio.create_task(self._feed_wav())
        elif t == "speech_end":
            self.streaming = False
            self.phase = "thinking"
            Head.show(Head.THINK)
        elif t == "tts_start":
            self.phase = "speaking"
            Head.show(Head.SPEAK)
        elif t == "tts_done":
            self.phase = "idle"
            Head.show(Head.IDLE, "pronto — TAP per parlare")
        elif t == "trigger_listen":
            # Multiturn / prossimo turno live session → riattiva mic senza tap
            Head.show(Head.LISTEN, "follow-up…")
            await self.start_turn()
        elif t == "live_session_start":
            self._live_session = True
            Head.show(Head.LISTEN, "🎙️ live session ON")
        elif t == "live_session_end":
            self._live_session = False
            Head.show(Head.IDLE, "🎙️ live session OFF")
        elif t == "ping":
            await self._send_json({"type": "pong"})
        elif t == "error":
            Head.show(Head.IDLE, f"server error: {m.get('msg') or m.get('message')}")

    def _on_binary(self, data):
        # Frame Opus TTS → decode → coda di playback
        try:
            pcm = self._opus_dec.decode(data, OPUS_MAX_SAMPLES)
        except Exception:
            return
        arr = np.frombuffer(pcm, dtype=np.int16)
        if self.play:
            self._pb_q.put(arr)
        self._touch()

    # ─────────────────────────────────────────────────────────────────────────
    # Sorgenti audio → coda in uscita
    # ─────────────────────────────────────────────────────────────────────────
    def _mic_callback(self, indata, frames, time_info, status):
        # Gira nel thread PortAudio: inoltra i frame all'event loop solo se stiamo streammando
        if self.streaming and self._loop is not None:
            self._loop.call_soon_threadsafe(self._out_q.put_nowait, bytes(indata))

    async def _feed_wav(self):
        """Invia un WAV (16 kHz mono 16-bit) a ritmo realtime, poi flush."""
        with wave.open(self.wav_path, "rb") as w:
            if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                Head.show(Head.IDLE, "⚠️  WAV deve essere 16kHz mono 16-bit")
            while self.streaming and not self._quit:
                chunk = w.readframes(FRAME_SAMPLES)
                if not chunk:
                    break
                if len(chunk) < FRAME_BYTES:
                    chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
                await self._out_q.put(chunk)
                await asyncio.sleep(0.02)
        # WAV finito → chiudi il turno subito (il file non ha silenzio di coda)
        if self.streaming:
            await self._send_json({"type": "audio_flush"})
            self.streaming = False

    async def _sender_loop(self):
        sent = 0
        while not self._quit:
            data = await self._out_q.get()
            if data is None:
                continue
            if self.ws and self.connected and self.streaming:
                # Diagnostica mic: stampa RMS/peak dei frame inviati.
                # Se RMS≈0 il microfono non sta catturando nulla (tipico in VM/RustDesk).
                sent += 1
                if sent % 50 == 1:
                    a = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(a * a)))
                    peak = float(np.max(np.abs(a)))
                    tag = " ⚠️ MIC MUTO?" if rms < 5 else ""
                    Head.show(Head.LISTEN, f"mic frame#{sent} rms={rms:.0f} peak={peak:.0f}{tag}")
                with contextlib.suppress(Exception):
                    await self.ws.send(data)

    # ─────────────────────────────────────────────────────────────────────────
    # Playback (callback PortAudio che svuota la coda TTS)
    # ─────────────────────────────────────────────────────────────────────────
    def _play_callback(self, outdata, frames, time_info, status):
        buf = self._pb_residual
        while len(buf) < frames:
            try:
                buf = np.concatenate([buf, self._pb_q.get_nowait()])
            except queue.Empty:
                break
        n = min(len(buf), frames)
        outdata[:n, 0] = buf[:n]
        if n < frames:
            outdata[n:, 0] = 0
        self._pb_residual = buf[n:]

    # ─────────────────────────────────────────────────────────────────────────
    # Loop TTL: chiude la connessione dopo inattività (on-demand warm window)
    # ─────────────────────────────────────────────────────────────────────────
    async def _ttl_loop(self):
        while not self._quit:
            await asyncio.sleep(1.0)
            if not self.connected:
                continue
            if self._live_session or self.phase != "idle":
                self._touch()
                continue
            if time.monotonic() - self._last_activity > self.ttl:
                await self.close_connection(reason=f"idle > {self.ttl}s")

    # ─────────────────────────────────────────────────────────────────────────
    # Input da tastiera (simula il tap)
    # ─────────────────────────────────────────────────────────────────────────
    async def _input_loop(self):
        while not self._quit:
            line = await self._loop.run_in_executor(None, sys.stdin.readline)
            if line == "":  # EOF
                self._quit = True
                break
            cmd = line.strip().lower()
            if cmd in ("q", "quit", "exit"):
                self._quit = True
                break
            await self.on_tap()

    # ─────────────────────────────────────────────────────────────────────────
    async def run(self):
        self._loop = asyncio.get_running_loop()

        if not self.wav_path and sd is None:
            sys.exit("sounddevice non disponibile: usa --wav oppure installa sounddevice+PortAudio")

        # Mic input (solo se non in modalità WAV)
        if not self.wav_path:
            self._in_stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=FRAME_SAMPLES, callback=self._mic_callback,
            )
            self._in_stream.start()

        # Speaker output
        if self.play:
            self._out_stream = sd.OutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=FRAME_SAMPLES, callback=self._play_callback,
            )
            self._out_stream.start()

        print(f"JARVIS PTT spike — device={self.device_id}  ttl={self.ttl}s  "
              f"src={'WAV:' + self.wav_path if self.wav_path else 'mic'}")
        print("TAP = premi ENTER  |  q = esci\n")
        Head.show(Head.IDLE, "pronto — TAP per parlare")

        tasks = [
            asyncio.create_task(self._sender_loop()),
            asyncio.create_task(self._ttl_loop()),
            asyncio.create_task(self._input_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self._quit = True
            for t in tasks:
                t.cancel()
            await self.close_connection()
            if self._in_stream:
                self._in_stream.stop(); self._in_stream.close()
            if self._out_stream:
                self._out_stream.stop(); self._out_stream.close()
            print("\nbye.")


def main():
    ap = argparse.ArgumentParser(description="JARVIS PTT spike client")
    ap.add_argument("--url", default=os.getenv("JARVIS_WS_URL", "ws://localhost:5000"),
                    help="Base WS dell'orchestrator, es. ws://100.x.y.z:5000 (senza /ws/audio)")
    ap.add_argument("--device-id", default=os.getenv("JARVIS_DEVICE_ID", "AABBCCDDEE01"),
                    help="device_id (formato MAC uppercase). Sconosciuto = auto-registrato dal server")
    ap.add_argument("--token", default=os.getenv("JARVIS_DEVICE_TOKEN", ""),
                    help="DEVICE_API_TOKEN, se configurato sul server")
    ap.add_argument("--ttl", type=float, default=600.0,
                    help="Secondi di inattività prima di chiudere la connessione (default 600 = 10 min)")
    ap.add_argument("--fw", default="python-ptt-spike-1.0", help="Stringa firmware nell'hello")
    ap.add_argument("--wav", default=None, help="Invia un file WAV 16kHz mono invece del microfono")
    ap.add_argument("--no-play", action="store_true", help="Non riprodurre la TTS (solo log)")
    args = ap.parse_args()

    client = JarvisPTTClient(
        url=args.url, device_id=args.device_id, token=args.token,
        ttl=args.ttl, fw=args.fw, wav_path=args.wav, play=not args.no_play,
    )
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
