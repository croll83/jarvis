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


# ===========================================================================
# VECTOR SEARCH (semantic search via HA Memory Services)
# ===========================================================================

async def search_location_events(
    location_id: str,
    query: str,
    n_results: int = None,
    min_timestamp: float = None
) -> List[Dict[str, Any]]:
    """
    Ricerca semantica eventi in una location specifica.
    """
    if n_results is None:
        n_results = config.LOCATION_MEMORY_DEFAULTS["n_results"]

    url = MEMORY_SERVICE_URLS.get(location_id)
    if not url:
        logger.warning(f"No memory service for {location_id}")
        return []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/memory/search",
                json={
                    "query": query,
                    "n_results": n_results,
                    "min_timestamp": min_timestamp
                },
                timeout=aiohttp.ClientTimeout(total=config.TIMEOUTS["location_memory"])
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("results", [])
                else:
                    logger.error(f"Location search error {location_id}: {resp.status}")
                    return []
    except Exception as e:
        logger.error(f"Location search failed {location_id}: {e}")
        return []


async def search_all_locations_events(
    query: str,
    location_ids: List[str] = None,
    n_results_per_location: int = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Ricerca semantica su tutte le location.
    """
    if n_results_per_location is None:
        n_results_per_location = config.LOCATION_MEMORY_DEFAULTS["n_results_per_location"]

    if location_ids is None:
        location_ids = list(MEMORY_SERVICE_URLS.keys())

    results = {}
    for loc_id in location_ids:
        loc_results = await search_location_events(
            loc_id, query, n_results_per_location
        )
        if loc_results:
            results[loc_id] = loc_results

    return results


def format_location_vector_results_for_llm(
    results: Dict[str, List[Dict[str, Any]]],
    max_tokens: int = 500
) -> str:
    """
    Formatta risultati vector search location per il prompt.
    """
    if not results:
        return ""

    lines = ["\n[Eventi rilevanti]"]
    tokens_used = 0

    for loc_id, events in results.items():
        loc = get_location(loc_id)
        loc_name = loc.name if loc else loc_id

        lines.append(f"\n{loc_name}:")

        for event in events:
            if event.get('final_score', 0) < config.LOCATION_MEMORY_DEFAULTS["min_final_score"]:
                continue

            meta = event.get('metadata', {})
            entity = meta.get('entity_id', 'unknown')
            new_state = meta.get('new_state', '?')

            # Timestamp human readable
            ts = meta.get('timestamp', time.time())
            time_str = datetime.fromtimestamp(ts).strftime('%d/%m %H:%M')

            line = f"  - {time_str}: {entity} -> {new_state}"
            est_tokens = len(line) // 4

            if tokens_used + est_tokens > max_tokens:
                break

            lines.append(line)
            tokens_used += est_tokens

    if len(lines) == 1:
        return ""

    return "\n".join(lines)
