"""
Service Health Status Tracker for JARVIS Graceful Degradation.

Tracks the health of external services (Ollama, Whisper, Home Assistant, Openclaw)
and provides status information to the router for intelligent responses when
services are offline.

Supports multi-location Home Assistant instances.
"""

import asyncio
import aiohttp
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum

import config
from database import get_all_locations

logger = logging.getLogger("JARVIS_HEALTH")


class ServiceState(Enum):
    """Stato di un servizio."""
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"  # Funziona ma lento o con errori parziali
    UNKNOWN = "unknown"


@dataclass
class ServiceHealth:
    """Health info per un singolo servizio."""
    state: ServiceState = ServiceState.UNKNOWN
    last_check: float = 0.0
    last_success: float = 0.0
    last_error: Optional[str] = None
    response_time_ms: float = 0.0
    consecutive_failures: int = 0


class ServiceStatus:
    """
    Singleton per tracking stato servizi JARVIS.

    Servizi monitorati:
    - ollama_router: Qwen (routing) - CRITICO
    - whisper: STT - IMPORTANTE per voice
    - home_assistant: Domotica - IMPORTANTE per HOME_CONTROL
    - openclaw: Servizio AI avanzato - IMPORTANTE
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.services: Dict[str, ServiceHealth] = {
            "openclaw": ServiceHealth(),
            # HA services aggiunti dinamicamente da _load_ha_services()
        }

        # Ollama e Whisper solo in modalita local (non in cloud/API)
        if config.AI_BACKEND != "api":
            self.services["ollama_router"] = ServiceHealth()
            self.services["whisper"] = ServiceHealth()

        self._lock = asyncio.Lock()
        self._check_interval = 30  # secondi
        self._initialized = True

        # Carica servizi HA dal database
        self._load_ha_services()

        logger.info(f"ServiceStatus initialized (backend={config.AI_BACKEND}, services={list(self.services.keys())})")

    def _load_ha_services(self):
        """Carica servizi HA dal database."""
        try:
            locations = get_all_locations(enabled_only=True)
            if not locations:
                logger.info("No Home Assistant locations configured - running in AI-only mode")
                return

            for loc in locations:
                # Skip locations without token
                if not loc.hass_token:
                    logger.info(f"Skipping HA service for '{loc.id}' - no token configured")
                    continue

                service_key = f"home_assistant_{loc.id}"
                if service_key not in self.services:
                    self.services[service_key] = ServiceHealth()
                    logger.info(f"Added HA service: {service_key}")
        except Exception as e:
            logger.warning(f"Could not load HA services from DB: {e}")

    def reload_ha_services(self):
        """Ricarica servizi HA (dopo modifiche location)."""
        # Rimuovi vecchi servizi HA
        self.services = {k: v for k, v in self.services.items()
                        if not k.startswith("home_assistant_")}
        self._load_ha_services()
        logger.info("HA services reloaded")

    async def check_all(self) -> Dict[str, ServiceState]:
        """Esegue health check su tutti i servizi."""
        async with self._lock:
            tasks = [self._check_openclaw()]

            # Ollama e Whisper solo in local mode
            if "ollama_router" in self.services:
                tasks.append(self._check_ollama_router())
            if "whisper" in self.services:
                tasks.append(self._check_whisper())

            # Aggiungi check per tutte le location HA
            try:
                locations = get_all_locations(enabled_only=True)
                for loc in locations:
                    tasks.append(self._check_home_assistant(loc.id, loc.hass_url, loc.hass_token))
            except Exception as e:
                logger.warning(f"Could not load locations for health check: {e}")

            await asyncio.gather(*tasks, return_exceptions=True)

        return self.get_summary()

    async def _check_ollama_router(self):
        """Check Ollama con modello router (Qwen)."""
        service = self.services["ollama_router"]
        start = time.time()

        try:
            async with aiohttp.ClientSession() as session:
                # Test con ping semplice al modello
                payload = {
                    "model": config.ROUTER_MODEL,
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": False,
                    "options": {"num_predict": 1}
                }
                async with session.post(
                    config.OLLAMA_CHAT_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=config.TIMEOUTS["health_check"])
                ) as resp:
                    elapsed = (time.time() - start) * 1000

                    if resp.status == 200:
                        service.state = ServiceState.ONLINE
                        service.last_success = time.time()
                        service.response_time_ms = elapsed
                        service.consecutive_failures = 0
                        service.last_error = None
                    else:
                        self._mark_failed(service, f"HTTP {resp.status}", elapsed)

        except asyncio.TimeoutError:
            self._mark_failed(service, "Timeout", (time.time() - start) * 1000)
        except Exception as e:
            self._mark_failed(service, str(e), (time.time() - start) * 1000)

        service.last_check = time.time()

    async def _check_whisper(self):
        """Check Whisper STT service."""
        service = self.services["whisper"]
        start = time.time()

        try:
            async with aiohttp.ClientSession() as session:
                # Solo check che il servizio risponda
                async with session.get(
                    config.WHISPER_URL,
                    timeout=aiohttp.ClientTimeout(total=config.TIMEOUTS["health_check"])
                ) as resp:
                    elapsed = (time.time() - start) * 1000

                    # Whisper potrebbe restituire 404 su / ma è OK se risponde
                    if resp.status in [200, 404, 405]:
                        service.state = ServiceState.ONLINE
                        service.last_success = time.time()
                        service.response_time_ms = elapsed
                        service.consecutive_failures = 0
                        service.last_error = None
                    else:
                        self._mark_failed(service, f"HTTP {resp.status}", elapsed)

        except asyncio.TimeoutError:
            self._mark_failed(service, "Timeout", (time.time() - start) * 1000)
        except Exception as e:
            self._mark_failed(service, str(e), (time.time() - start) * 1000)

        service.last_check = time.time()

    async def _check_home_assistant(self, location_id: str, hass_url: str, hass_token: str):
        """Check Home Assistant API per una specifica location."""
        service_key = f"home_assistant_{location_id}"

        # Assicura che il servizio esista
        if service_key not in self.services:
            self.services[service_key] = ServiceHealth()

        service = self.services[service_key]
        start = time.time()

        # Se non c'è token, marca come offline
        if not hass_token:
            self._mark_failed(service, "Token non configurato", 0)
            service.last_check = time.time()
            return

        # Timeout più alto per location remote (Tailscale)
        is_remote = any(p in hass_url for p in config.TAILSCALE_PATTERNS)
        timeout_seconds = config.TAILSCALE_TIMEOUT_REMOTE if is_remote else config.TAILSCALE_TIMEOUT_LOCAL

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {hass_token}",
                    "Content-Type": "application/json"
                }
                async with session.get(
                    f"{hass_url}/api/",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds)
                ) as resp:
                    elapsed = (time.time() - start) * 1000

                    if resp.status == 200:
                        service.state = ServiceState.ONLINE
                        service.last_success = time.time()
                        service.response_time_ms = elapsed
                        service.consecutive_failures = 0
                        service.last_error = None
                    elif resp.status == 401:
                        self._mark_failed(service, "Token non valido", elapsed)
                    else:
                        self._mark_failed(service, f"HTTP {resp.status}", elapsed)

        except asyncio.TimeoutError:
            self._mark_failed(service, "Timeout", (time.time() - start) * 1000)
        except Exception as e:
            self._mark_failed(service, str(e), (time.time() - start) * 1000)

        service.last_check = time.time()

    async def _check_openclaw(self):
        """Check Openclaw service."""
        service = self.services["openclaw"]
        start = time.time()

        try:
            async with aiohttp.ClientSession() as session:
                # OPENCLAW_URL include gia http:// (es: http://localhost:18789)
                openclaw_url = config.OPENCLAW_URL.rstrip("/")
                async with session.get(
                    f"{openclaw_url}/health",
                    timeout=aiohttp.ClientTimeout(total=config.TIMEOUTS.get("openclaw", 5))
                ) as resp:
                    elapsed = (time.time() - start) * 1000

                    if resp.status == 200:
                        service.state = ServiceState.ONLINE
                        service.last_success = time.time()
                        service.response_time_ms = elapsed
                        service.consecutive_failures = 0
                        service.last_error = None
                    else:
                        self._mark_failed(service, f"HTTP {resp.status}", elapsed)

        except asyncio.TimeoutError:
            self._mark_failed(service, "Timeout", (time.time() - start) * 1000)
        except Exception as e:
            self._mark_failed(service, str(e), (time.time() - start) * 1000)

        service.last_check = time.time()

    def _mark_failed(self, service: ServiceHealth, error: str, elapsed_ms: float):
        """Marca un servizio come fallito."""
        service.consecutive_failures += 1
        service.last_error = error
        service.response_time_ms = elapsed_ms

        # Dopo N fallimenti consecutivi: OFFLINE
        # Meno di N: DEGRADED
        if service.consecutive_failures >= config.HEALTH_FAILURE_THRESHOLD:
            service.state = ServiceState.OFFLINE
        else:
            service.state = ServiceState.DEGRADED

        logger.warning(f"Service check failed: {error} (failures: {service.consecutive_failures})")

    def get_summary(self) -> Dict[str, ServiceState]:
        """Restituisce un riepilogo degli stati."""
        return {name: s.state for name, s in self.services.items()}

    def get_status_for_prompt(self, location_id: str = None) -> str:
        """
        Genera una stringa per il router che descrive quali servizi sono offline.
        Usata per iniettare nel prompt del router.

        Args:
            location_id: Se specificato, indica solo lo stato di quella location HA
        """
        offline_services = []
        degraded_services = []
        ha_offline_locations = []
        ha_online_locations = []

        for name, service in self.services.items():
            # Gestisci servizi HA per location
            if name.startswith("home_assistant_"):
                loc_id = name.replace("home_assistant_", "")
                if service.state == ServiceState.OFFLINE:
                    ha_offline_locations.append(loc_id)
                elif service.state in [ServiceState.ONLINE, ServiceState.DEGRADED]:
                    ha_online_locations.append(loc_id)
                continue

            if service.state == ServiceState.OFFLINE:
                offline_services.append(name)
            elif service.state == ServiceState.DEGRADED:
                degraded_services.append(name)

        if not offline_services and not degraded_services and not ha_offline_locations:
            return ""  # Tutto OK, nessuna nota

        parts = []

        if offline_services:
            parts.append(f"SERVIZI OFFLINE: {', '.join(offline_services)}")
        if degraded_services:
            parts.append(f"SERVIZI DEGRADATI: {', '.join(degraded_services)}")

        # Status HA per location
        if ha_offline_locations:
            parts.append(f"HOME ASSISTANT OFFLINE: {', '.join(ha_offline_locations)}")
        if ha_online_locations:
            parts.append(f"HOME ASSISTANT ONLINE: {', '.join(ha_online_locations)}")

        # Istruzioni contestuali per il router
        hints = []

        # Check specifico per location se richiesto
        if location_id:
            if location_id in ha_offline_locations:
                hints.append(f"- HOME_CONTROL per {location_id} non disponibile")
            elif location_id not in ha_online_locations:
                hints.append(f"- Location {location_id} non configurata")
        elif ha_offline_locations and not ha_online_locations:
            hints.append("- HOME_CONTROL non disponibile, tutti gli Home Assistant sono offline")

        if "openclaw" in offline_services:
            hints.append("- Openclaw non disponibile, il servizio AI avanzato è offline")
        if "whisper" in offline_services:
            hints.append("- STT offline, input vocale non funzionante")

        if hints:
            parts.append("\nCOMPORTAMENTO:\n" + "\n".join(hints))

        return "\n".join(parts)

    def is_critical_online(self) -> bool:
        """Verifica se i servizi critici (almeno router) sono online."""
        router = self.services.get("ollama_router")
        if not router:
            # In modalità API, il router Ollama non è presente → servizio critico = OK
            return True
        return router.state in [ServiceState.ONLINE, ServiceState.DEGRADED]

    def can_do_home_control(self, location_id: str = None) -> bool:
        """
        Verifica se Home Assistant è disponibile.

        Args:
            location_id: Se specificato, verifica solo quella location.
                        Se None, verifica se almeno una location è disponibile.
        """
        if location_id:
            service_key = f"home_assistant_{location_id}"
            ha = self.services.get(service_key)
            if not ha:
                return False
            return ha.state in [ServiceState.ONLINE, ServiceState.DEGRADED]

        # Verifica se almeno una location HA è online
        for name, service in self.services.items():
            if name.startswith("home_assistant_"):
                if service.state in [ServiceState.ONLINE, ServiceState.DEGRADED]:
                    return True
        return False

    def get_available_locations(self) -> List[str]:
        """Ritorna lista di location HA online."""
        available = []
        for name, service in self.services.items():
            if name.startswith("home_assistant_"):
                if service.state in [ServiceState.ONLINE, ServiceState.DEGRADED]:
                    loc_id = name.replace("home_assistant_", "")
                    available.append(loc_id)
        return available

    def can_do_openclaw(self) -> bool:
        """Verifica se Openclaw è disponibile."""
        openclaw = self.services["openclaw"]
        return openclaw.state in [ServiceState.ONLINE, ServiceState.DEGRADED]

    def get_offline_message(self, intent: str, location_id: str = None) -> Optional[str]:
        """
        Restituisce un messaggio user-friendly per intent non disponibili.

        Args:
            intent: L'intent richiesto
            location_id: Per HOME_CONTROL, la location specifica

        Returns:
            Messaggio di errore o None se l'intent è disponibile
        """
        if intent == "HOME_CONTROL":
            if location_id:
                if not self.can_do_home_control(location_id):
                    # Recupera nome location dal DB per messaggio user-friendly
                    try:
                        from database import get_location
                        loc = get_location(location_id)
                        loc_name = loc.name if loc else location_id
                    except Exception:
                        loc_name = location_id
                    return f"Mi dispiace, Home Assistant per {loc_name} non è raggiungibile in questo momento."
            elif not self.can_do_home_control():
                return "Mi dispiace, Home Assistant non è raggiungibile in questo momento. Riprova tra poco."

        if intent in ("DEEP_REASONING", "RESEARCH") and not self.can_do_openclaw():
            return "Il servizio Openclaw non è disponibile al momento. Riprova tra poco."

        return None

    def to_dict(self) -> dict:
        """Serializza lo stato per API/dashboard."""
        result = {}
        for name, service in self.services.items():
            result[name] = {
                "state": service.state.value,
                "last_check": service.last_check,
                "last_success": service.last_success,
                "last_error": service.last_error,
                "response_time_ms": service.response_time_ms,
                "consecutive_failures": service.consecutive_failures
            }
        return result


# Singleton instance
service_status = ServiceStatus()
