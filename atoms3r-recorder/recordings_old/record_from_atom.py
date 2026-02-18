#!/usr/bin/env python3
"""
record_from_atom.py — Registra file WAV dall'AtomS3R via TCP.

Zero dipendenze esterne (solo stdlib Python).
Funziona col firmware atoms3r-recorder.

Uso:
  python3 record_from_atom.py <nome> [--port 5555] [--output-dir .]

Flusso:
  1. Avvia TCP server, attende connessione AtomS3R
  2. [INVIO] → manda REC → AtomS3R emette beep e streamma PCM
  3. [INVIO] → manda STOP → salva WAV (16kHz/Mono/16-bit PCM)
  4. Torna al punto 2 (loop fino a Ctrl+C)
"""

import argparse
import os
import selectors
import socket
import struct
import sys
import threading
import time
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (must match firmware)
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
FRAME_MAGIC = b"\xAA\x55"

# Trim iniziale: il beep (150ms) viene captato dal microfono.
# Tagliamo 300ms per sicurezza.
TRIM_MS = 300
TRIM_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * TRIM_MS / 1000)  # 9600


def save_wav(pcm_data: bytes, filepath: Path) -> float:
    """Save raw PCM as WAV. Returns duration in seconds."""
    with wave.open(str(filepath), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    return len(pcm_data) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)


class Recorder:
    def __init__(self, name: str, output_dir: Path):
        self.name = name
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.conn: socket.socket | None = None
        self.recording = False
        self.pcm_chunks: list[bytes] = []
        self.recv_buf = b""
        self._lock = threading.Lock()

    def wait_connection(self, server_sock: socket.socket):
        """Block until AtomS3R connects."""
        print("\n[*] In attesa di connessione dall'AtomS3R...")
        conn, addr = server_sock.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[+] AtomS3R connesso da {addr[0]}:{addr[1]}")
        self.conn = conn

    def send_command(self, cmd: str):
        """Send a text command to the device."""
        if self.conn:
            try:
                self.conn.sendall((cmd + "\n").encode())
            except OSError:
                pass

    def recv_loop(self):
        """Background thread: receive data from AtomS3R and dispatch."""
        while self.conn:
            try:
                data = self.conn.recv(4096)
                if not data:
                    print("\n[!] Connessione chiusa dall'AtomS3R")
                    self.conn = None
                    break
                self._process_data(data)
            except OSError:
                self.conn = None
                break

    def _process_data(self, data: bytes):
        """Parse incoming data: STATUS lines or audio frames."""
        with self._lock:
            self.recv_buf += data

        while True:
            with self._lock:
                buf = self.recv_buf

            if len(buf) == 0:
                break

            # Check for STATUS text line
            if buf[0:1] == b"S":
                nl = buf.find(b"\n")
                if nl < 0:
                    break  # incomplete line
                line = buf[:nl].decode("utf-8", errors="replace")
                with self._lock:
                    self.recv_buf = buf[nl + 1:]
                if line.startswith("STATUS:"):
                    status = line[7:]
                    print(f"[*] Device status: {status}")
                continue

            # Check for audio frame: [0xAA 0x55] [uint16_le len] [pcm_data]
            if len(buf) < 4:
                break

            if buf[0] == 0xAA and buf[1] == 0x55:
                pcm_bytes = struct.unpack_from("<H", buf, 2)[0]
                total_frame = 4 + pcm_bytes
                if len(buf) < total_frame:
                    break  # incomplete frame
                pcm_data = buf[4:total_frame]
                with self._lock:
                    self.recv_buf = buf[total_frame:]
                    if self.recording:
                        self.pcm_chunks.append(pcm_data)
                continue

            # Unknown byte — skip it
            with self._lock:
                self.recv_buf = buf[1:]

    def start_recording(self):
        """Send REC, start accumulating audio."""
        with self._lock:
            self.pcm_chunks = []
            self.recording = True
        self.send_command("REC")
        print("[*] REC inviato (beep in arrivo)...")

    def stop_and_save(self) -> str | None:
        """Send STOP, save accumulated audio as WAV."""
        self.send_command("STOP")
        with self._lock:
            self.recording = False
            pcm_data = b"".join(self.pcm_chunks)
            self.pcm_chunks = []

        if len(pcm_data) == 0:
            print("[!] Nessun audio ricevuto, file non salvato")
            return None

        # Taglia i primi 300ms (beep captato dal microfono)
        if len(pcm_data) > TRIM_BYTES:
            pcm_data = pcm_data[TRIM_BYTES:]
        else:
            print("[!] Registrazione troppo corta, nulla da salvare dopo il trim")
            return None

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{self.name}-{timestamp}.wav"
        filepath = self.output_dir / filename

        duration = save_wav(pcm_data, filepath)
        print(f"[+] Salvato: {filepath} ({duration:.1f}s, {len(pcm_data)} bytes, trimmed {TRIM_MS}ms)")
        return str(filepath)


def main():
    parser = argparse.ArgumentParser(
        description="Registra WAV dall'AtomS3R via TCP",
    )
    parser.add_argument("nome", help="Nome base per i file (es: giorgio)")
    parser.add_argument("--port", type=int, default=5555,
                        help="Porta TCP (default 5555)")
    parser.add_argument("--output-dir", default=".",
                        help="Cartella output (default: corrente)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    recorder = Recorder(args.nome, output_dir)

    # Create TCP server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.port))
    server.listen(1)

    print(f"[*] TCP server su 0.0.0.0:{args.port}")
    print(f"[*] File: {args.nome}-<timestamp>.wav")
    print(f"[*] Formato: {SAMPLE_RATE}Hz, Mono, 16-bit PCM")
    print(f"[*] Output: {output_dir.resolve()}")
    print("[*] Ctrl+C per uscire")

    try:
        while True:
            recorder.wait_connection(server)

            # Start receive thread
            recv_thread = threading.Thread(target=recorder.recv_loop, daemon=True)
            recv_thread.start()

            # Wait a moment for STATUS:connected
            time.sleep(0.5)

            print("\n" + "=" * 60)
            print(f"  REGISTRAZIONE VOCALE: {args.nome}")
            print("=" * 60)

            count = 0
            while recorder.conn:
                count += 1
                print(f"\n--- Registrazione #{count} ---")
                print("[*] Premi INVIO per avviare la registrazione...")

                try:
                    input()
                except EOFError:
                    break

                if not recorder.conn:
                    print("[!] Connessione persa, riconnessione...")
                    break

                recorder.start_recording()
                print("[*] REGISTRAZIONE IN CORSO... (premi INVIO per salvare)")

                try:
                    input()
                except EOFError:
                    break

                recorder.stop_and_save()

            print("[*] Connessione chiusa, in attesa di nuova connessione...")

    except KeyboardInterrupt:
        print("\n[*] Ctrl+C — uscita.")
    finally:
        if recorder.conn:
            recorder.conn.close()
        server.close()


if __name__ == "__main__":
    main()
