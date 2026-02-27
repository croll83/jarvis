"""On-demand WebSocket relay to the orchestrator VPS.

Opens a WS connection to the VPS when a wake word is detected,
relays audio and control messages bidirectionally, and closes
the connection after tts_done. Zero bandwidth when idle.
"""

import asyncio
import json
import logging
from typing import Optional, Callable, Awaitable

import websockets
from websockets.asyncio.client import ClientConnection

from config import ORCHESTRATOR_WS_URL, DEVICE_API_TOKEN

logger = logging.getLogger("wakeword-server")


class OrchestratorRelay:
    """Manages a single on-demand WS session to the VPS orchestrator for one device."""

    def __init__(self, device_id: str,
                 on_vps_message: Callable[[str | bytes], Awaitable[None]]):
        """
        Args:
            device_id: MAC address of the originating device.
            on_vps_message: callback for every message received from VPS
                            (text JSON or binary TTS Opus).
        """
        self.device_id = device_id
        self._on_vps_message = on_vps_message
        self._ws: Optional[ClientConnection] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()  # Serializza write per evitare drain concorrenti

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    async def connect(self):
        """Open WS to VPS orchestrator. Blocks until connected."""
        if self.is_connected:
            return

        url = f"{ORCHESTRATOR_WS_URL}?device_id={self.device_id}"
        if DEVICE_API_TOKEN:
            url += f"&token={DEVICE_API_TOKEN}"

        logger.info(f"[{self.device_id}] Relay connecting to {ORCHESTRATOR_WS_URL}")
        try:
            self._ws = await websockets.connect(
                url,
                ping_interval=None,  # Disabilitato: il relay è attivo solo durante sessioni, no keepalive necessario
                close_timeout=5,
                max_size=2**20,  # 1 MB
            )
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info(f"[{self.device_id}] Relay connected")
        except Exception as e:
            logger.error(f"[{self.device_id}] Relay connect failed: {e}")
            self._ws = None
            raise

    async def _recv_loop(self):
        """Forward all VPS messages to the device callback."""
        try:
            async for message in self._ws:
                await self._on_vps_message(message)
        except websockets.ConnectionClosed:
            logger.info(f"[{self.device_id}] Relay VPS connection closed")
        except Exception as e:
            logger.error(f"[{self.device_id}] Relay recv error: {e}")
        finally:
            self._ws = None

    async def send_text(self, text: str):
        """Send a JSON text message to VPS."""
        if not self.is_connected:
            logger.warning(f"[{self.device_id}] Relay send_text but not connected")
            return
        try:
            async with self._write_lock:
                await self._ws.send(text)
        except Exception as e:
            logger.error(f"[{self.device_id}] Relay send_text error: {e}")

    async def send_binary(self, data: bytes):
        """Send binary Opus data to VPS."""
        if not self.is_connected:
            return
        try:
            async with self._write_lock:
                await self._ws.send(data)
        except Exception as e:
            logger.error(f"[{self.device_id}] Relay send_binary error: {e}")

    async def close(self):
        """Close the relay connection."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info(f"[{self.device_id}] Relay closed")
