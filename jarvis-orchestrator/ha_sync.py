"""
JARVIS Home Assistant Entity Sync
- Sincronizza entity da Home Assistant al database JARVIS
- Fetch via HA REST API
- Parse friendly_name, entity_id, area, device_class
- Import nel database con mapping automatico
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger("JARVIS_HA_SYNC")


@dataclass
class HAEntity:
    """Rappresenta un'entity Home Assistant."""
    entity_id: str
    friendly_name: str
    domain: str  # light, switch, media_player, etc.
    area_id: Optional[str] = None
    area_name: Optional[str] = None
    device_class: Optional[str] = None
    state: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "friendly_name": self.friendly_name,
            "domain": self.domain,
            "area_id": self.area_id,
            "area_name": self.area_name,
            "device_class": self.device_class,
            "state": self.state
        }


# Entity types che ci interessano per JARVIS
SUPPORTED_DOMAINS = [
    "light",
    "switch",
    "cover",
    "climate",
    "media_player",
    "camera",
    "lock",
    "fan",
    "vacuum",
    "sensor",
    "binary_sensor",
    "alarm_control_panel",
    "scene",
    "script",
    "automation",
    "input_boolean",
    "input_number",
    "input_select",
]


async def fetch_ha_states(hass_url: str, hass_token: str) -> List[dict]:
    """
    Fetch tutti gli states da Home Assistant.

    Returns:
        Lista di state objects da HA API
    """
    url = f"{hass_url.rstrip('/')}/api/states"
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    error = await resp.text()
                    logger.error(f"HA API error: {resp.status} - {error}")
                    return []
    except Exception as e:
        logger.error(f"Failed to fetch HA states: {e}")
        return []


async def fetch_ha_areas(hass_url: str, hass_token: str) -> Dict[str, str]:
    """
    Fetch le aree da Home Assistant.

    Returns:
        Dict area_id → area_name
    """
    url = f"{hass_url.rstrip('/')}/api/config/area_registry/list"
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json={}, timeout=10) as resp:
                if resp.status == 200:
                    areas = await resp.json()
                    return {area["area_id"]: area["name"] for area in areas}
                else:
                    # Prova con GET per versioni HA più vecchie
                    pass
    except Exception as e:
        logger.warning(f"Could not fetch areas via registry: {e}")

    # Fallback: usa template API
    try:
        url = f"{hass_url.rstrip('/')}/api/template"
        template = '{{ areas() | map("area_name") | list | to_json }}'
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers,
                json={"template": template},
                timeout=10
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    # Questo ritorna solo i nomi, non gli ID
                    # Per ora ritorniamo dict vuoto e usiamo solo gli attributi delle entity
                    return {}
    except Exception as e:
        logger.warning(f"Could not fetch areas via template: {e}")

    return {}


async def fetch_ha_entity_registry(hass_url: str, hass_token: str) -> Dict[str, dict]:
    """
    Fetch l'entity registry per ottenere area_id per ogni entity.

    Returns:
        Dict entity_id → {area_id, device_id, ...}
    """
    url = f"{hass_url.rstrip('/')}/api/config/entity_registry/list"
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Entity registry usa POST con body vuoto
            async with session.post(url, headers=headers, json={}, timeout=30) as resp:
                if resp.status == 200:
                    entities = await resp.json()
                    return {e["entity_id"]: e for e in entities}
    except Exception as e:
        logger.warning(f"Could not fetch entity registry: {e}")

    return {}


async def fetch_ha_device_registry(hass_url: str, hass_token: str) -> Dict[str, dict]:
    """
    Fetch il device registry per ottenere area_id dai devices.

    Returns:
        Dict device_id → {area_id, name, ...}
    """
    url = f"{hass_url.rstrip('/')}/api/config/device_registry/list"
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json={}, timeout=30) as resp:
                if resp.status == 200:
                    devices = await resp.json()
                    return {d["id"]: d for d in devices}
    except Exception as e:
        logger.warning(f"Could not fetch device registry: {e}")

    return {}


async def sync_entities_from_ha(
    location_id: str,
    hass_url: str,
    hass_token: str,
    entity_types: List[str] = None,
    overwrite_existing: bool = False
) -> Tuple[int, int, List[str]]:
    """
    Sincronizza le entity da Home Assistant al database JARVIS.

    Args:
        location_id: ID della location JARVIS
        hass_url: URL Home Assistant
        hass_token: Token HA
        entity_types: Lista domini da sincronizzare (default: tutti supportati)
        overwrite_existing: Se True, sovrascrive entity_id esistenti

    Returns:
        Tuple[added, updated, errors]
    """
    from database import (
        get_db_connection, resolve_entity_id, update_entity_id
    )

    domains = entity_types or SUPPORTED_DOMAINS
    added = 0
    updated = 0
    errors = []

    # Fetch dati da HA
    logger.info(f"Syncing entities from HA for location {location_id}")

    states = await fetch_ha_states(hass_url, hass_token)
    if not states:
        return 0, 0, ["Failed to fetch states from Home Assistant"]

    areas = await fetch_ha_areas(hass_url, hass_token)
    entity_registry = await fetch_ha_entity_registry(hass_url, hass_token)
    device_registry = await fetch_ha_device_registry(hass_url, hass_token)

    logger.info(f"Fetched {len(states)} states, {len(areas)} areas, "
                f"{len(entity_registry)} entity registry entries")

    # Parse entities
    ha_entities = []
    for state in states:
        entity_id = state.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        if domain not in domains:
            continue

        attributes = state.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)

        # Trova area_id
        area_id = None
        area_name = None

        # Prima prova dall'entity registry
        if entity_id in entity_registry:
            reg_entry = entity_registry[entity_id]
            area_id = reg_entry.get("area_id")

            # Se non ha area_id, prova dal device
            if not area_id and reg_entry.get("device_id"):
                device_id = reg_entry["device_id"]
                if device_id in device_registry:
                    area_id = device_registry[device_id].get("area_id")

        # Converti area_id in area_name
        if area_id and area_id in areas:
            area_name = areas[area_id]

        ha_entities.append(HAEntity(
            entity_id=entity_id,
            friendly_name=friendly_name,
            domain=domain,
            area_id=area_id,
            area_name=area_name,
            device_class=attributes.get("device_class"),
            state=state.get("state")
        ))

    logger.info(f"Parsed {len(ha_entities)} relevant entities")

    # Import nel database
    conn = get_db_connection()
    c = conn.cursor()

    for entity in ha_entities:
        try:
            # Determina room (usa area_name o "Sconosciuto")
            room = entity.area_name or "Sconosciuto"

            # Determina zone (basato su device_class o domain)
            zone = _infer_zone(entity)

            # Controlla se esiste già
            c.execute("""
                SELECT id, entity_id FROM entity_maps
                WHERE location_id = ? AND LOWER(entity_name) = LOWER(?)
                AND entity_type = ?
            """, (location_id, entity.friendly_name, entity.domain))

            existing = c.fetchone()

            if existing:
                # Entity esiste già
                if overwrite_existing or not existing['entity_id']:
                    # Aggiorna entity_id
                    c.execute("""
                        UPDATE entity_maps SET entity_id = ?, room = ?
                        WHERE id = ?
                    """, (entity.entity_id, room, existing['id']))
                    updated += 1
            else:
                # Nuova entity
                c.execute("""
                    INSERT INTO entity_maps
                    (location_id, zone, area, room, entity_type, entity_name, entity_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    location_id,
                    zone,
                    room,  # area = room per semplicità
                    room,
                    entity.domain,
                    entity.friendly_name,
                    entity.entity_id
                ))
                added += 1

        except Exception as e:
            errors.append(f"Error importing {entity.entity_id}: {e}")
            logger.warning(f"Error importing {entity.entity_id}: {e}")

    conn.commit()
    conn.close()

    logger.info(f"Sync complete: {added} added, {updated} updated, {len(errors)} errors")
    return added, updated, errors


def _infer_zone(entity: HAEntity) -> str:
    """
    Inferisce la zona basandosi su device_class e domain.
    """
    # Camere di sicurezza
    if entity.domain == "camera":
        if entity.device_class in ["doorbell"]:
            return "Perimetrale"
        return "Interno"

    # Sensori
    if entity.domain == "binary_sensor":
        if entity.device_class in ["door", "window", "motion", "occupancy"]:
            return "Sicurezza"
        return "Sensori"

    if entity.domain == "sensor":
        if entity.device_class in ["temperature", "humidity"]:
            return "Clima"
        return "Sensori"

    # Illuminazione
    if entity.domain == "light":
        return "Illuminazione"

    # Automazione casa
    if entity.domain in ["cover", "lock", "alarm_control_panel"]:
        return "Sicurezza"

    if entity.domain in ["climate", "fan"]:
        return "Clima"

    if entity.domain == "media_player":
        return "Media"

    if entity.domain in ["switch", "scene", "script", "automation"]:
        return "Automazione"

    return "Altro"


async def preview_ha_sync(
    hass_url: str,
    hass_token: str,
    entity_types: List[str] = None
) -> Dict[str, any]:
    """
    Preview di cosa verrebbe sincronizzato (dry-run).

    Returns:
        Dict con statistiche e lista entity
    """
    domains = entity_types or SUPPORTED_DOMAINS

    states = await fetch_ha_states(hass_url, hass_token)
    if not states:
        return {"error": "Failed to fetch states from Home Assistant", "entities": []}

    areas = await fetch_ha_areas(hass_url, hass_token)
    entity_registry = await fetch_ha_entity_registry(hass_url, hass_token)
    device_registry = await fetch_ha_device_registry(hass_url, hass_token)

    entities_by_domain = {}
    entities_by_area = {}
    entities_list = []

    for state in states:
        entity_id = state.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        if domain not in domains:
            continue

        attributes = state.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)

        # Trova area
        area_id = None
        area_name = "Sconosciuto"

        if entity_id in entity_registry:
            reg_entry = entity_registry[entity_id]
            area_id = reg_entry.get("area_id")
            if not area_id and reg_entry.get("device_id"):
                device_id = reg_entry["device_id"]
                if device_id in device_registry:
                    area_id = device_registry[device_id].get("area_id")

        if area_id and area_id in areas:
            area_name = areas[area_id]

        # Conteggi
        entities_by_domain[domain] = entities_by_domain.get(domain, 0) + 1
        entities_by_area[area_name] = entities_by_area.get(area_name, 0) + 1

        entities_list.append({
            "entity_id": entity_id,
            "friendly_name": friendly_name,
            "domain": domain,
            "area": area_name,
            "state": state.get("state")
        })

    return {
        "total": len(entities_list),
        "by_domain": entities_by_domain,
        "by_area": entities_by_area,
        "areas_found": list(set(a for a in entities_by_area.keys() if a != "Sconosciuto")),
        "entities": entities_list
    }


async def get_ha_media_players(hass_url: str, hass_token: str) -> List[dict]:
    """
    Ritorna lista media_player per selezione speaker output.
    Utile per configurazione voice devices.
    """
    states = await fetch_ha_states(hass_url, hass_token)

    media_players = []
    for state in states:
        entity_id = state.get("entity_id", "")
        if not entity_id.startswith("media_player."):
            continue

        attributes = state.get("attributes", {})
        media_players.append({
            "entity_id": entity_id,
            "friendly_name": attributes.get("friendly_name", entity_id),
            "device_class": attributes.get("device_class"),
            "supported_features": attributes.get("supported_features", 0),
            "state": state.get("state")
        })

    return media_players
