"""
JARVIS Event Bus
- Gestione connessioni SSE per dashboard admin
- Pattern pub/sub per eventi in tempo reale
- Heartbeat automatico per keep-alive
"""

import asyncio
import json
import time
import logging
from typing import Set, Dict, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("JARVIS_EVENTS")


@dataclass
class SSEClient:
    """Rappresenta un client SSE connesso."""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=50))
    connected_at: float = field(default_factory=time.time)
    user_id: Optional[int] = None


class EventBus:
    """
    Bus eventi globale per push real-time alla dashboard.
    Gestisce multiple connessioni SSE e distribuisce eventi a tutti i client.
    """

    def __init__(self):
        self._clients: Set[SSEClient] = set()
        self._lock = asyncio.Lock()

    async def connect(self, user_id: Optional[int] = None) -> SSEClient:
        """Registra un nuovo client SSE."""
        client = SSEClient(user_id=user_id)
        async with self._lock:
            self._clients.add(client)
        logger.info(f"SSE client connesso (user_id={user_id}). Totale: {len(self._clients)}")
        return client

    async def disconnect(self, client: SSEClient):
        """Rimuove un client SSE."""
        async with self._lock:
            self._clients.discard(client)
        logger.info(f"SSE client disconnesso. Totale: {len(self._clients)}")

    async def publish(self, event_type: str, data: Dict[str, Any]):
        """Pubblica un evento a tutti i client connessi."""
        if not self._clients:
            return

        message = {
            "type": event_type,
            "data": data,
            "timestamp": time.time()
        }

        disconnected = set()
        async with self._lock:
            for client in self._clients:
                try:
                    client.queue.put_nowait(message)
                except asyncio.QueueFull:
                    # Client troppo lento, rimuovi
                    disconnected.add(client)
                    logger.debug(f"SSE client rimosso (queue piena)")

            self._clients -= disconnected

    @property
    def client_count(self) -> int:
        return len(self._clients)


def publish_sync(event_type: str, data: Dict[str, Any]):
    """
    Pubblica un evento in modo sincrono (fire-and-forget).
    Usato da codice sincrono come log_event() in database.py.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(event_bus.publish(event_type, data))
    except RuntimeError:
        pass  # Nessun event loop attivo, ignora


# Istanza globale
event_bus = EventBus()
