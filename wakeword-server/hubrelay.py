"""Client TCP verso il Voice Live Hub: una connessione = una sessione live.

Si apre solo quando l'orchestrator manda live_session_start con backend "hub" e si chiude a
fine sessione. Il wakeword-server manda al hub il mic dell'Atom gia' ripulito dall'AEC
(PCM 16 kHz) e riceve la voce del modello (PCM 24 kHz) piu' i controlli clear/hangup.
"""

import asyncio
import json
import logging
import struct
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("wakeword-server")

# --- Framing: copia minima di voice_live_hub/ipc.py del hub
# (hermes-jarvis/services/voice-live-hub). Tenerli allineati.
# Frame = kind: u8 | len: u32 big-endian | payload. Kind 1 = JSON, kind 2 = audio con
# payload id_len: u8 | call_id | PCM s16le mono.
JSON = 1
AUDIO = 2
MAX_FRAME = 1 << 20


def encode_json(msg: dict) -> bytes:
    payload = json.dumps(msg, separators=(",", ":")).encode()
    return struct.pack(">BI", JSON, len(payload)) + payload


def encode_audio(call_id: str, pcm: bytes) -> bytes:
    cid = call_id.encode()
    payload = bytes([len(cid)]) + cid + pcm
    return struct.pack(">BI", AUDIO, len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> Optional[tuple]:
    """(JSON, dict) oppure (AUDIO, (call_id, pcm)); None a fine stream."""
    try:
        head = await reader.readexactly(5)
    except asyncio.IncompleteReadError:
        return None
    kind, length = struct.unpack(">BI", head)
    if length > MAX_FRAME:
        raise ValueError(f"frame too large: {length}")
    payload = await reader.readexactly(length)
    if kind == JSON:
        return JSON, json.loads(payload)
    if kind == AUDIO:
        n = payload[0]
        return AUDIO, (payload[1:1 + n].decode(), payload[1 + n:])
    raise ValueError(f"unknown frame kind {kind}")


class HubUnavailable(Exception):
    """Hub spento, irraggiungibile o muto prima di accept/rejected."""


class HubRejected(Exception):
    """Il hub ha risposto rejected all'offer."""


class HubRelay:
    """Una sessione live verso il hub per un device."""

    CONNECT_TIMEOUT_S = 3.0
    ACCEPT_TIMEOUT_S = 5.0
    # Oltre questo backlog in uscita (~8 s di audio 16 kHz) il hub non sta leggendo:
    # si scarta l'audio invece di bloccare il loop WS del device.
    MAX_WRITE_BACKLOG = 256 * 1024

    def __init__(self, device_id: str, call_id: str):
        self.device_id = device_id
        self.call_id = call_id
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = False
        self._dropping = False

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._closed and not self._writer.is_closing()

    async def connect(self, host: str, port: int):
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), self.CONNECT_TIMEOUT_S)
        except Exception as e:  # OSError, timeout, ma anche OverflowError (porta fuori range)
            raise HubUnavailable(f"connect {host}:{port} failed: {e!r}") from e
        logger.info(f"[{self.device_id}] Hub connected {host}:{port} (call {self.call_id})")

    async def send_offer(self, offer: dict):
        """Manda l'offer e aspetta la risposta: ritorna su accept, eccezione altrimenti."""
        await self.send_json(offer)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ACCEPT_TIMEOUT_S
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise HubUnavailable("no accept from hub")
                frame = await asyncio.wait_for(read_frame(self._reader), remaining)
                if frame is None:
                    # Chiusura senza frame prima dell'accept (es. call_id duplicato sul hub):
                    # per contratto e' un rifiuto, come lato app
                    raise HubRejected("hub closed before accept")
                kind, data = frame
                if kind != JSON or not isinstance(data, dict):
                    continue
                mtype = data.get("type")
                if mtype == "accept" and data.get("call_id") == self.call_id:
                    return
                if mtype == "rejected":
                    # Qualunque rejected chiude, anche senza call_id
                    raise HubRejected(str(data.get("reason") or ""))
        except (HubRejected, HubUnavailable):
            raise
        except asyncio.TimeoutError as e:
            raise HubUnavailable("no accept from hub") from e
        except Exception as e:
            # ConnectionError, frame troncato (IncompleteReadError), frame malformato
            # (ValueError, IndexError, JSON non valido): hub inutilizzabile
            raise HubUnavailable(f"hub broken during offer: {e!r}") from e

    def send_audio(self, pcm: bytes):
        """Mic pulito 16 kHz verso il hub. Non blocca: con il hub fermo l'audio si scarta."""
        if not pcm or not self.is_connected:
            return
        if self._writer.transport.get_write_buffer_size() > self.MAX_WRITE_BACKLOG:
            if not self._dropping:
                logger.warning(f"[{self.device_id}] Hub not reading, dropping mic audio")
                self._dropping = True
            return
        self._dropping = False
        self._writer.write(encode_audio(self.call_id, pcm))

    async def send_json(self, msg: dict):
        if not self.is_connected:
            return
        try:
            self._writer.write(encode_json(msg))
            await asyncio.wait_for(self._writer.drain(), 2.0)
        except Exception as e:
            logger.warning(f"[{self.device_id}] Hub send_json error: {e!r}")

    def start(self, on_json: Callable[[dict], Awaitable[None]],
              on_audio: Callable[[bytes], None],
              on_closed: Callable[[], Awaitable[None]]):
        """Avvia il loop di ricezione (dopo l'accept)."""
        self._recv_task = asyncio.create_task(self._recv_loop(on_json, on_audio, on_closed))

    async def _recv_loop(self, on_json, on_audio, on_closed):
        try:
            while not self._closed:
                frame = await read_frame(self._reader)
                if frame is None:
                    break
                kind, data = frame
                if kind == AUDIO:
                    call_id, pcm = data
                    if call_id == self.call_id:
                        on_audio(pcm)
                elif isinstance(data, dict):
                    await on_json(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._closed:
                logger.warning(f"[{self.device_id}] Hub connection broken: {e!r}")
        if not self._closed:
            logger.info(f"[{self.device_id}] Hub closed the connection (call {self.call_id})")
            await on_closed()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        task = self._recv_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), 1.0)
            except Exception:
                pass
        logger.info(f"[{self.device_id}] Hub relay closed (call {self.call_id})")
