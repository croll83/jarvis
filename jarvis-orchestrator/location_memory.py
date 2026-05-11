"""
JARVIS Location Memory Client
- Comunica con HA Memory Services
- Aggrega memoria da multiple location
- Vector search per eventi location
"""

import time
import aiohttp
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List

from database import get_all_locations, get_location
import config

logger = logging.getLogger("JARVIS_LOCATION_MEMORY")

# Registry dei memory service URL (popolato da config o DB)
MEMORY_SERVICE_URLS: Dict[str, str] = {}


def register_location_memory_service(location_id: str, url: str):
    """Registra URL del memory service per una location."""
    MEMORY_SERVICE_URLS[location_id] = url
    logger.info(f"Registered memory service for {location_id}: {url}")


def load_memory_services_from_db():
    """Carica URL memory service dal DB locations."""
    locations = get_all_locations(enabled_only=True)
    for loc in locations:
        # Il memory service gira sulla stessa rete di HA, porta 8100
        if loc.hass_url:
            # Rimuovi porta HA e aggiungi porta memory service
            base = loc.hass_url.rsplit(':', 1)[0]
            memory_url = f"{base}:8100"
            MEMORY_SERVICE_URLS[loc.id] = memory_url

    if MEMORY_SERVICE_URLS:
        logger.info(f"Loaded {len(MEMORY_SERVICE_URLS)} memory service URLs: {list(MEMORY_SERVICE_URLS.keys())}")
    else:
        logger.info("No location memory services configured")


async def get_location_memory(
    location_id: str,
    hot_minutes: int = None,
    warm_hours: int = None,
    cold_days: int = None
) -> Optional[Dict[str, Any]]:
    """Recupera memoria da una location specifica."""
    if hot_minutes is None:
        hot_minutes = config.LOCATION_MEMORY_DEFAULTS["hot_minutes"]
    if warm_hours is None:
        warm_hours = config.LOCATION_MEMORY_DEFAULTS["warm_hours"]
    if cold_days is None:
        cold_days = config.LOCATION_MEMORY_DEFAULTS["cold_days"]

    url = MEMORY_SERVICE_URLS.get(location_id)
    if not url:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{url}/memory",
                params={
                    "hot_minutes": hot_minutes,
                    "warm_hours": warm_hours,
                    "cold_days": cold_days
                },
                timeout=aiohttp.ClientTimeout(total=config.TIMEOUTS["location_memory"])
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.error(f"Memory service {location_id} error: {resp.status}")
                    return None
    except Exception as e:
        logger.debug(f"Memory service {location_id} unreachable: {e}")
        return None


async def get_all_locations_memory(
    location_ids: Optional[List[str]] = None
) -> Dict[str, Dict[str, Any]]:
    """Recupera memoria da tutte le location (o quelle specificate)."""
    if location_ids is None:
        location_ids = list(MEMORY_SERVICE_URLS.keys())

    results = {}
    for loc_id in location_ids:
        memory = await get_location_memory(loc_id)
        if memory:
            results[loc_id] = memory

    return results


def format_locations_memory_for_llm(
    memories: Dict[str, Dict[str, Any]],
    max_tokens_per_location: int = None
) -> str:
    """Formatta memoria delle location per il prompt."""
    if max_tokens_per_location is None:
        max_tokens_per_location = config.LOCATION_MEMORY_DEFAULTS["max_tokens_per_location"]

    if not memories:
        return ""

    lines = ["\n[STATO DOMOTICO]"]
    tokens_used = 0

    for loc_id, memory in memories.items():
        loc = get_location(loc_id)
        loc_name = loc.name if loc else loc_id

        lines.append(f"\n## {loc_name}")
        loc_tokens = 0

        # Longterm facts
        if memory.get("longterm"):
            lines.append("Pattern noti:")
            for fact in memory["longterm"][:config.LOCATION_MEMORY_DEFAULTS["max_longterm_facts"]]:
                text = f"  - {fact['fact']}"
                est = len(text) // 4
                if loc_tokens + est > max_tokens_per_location:
                    break
                lines.append(text)
                loc_tokens += est

        # Stato attuale (hot)
        if memory.get("hot") and loc_tokens < max_tokens_per_location * 0.7:
            lines.append("Attivita' recente:")
            for event in memory["hot"][:config.LOCATION_MEMORY_DEFAULTS["max_hot_events"]]:
                text = f"  - {event['entity_id']}: {event['new_state']}"
                est = len(text) // 4
                if loc_tokens + est > max_tokens_per_location:
                    break
                lines.append(text)
                loc_tokens += est

        # Summaries (warm)
        if memory.get("warm") and loc_tokens < max_tokens_per_location * 0.9:
            latest = memory["warm"][0]
            text = f"Ultima ora: {latest['summary'][:100]}"
            lines.append(text)

        tokens_used += loc_tokens

    return "\n".join(lines)


# NOTE: vector search per eventi-location era servita dal vector store di
# ha_memory_service (deprecato). Memoria semantica cross-location e' ora
# delegata a mem0-stack (MEM0_BASE_URL). Nessuna API qui sopra.
