"""
JARVIS Home Assistant Entity Sync
- Sincronizza entity da Home Assistant al database JARVIS
- Fetch via HA REST API (states) + WebSocket API (registries)
- Parse friendly_name, entity_id, area, device_class
- Import nel database con mapping automatico
"""

import logging
import json
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
    device_name: Optional[str] = None
    state: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "friendly_name": self.friendly_name,
            "domain": self.domain,
            "area_id": self.area_id,
            "area_name": self.area_name,
            "device_class": self.device_class,
            "device_name": self.device_name,
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


async def _ha_ws_command(ws, msg_id: int, msg_type: str) -> Optional[list]:
    """
    Invia un comando WebSocket a HA e ritorna il risultato.

    Args:
        ws: WebSocket connection aiohttp
        msg_id: ID messaggio incrementale
        msg_type: Tipo comando (es. "config/area_registry/list")

    Returns:
        Lista risultati o None in caso di errore
    """
    await ws.send_json({"id": msg_id, "type": msg_type})
    resp = await ws.receive_json()
    if resp.get("success"):
        return resp.get("result", [])
    else:
        logger.warning(f"WS command '{msg_type}' failed: {resp.get('error', {}).get('message', 'unknown')}")
        return None


async def fetch_ha_registries(
    hass_url: str, hass_token: str
) -> Tuple[Dict[str, str], Dict[str, dict], Dict[str, dict]]:
    """
    Fetch area, entity e device registry da HA via WebSocket API.

    Le registry REST API (/api/config/*/list) non esistono nella maggior
    parte delle versioni HA. L'unico modo per accedere a queste info è
    il WebSocket API.

    Returns:
        Tuple di (areas, entity_registry, device_registry):
        - areas: Dict area_id → area_name
        - entity_registry: Dict entity_id → {area_id, device_id, ...}
        - device_registry: Dict device_id → {area_id, name, ...}
    """
    areas: Dict[str, str] = {}
    entity_registry: Dict[str, dict] = {}
    device_registry: Dict[str, dict] = {}

    # Costruisci URL WebSocket da URL HTTP
    ws_url = hass_url.rstrip("/")
    ws_url = ws_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url += "/api/websocket"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, timeout=30) as ws:
                # Step 1: Ricevi auth_required
                auth_req = await ws.receive_json()
                if auth_req.get("type") != "auth_required":
                    logger.error(f"Unexpected WS message: {auth_req}")
                    return areas, entity_registry, device_registry

                # Step 2: Autenticazione
                await ws.send_json({
                    "type": "auth",
                    "access_token": hass_token
                })

                auth_resp = await ws.receive_json()
                if auth_resp.get("type") != "auth_ok":
                    logger.error(f"WS auth failed: {auth_resp}")
                    return areas, entity_registry, device_registry

                logger.info(f"WS connected to HA v{auth_resp.get('ha_version', '?')}")

                # Step 3: Fetch area registry
                area_list = await _ha_ws_command(ws, 1, "config/area_registry/list")
                if area_list:
                    areas = {a["area_id"]: a["name"] for a in area_list}
                    logger.info(f"WS: fetched {len(areas)} areas")

                # Step 4: Fetch entity registry
                entity_list = await _ha_ws_command(ws, 2, "config/entity_registry/list")
                if entity_list:
                    entity_registry = {e["entity_id"]: e for e in entity_list}
                    logger.info(f"WS: fetched {len(entity_registry)} entity registry entries")

                # Step 5: Fetch device registry
                device_list = await _ha_ws_command(ws, 3, "config/device_registry/list")
                if device_list:
                    device_registry = {d["id"]: d for d in device_list}
                    logger.info(f"WS: fetched {len(device_registry)} device registry entries")

    except aiohttp.WSServerHandshakeError as e:
        logger.error(f"WS handshake failed (check SSL/URL): {e}")
    except Exception as e:
        logger.error(f"Failed to fetch HA registries via WebSocket: {e}")

    return areas, entity_registry, device_registry


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
        _get_conn, resolve_entity_id, update_entity_id,
        get_hierarchy_mapping
    )

    domains = entity_types or SUPPORTED_DOMAINS
    added = 0
    updated = 0
    skipped = 0
    errors = []

    # Carica gerarchia personalizzata (se importata)
    hierarchy = get_hierarchy_mapping(location_id)
    has_hierarchy = bool(hierarchy)
    if has_hierarchy:
        logger.info(f"Using custom hierarchy with {len(hierarchy)} area mappings")

    # Fetch dati da HA
    logger.info(f"Syncing entities from HA for location {location_id}")

    states = await fetch_ha_states(hass_url, hass_token)
    if not states:
        return 0, 0, ["Failed to fetch states from Home Assistant"]

    # Fetch registries via WebSocket (REST API non disponibile)
    areas, entity_registry, device_registry = await fetch_ha_registries(hass_url, hass_token)

    logger.info(f"Fetched {len(states)} states, {len(areas)} areas, "
                f"{len(entity_registry)} entity registry entries, "
                f"{len(device_registry)} device registry entries")

    # Parse entities
    ha_entities = []
    disabled_count = 0
    for state in states:
        entity_id = state.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        if domain not in domains:
            continue

        attributes = state.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)

        # Trova area_id e device_name
        area_id = None
        area_name = None
        device_name = None

        # Prima prova dall'entity registry
        if entity_id in entity_registry:
            reg_entry = entity_registry[entity_id]

            # Salta entity disabilitate o nascoste (es. switch rimpiazzati da switch_as_x)
            if reg_entry.get("disabled_by") or reg_entry.get("hidden_by"):
                disabled_count += 1
                continue

            area_id = reg_entry.get("area_id")
            device_id_ref = reg_entry.get("device_id")

            # Se ha un device, recupera device_name e area_id (se mancante)
            if device_id_ref and device_id_ref in device_registry:
                device = device_registry[device_id_ref]
                device_name = device.get("name_by_user") or device.get("name")
                if not area_id:
                    area_id = device.get("area_id")

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
            device_name=device_name,
            state=state.get("state")
        ))

    logger.info(f"Parsed {len(ha_entities)} relevant entities ({disabled_count} disabled skipped)")

    # Import nel database
    conn = _get_conn()
    c = conn.cursor()

    for entity in ha_entities:
        try:
            # Determina room = area HA (area_name)
            ha_area_key = entity.area_id or ""
            room = entity.area_name or "Sconosciuto"

            if has_hierarchy:
                # Usa la gerarchia personalizzata per zone/area
                hier = hierarchy.get(ha_area_key)
                if not hier:
                    # Prova match case-insensitive sulle chiavi
                    for k, v in hierarchy.items():
                        if k.lower() == ha_area_key.lower():
                            hier = v
                            break

                if hier:
                    zone = hier["zone"]    # floor_name (Piano 1, etc.)
                    area = hier["area"]    # zone_name (Zona Giorno, etc.)
                else:
                    # Entity in un'area HA non presente nella gerarchia → skip o "Altri"
                    zone = "Non classificato"
                    area = "Non classificato"
            else:
                # Nessuna gerarchia → usa inferenza automatica
                zone = _infer_zone(entity)
                area = room

            # Controlla se esiste già — usa entity_id come chiave primaria
            existing = None
            if entity.entity_id:
                c.execute("""
                    SELECT id, entity_id FROM entity_maps
                    WHERE location_id = ? AND entity_id = ?
                """, (location_id, entity.entity_id))
                existing = c.fetchone()

            # Fallback: cerca per friendly_name (per entity senza entity_id)
            if not existing:
                c.execute("""
                    SELECT id, entity_id FROM entity_maps
                    WHERE location_id = ? AND LOWER(entity_name) = LOWER(?)
                    AND entity_type = ?
                """, (location_id, entity.friendly_name, entity.domain))
                existing = c.fetchone()

            if existing:
                # Entity esiste già — aggiorna sempre (room, zone, area, device_name, friendly_name)
                c.execute("""
                    UPDATE entity_maps
                    SET entity_id = ?, entity_name = ?, room = ?, zone = ?, area = ?, device_name = ?
                    WHERE id = ?
                """, (entity.entity_id, entity.friendly_name, room, zone, area,
                      entity.device_name, existing['id']))
                updated += 1
            else:
                # Nuova entity
                c.execute("""
                    INSERT INTO entity_maps
                    (location_id, zone, area, room, device_name,
                     entity_type, entity_name, entity_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    location_id,
                    zone,
                    area,
                    room,
                    entity.device_name,
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

    # Fetch registries via WebSocket (REST API non disponibile)
    areas, entity_registry, device_registry = await fetch_ha_registries(hass_url, hass_token)

    entities_by_domain = {}
    entities_by_area = {}
    entities_list = []
    disabled_count = 0

    for state in states:
        entity_id = state.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        if domain not in domains:
            continue

        attributes = state.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)

        # Trova area e device
        area_id = None
        area_name = "Sconosciuto"
        device_name = None

        if entity_id in entity_registry:
            reg_entry = entity_registry[entity_id]

            # Salta entity disabilitate o nascoste (es. switch rimpiazzati da switch_as_x)
            if reg_entry.get("disabled_by") or reg_entry.get("hidden_by"):
                disabled_count += 1
                continue

            area_id = reg_entry.get("area_id")
            device_id_ref = reg_entry.get("device_id")

            if device_id_ref and device_id_ref in device_registry:
                device = device_registry[device_id_ref]
                device_name = device.get("name_by_user") or device.get("name")
                if not area_id:
                    area_id = device.get("area_id")

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
            "device": device_name,
            "state": state.get("state")
        })

    return {
        "total": len(entities_list),
        "disabled_skipped": disabled_count,
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
