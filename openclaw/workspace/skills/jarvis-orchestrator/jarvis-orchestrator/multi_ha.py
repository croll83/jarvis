"""
JARVIS Multi-Home Assistant Client
- Gestisce connessioni a multiple istanze Home Assistant
- Carica configurazione dal database
- Supporta WebSocket per performance e REST come fallback
- Entity maps caricate dal database (non più da file JSON)
"""

import aiohttp
import asyncio
import json
import logging
from typing import Dict, List, Optional, Tuple, Any

import websockets

from database import (
    get_all_locations, get_location, log_event,
    increment_device_failure, reset_device_failure,
    get_entity_map
)

logger = logging.getLogger("JARVIS_MULTI_HA")


class HomeAssistantClient:
    """Client WebSocket/REST per una singola istanza Home Assistant."""

    def __init__(self, location_id: str, hass_url: str, hass_token: str, timeout: float = 10.0):
        self.location_id = location_id
        self.hass_url = hass_url
        self.hass_token = hass_token
        self.timeout = timeout
        self.ws = None
        self.msg_id = 0
        self._lock = asyncio.Lock()
        self._ws_url = hass_url.replace("http", "ws") + "/api/websocket"

    async def connect(self) -> bool:
        """Stabilisce connessione WebSocket con HA."""
        if self.ws and not self.ws.closed:
            return True

        if not self.hass_token:
            logger.warning(f"[{self.location_id}] No token configured, skipping WebSocket")
            return False

        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(self._ws_url),
                timeout=self.timeout
            )

            # Auth flow
            await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)  # auth_required
            await self.ws.send(json.dumps({
                "type": "auth",
                "access_token": self.hass_token
            }))

            auth_result = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=self.timeout))
            if auth_result.get("type") != "auth_ok":
                logger.error(f"[{self.location_id}] HA Auth failed: {auth_result}")
                return False

            logger.info(f"[{self.location_id}] Home Assistant WebSocket connected")
            return True

        except asyncio.TimeoutError:
            logger.error(f"[{self.location_id}] HA WebSocket connection timeout")
            self.ws = None
            return False
        except Exception as e:
            logger.error(f"[{self.location_id}] HA WebSocket connection failed: {e}")
            self.ws = None
            return False

    async def call_service(self, domain: str, service: str, service_data: dict) -> Tuple[bool, str]:
        """Chiama un servizio HA via WebSocket, con fallback REST."""
        async with self._lock:
            entity_id = service_data.get("entity_id", "unknown")

            try:
                if not await self.connect():
                    # Fallback a REST se WebSocket fallisce
                    return await self._call_service_rest(domain, service, service_data)

                self.msg_id += 1
                await self.ws.send(json.dumps({
                    "id": self.msg_id,
                    "type": "call_service",
                    "domain": domain,
                    "service": service,
                    "service_data": service_data
                }))

                result = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=self.timeout))

                if result.get("success"):
                    log_event("HASS", f"[{self.location_id}] Servizio {domain}.{service} eseguito su {entity_id}")
                    reset_device_failure(entity_id)
                    return True, "OK"
                else:
                    error_msg = result.get("error", {}).get("message", "Unknown error")
                    increment_device_failure(entity_id, error_msg)
                    log_event("HARDWARE_ERROR", f"[{self.location_id}] Fallito comando su {entity_id}: {error_msg}")
                    return False, error_msg

            except asyncio.TimeoutError:
                increment_device_failure(entity_id, "Timeout")
                log_event("HARDWARE_ERROR", f"[{self.location_id}] Timeout comando su {entity_id}")
                self.ws = None  # Force reconnect
                return False, "Timeout"

            except Exception as e:
                increment_device_failure(entity_id, str(e))
                log_event("HARDWARE_ERROR", f"[{self.location_id}] Errore comando su {entity_id}: {e}")
                self.ws = None  # Force reconnect
                return False, str(e)

    async def _call_service_rest(self, domain: str, service: str, service_data: dict) -> Tuple[bool, str]:
        """Fallback REST API per Home Assistant."""
        if not self.hass_token:
            return False, f"No token configured for location {self.location_id}"

        url = f"{self.hass_url}/api/services/{domain}/{service}"
        headers = {
            "Authorization": f"Bearer {self.hass_token}",
            "Content-Type": "application/json"
        }
        entity_id = service_data.get("entity_id", "unknown")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=service_data, headers=headers, timeout=self.timeout) as resp:
                    if resp.status == 200:
                        log_event("HASS", f"[{self.location_id}][REST] Servizio {domain}.{service} eseguito su {entity_id}")
                        reset_device_failure(entity_id)
                        return True, "OK"
                    else:
                        err_body = await resp.text()
                        increment_device_failure(entity_id, f"HTTP {resp.status}: {err_body}")
                        log_event("HARDWARE_ERROR", f"[{self.location_id}][REST] Fallito comando su {entity_id}: {err_body}")
                        return False, f"Status {resp.status}"

        except Exception as e:
            increment_device_failure(entity_id, str(e))
            log_event("HARDWARE_ERROR", f"[{self.location_id}][REST] Eccezione su {entity_id}: {e}")
            return False, str(e)

    async def health_check(self) -> Tuple[bool, float]:
        """Verifica lo stato di HA e ritorna latenza in ms."""
        if not self.hass_token:
            return False, 0

        url = f"{self.hass_url}/api/"
        headers = {"Authorization": f"Bearer {self.hass_token}"}

        try:
            import time
            start = time.time()
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=self.timeout) as resp:
                    latency = (time.time() - start) * 1000
                    return resp.status == 200, latency
        except Exception:
            return False, 0

    async def get_state(self, entity_id: str) -> Optional[dict]:
        """
        Fetch a single entity state from HA via GET /api/states/<entity_id>.
        Much lighter than get_states_bulk() for individual entity lookups.

        Returns:
            {"state": str, "attributes": dict, "last_changed": str} or None
        """
        if not self.hass_token:
            return None

        url = f"{self.hass_url}/api/states/{entity_id}"
        headers = {"Authorization": f"Bearer {self.hass_token}"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status == 404:
                        logger.debug(f"[{self.location_id}] Entity {entity_id} not found")
                        return None
                    if resp.status != 200:
                        logger.error(f"[{self.location_id}] GET /api/states/{entity_id} returned {resp.status}")
                        return None

                    s = await resp.json()
                    return {
                        "state": s.get("state"),
                        "attributes": s.get("attributes", {}),
                        "last_changed": s.get("last_changed"),
                    }

        except Exception as e:
            logger.error(f"[{self.location_id}] get_state({entity_id}) error: {e}")
            return None

    async def get_states_bulk(self, entity_ids: List[str] = None) -> Dict[str, dict]:
        """
        Fetch all entity states from HA via GET /api/states in a single call.
        Optionally filter locally by entity_ids list.

        Returns:
            {entity_id: {"state": str, "attributes": dict, "last_changed": str}}
        """
        if not self.hass_token:
            logger.warning(f"[{self.location_id}] No token for bulk state fetch")
            return {}

        url = f"{self.hass_url}/api/states"
        headers = {"Authorization": f"Bearer {self.hass_token}"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status != 200:
                        logger.error(f"[{self.location_id}] GET /api/states returned {resp.status}")
                        return {}

                    all_states = await resp.json()

            # Build lookup dict
            entity_id_set = set(entity_ids) if entity_ids else None
            result = {}
            for s in all_states:
                eid = s.get("entity_id", "")
                if entity_id_set and eid not in entity_id_set:
                    continue
                result[eid] = {
                    "state": s.get("state"),
                    "attributes": s.get("attributes", {}),
                    "last_changed": s.get("last_changed"),
                }

            logger.debug(f"[{self.location_id}] Bulk state fetch: {len(result)} entities"
                         f" (filtered from {len(all_states)} total)")
            return result

        except Exception as e:
            logger.error(f"[{self.location_id}] Bulk state fetch error: {e}")
            return {}

    async def call_service_bulk(
        self, domain: str, service: str, entity_ids: List[str],
        service_data: dict = None
    ) -> Tuple[bool, str]:
        """
        Execute a service on multiple entities in a single HA call.
        HA natively supports entity_id as a list in service_data.

        Returns:
            (success, message)
        """
        data = dict(service_data) if service_data else {}
        data["entity_id"] = entity_ids

        async with self._lock:
            try:
                if not await self.connect():
                    return await self._call_service_rest(domain, service, data)

                self.msg_id += 1
                await self.ws.send(json.dumps({
                    "id": self.msg_id,
                    "type": "call_service",
                    "domain": domain,
                    "service": service,
                    "service_data": data
                }))

                result = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=self.timeout))

                if result.get("success"):
                    log_event("HASS",
                              f"[{self.location_id}] Bulk {domain}.{service} su {len(entity_ids)} entity")
                    for eid in entity_ids:
                        reset_device_failure(eid)
                    return True, f"OK — {len(entity_ids)} entities"
                else:
                    error_msg = result.get("error", {}).get("message", "Unknown error")
                    log_event("HARDWARE_ERROR",
                              f"[{self.location_id}] Bulk {domain}.{service} fallito: {error_msg}")
                    return False, error_msg

            except asyncio.TimeoutError:
                log_event("HARDWARE_ERROR",
                          f"[{self.location_id}] Bulk {domain}.{service} timeout")
                self.ws = None
                return False, "Timeout"

            except Exception as e:
                log_event("HARDWARE_ERROR",
                          f"[{self.location_id}] Bulk {domain}.{service} errore: {e}")
                self.ws = None
                return False, str(e)

    async def close(self):
        """Chiude la connessione WebSocket."""
        if self.ws:
            await self.ws.close()
            self.ws = None


class MultiHomeAssistant:
    """
    Gestisce connessioni a multiple istanze Home Assistant.
    Carica configurazione dal database.
    """

    def __init__(self):
        self.clients: Dict[str, HomeAssistantClient] = {}
        self.entity_maps: Dict[str, dict] = {}
        self._loaded = False

    def _load_from_db(self):
        """Carica tutte le location abilitate dal DB."""
        locations = get_all_locations(enabled_only=True)

        for loc in locations:
            # Skip location senza token configurato (non funzionante)
            if not loc.hass_token:
                logger.info(f"Skipping location '{loc.id}' - no token configured")
                continue

            # Timeout più alto per location remote (via Tailscale)
            import config as _cfg
            is_remote = any(p in loc.hass_url for p in _cfg.TAILSCALE_PATTERNS)
            timeout = _cfg.TAILSCALE_TIMEOUT_REMOTE if is_remote else _cfg.TAILSCALE_TIMEOUT_LOCAL

            self.clients[loc.id] = HomeAssistantClient(
                location_id=loc.id,
                hass_url=loc.hass_url,
                hass_token=loc.hass_token,
                timeout=timeout
            )

            # Carica entity map dal database
            entity_map = get_entity_map(loc.id)
            if entity_map:
                self.entity_maps[loc.id] = entity_map
                logger.info(f"Loaded entity map for '{loc.id}' from database")
            else:
                logger.info(f"No entity map found in DB for '{loc.id}'")

        self._loaded = True
        if self.clients:
            logger.info(f"Loaded {len(self.clients)} HA locations: {list(self.clients.keys())}")
        else:
            logger.info("No Home Assistant locations configured - running in AI-only mode")

    def ensure_loaded(self):
        """Assicura che la configurazione sia caricata."""
        if not self._loaded:
            self._load_from_db()

    def reload(self):
        """Ricarica configurazione dal DB (chiamato dopo modifiche da dashboard)."""
        # Chiudi connessioni esistenti
        for client in self.clients.values():
            asyncio.create_task(client.close())

        self.clients.clear()
        self.entity_maps.clear()
        self._loaded = False
        self._load_from_db()
        logger.info("Multi-HA configuration reloaded")

    def get_location_ids(self) -> list:
        """Ritorna la lista di location ID disponibili."""
        self.ensure_loaded()
        return list(self.clients.keys())

    def get_client(self, location_id: str) -> Optional[HomeAssistantClient]:
        """Ritorna il client per una specifica location."""
        self.ensure_loaded()
        return self.clients.get(location_id)

    async def call_service(self, location_id: str, domain: str, service: str, data: dict) -> Tuple[bool, str]:
        """Chiama un servizio su una specifica location."""
        self.ensure_loaded()
        client = self.clients.get(location_id)
        if not client:
            return False, f"Location '{location_id}' non configurata o disabilitata"
        return await client.call_service(domain, service, data)

    def get_entity_map(self, location_id: str) -> dict:
        """Restituisce l'entity map per una location."""
        self.ensure_loaded()
        return self.entity_maps.get(location_id, {})

    def get_all_entity_maps(self) -> Dict[str, dict]:
        """Restituisce tutte le entity maps (per il router)."""
        self.ensure_loaded()
        return self.entity_maps

    async def get_state(self, location_id: str, entity_id: str) -> Optional[dict]:
        """Fetch a single entity state for a location (lightweight)."""
        self.ensure_loaded()
        client = self.clients.get(location_id)
        if not client:
            return None
        return await client.get_state(entity_id)

    async def get_states_bulk(self, location_id: str,
                              entity_ids: List[str] = None) -> Dict[str, dict]:
        """Fetch bulk entity states for a location."""
        self.ensure_loaded()
        client = self.clients.get(location_id)
        if not client:
            return {}
        return await client.get_states_bulk(entity_ids)

    async def call_service_bulk(
        self, location_id: str, domain: str, service: str,
        entity_ids: List[str], service_data: dict = None
    ) -> Tuple[bool, str]:
        """Execute a service on multiple entities for a location."""
        self.ensure_loaded()
        client = self.clients.get(location_id)
        if not client:
            return False, f"Location '{location_id}' non configurata o disabilitata"
        return await client.call_service_bulk(domain, service, entity_ids, service_data)

    async def health_check_all(self) -> Dict[str, dict]:
        """Esegue health check su tutte le location."""
        self.ensure_loaded()
        results = {}

        for location_id, client in self.clients.items():
            is_healthy, latency = await client.health_check()
            results[location_id] = {
                "healthy": is_healthy,
                "latency_ms": round(latency, 1),
                "url": client.hass_url
            }

        return results

    async def health_check(self, location_id: str) -> Tuple[bool, float]:
        """Esegue health check su una specifica location."""
        self.ensure_loaded()
        client = self.clients.get(location_id)
        if not client:
            return False, 0
        return await client.health_check()


# Singleton
multi_ha = MultiHomeAssistant()
