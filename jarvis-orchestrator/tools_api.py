"""
JARVIS Tools API — OpenClaw Skill Endpoints

REST API endpoints that OpenClaw calls as a skill.
All endpoints are behind bearer token authentication (OPENCLAW_TOKEN).

9 tool endpoints:
- jarvis_home_control:   Control HA entities with L1-L4 security
- jarvis_speaker_id:     Identify speaker from audio
- jarvis_get_user_context: Get user info + location + preferences
- jarvis_security:       Set privacy mode, security actions
- jarvis_memory_query:   Query user/location memory
- jarvis_entity_resolve: Resolve friendly name → entity_id
- jarvis_tts:            Text-to-speech passthrough
- jarvis_get_locations:  List all locations with health status
- jarvis_audit_log:      Log an event to the audit trail
"""

import logging
import time
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel, Field

import config
from security_levels import check_security, needs_approval, SecurityLevel

logger = logging.getLogger("JARVIS_TOOLS_API")

router = APIRouter(prefix="/api/tools", tags=["OpenClaw Tools"])


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════

async def verify_openclaw_token(authorization: Optional[str] = Header(None)):
    """Verify the OpenClaw bearer token."""
    if not config.OPENCLAW_TOKEN:
        raise HTTPException(status_code=503, detail="OpenClaw token not configured")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization[7:]
    if token != config.OPENCLAW_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid OpenClaw token")


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class HomeControlRequest(BaseModel):
    """Request to control a Home Assistant entity."""
    entity_name: str = Field(..., description="Friendly name or entity_id (e.g., 'luce soggiorno' or 'light.soggiorno')")
    action: str = Field(..., description="Action to perform (e.g., 'turn_on', 'turn_off', 'toggle', 'set_temperature')")
    parameters: Optional[Dict[str, Any]] = Field(default=None, description="Additional parameters (e.g., {'brightness': 128, 'temperature': 22})")
    location_id: Optional[str] = Field(default=None, description="Target location (e.g., 'wagmi', 'napoli'). Auto-resolved if not provided.")
    source_channel: str = Field(default="api_tools", description="Source channel for security check")


class HomeControlResponse(BaseModel):
    success: bool
    message: str
    entity_id: Optional[str] = None
    security_level: Optional[str] = None
    approval_required: bool = False


class SpeakerIdRequest(BaseModel):
    """Request to identify a speaker from audio data."""
    audio_base64: str = Field(..., description="Base64-encoded audio data")
    sample_rate: int = Field(default=16000)


class SpeakerIdResponse(BaseModel):
    identified: bool
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    confidence: float = 0.0


class UserContextRequest(BaseModel):
    user_id: str


class UserContextResponse(BaseModel):
    user_id: str
    user_name: Optional[str] = None
    location_id: Optional[str] = None
    location_name: Optional[str] = None
    preferences: Dict[str, Any] = {}
    role: Optional[str] = None


class SecurityActionRequest(BaseModel):
    action: str = Field(..., description="Security action (e.g., 'set_privacy_mode', 'arm_alarm', 'disarm_alarm')")
    parameters: Optional[Dict[str, Any]] = None
    source_channel: str = Field(default="api_tools")


class SecurityActionResponse(BaseModel):
    success: bool
    message: str


class MemoryQueryRequest(BaseModel):
    user_id: Optional[str] = None
    location_id: Optional[str] = None
    query: Optional[str] = Field(default=None, description="Natural language query for vector search")
    context_type: str = Field(default="routing", description="'routing' (compact) or 'reasoning' (full)")


class MemoryQueryResponse(BaseModel):
    context: str
    sources: Dict[str, Any] = {}


class EntityResolveRequest(BaseModel):
    friendly_name: str = Field(..., description="Friendly name to resolve (e.g., 'luce soggiorno')")
    location_id: Optional[str] = None
    domain_filter: Optional[str] = Field(default=None, description="Filter by domain (e.g., 'light', 'switch')")


class EntityResolveResponse(BaseModel):
    found: bool
    entity_id: Optional[str] = None
    domain: Optional[str] = None
    friendly_name: Optional[str] = None
    area: Optional[str] = None
    location_id: Optional[str] = None
    state: Optional[str] = None
    available_services: Optional[List[str]] = None
    service_params: Optional[Dict[str, Any]] = None
    device_class: Optional[str] = None
    alternatives: List[Dict[str, str]] = []


# ═══════════════════════════════════════════════════════════════════════════════
# DOMAIN CAPABILITIES — Servizi disponibili per ogni tipo di dispositivo
# ═══════════════════════════════════════════════════════════════════════════════

DOMAIN_SERVICES: Dict[str, Dict[str, Any]] = {
    "light": {
        "services": ["turn_on", "turn_off", "toggle"],
        "params": {
            "turn_on": {
                "brightness": "0-255 (luminosità)",
                "color_temp_kelvin": "2000-6500 (temperatura colore)",
                "rgb_color": "[R, G, B] (colore RGB)",
                "transition": "secondi di transizione",
            }
        }
    },
    "switch": {
        "services": ["turn_on", "turn_off", "toggle"],
        "params": {}
    },
    "cover": {
        "services": ["open_cover", "close_cover", "stop_cover", "set_cover_position", "toggle"],
        "params": {
            "set_cover_position": {"position": "0-100 (0=chiuso, 100=aperto)"}
        }
    },
    "climate": {
        "services": ["set_temperature", "set_hvac_mode", "turn_on", "turn_off"],
        "params": {
            "set_temperature": {"temperature": "gradi Celsius"},
            "set_hvac_mode": {"hvac_mode": "heat | cool | auto | off | fan_only | dry"}
        }
    },
    "media_player": {
        "services": ["turn_on", "turn_off", "toggle", "volume_up", "volume_down",
                      "volume_set", "volume_mute", "media_play", "media_pause",
                      "media_stop", "media_next_track", "media_previous_track",
                      "play_media", "select_source"],
        "params": {
            "volume_set": {"volume_level": "0.0-1.0"},
            "volume_mute": {"is_volume_muted": "true/false"},
            "play_media": {"media_content_id": "URL o ID media", "media_content_type": "music | video | playlist"},
            "select_source": {"source": "nome input/sorgente"}
        }
    },
    "fan": {
        "services": ["turn_on", "turn_off", "toggle", "set_percentage", "set_preset_mode"],
        "params": {
            "set_percentage": {"percentage": "0-100"},
            "set_preset_mode": {"preset_mode": "dipende dal dispositivo"}
        }
    },
    "lock": {
        "services": ["lock", "unlock", "open"],
        "params": {}
    },
    "vacuum": {
        "services": ["start", "stop", "pause", "return_to_base", "locate",
                      "set_fan_speed", "send_command"],
        "params": {
            "set_fan_speed": {"fan_speed": "silent | standard | medium | turbo"}
        }
    },
    "alarm_control_panel": {
        "services": ["alarm_arm_away", "alarm_arm_home", "alarm_arm_night",
                      "alarm_disarm", "alarm_trigger"],
        "params": {
            "alarm_disarm": {"code": "codice di disarmo (se richiesto)"}
        }
    },
    "camera": {
        "services": ["turn_on", "turn_off"],
        "params": {}
    },
    "scene": {
        "services": ["turn_on"],
        "params": {}
    },
    "script": {
        "services": ["turn_on", "turn_off", "toggle"],
        "params": {}
    },
    "automation": {
        "services": ["turn_on", "turn_off", "toggle", "trigger"],
        "params": {}
    },
    "input_boolean": {
        "services": ["turn_on", "turn_off", "toggle"],
        "params": {}
    },
    "input_number": {
        "services": ["set_value"],
        "params": {
            "set_value": {"value": "numero (dipende da min/max configurato)"}
        }
    },
    "input_select": {
        "services": ["select_option"],
        "params": {
            "select_option": {"option": "valore dall'elenco opzioni"}
        }
    },
    "sensor": {
        "services": [],
        "params": {},
        "note": "I sensori sono read-only, puoi solo leggere lo stato"
    },
    "binary_sensor": {
        "services": [],
        "params": {},
        "note": "I sensori binari sono read-only (on/off)"
    },
}


class EntityDiscoveryRequest(BaseModel):
    """Search entities by area, domain, or text pattern."""
    location_id: Optional[str] = None
    room: Optional[str] = Field(default=None, description="Filter by room/area name (e.g., 'soggiorno', 'cucina')")
    zone: Optional[str] = Field(default=None, description="Filter by zone (e.g., 'Zona Giorno', 'Zona Notte')")
    floor: Optional[str] = Field(default=None, description="Filter by floor/piano (e.g., 'Piano 1')")
    domain: Optional[str] = Field(default=None, description="Filter by entity domain (e.g., 'camera', 'light', 'media_player')")
    search: Optional[str] = Field(default=None, description="Free text search in entity names (e.g., 'cam', 'temperatura')")
    limit: int = Field(default=50, description="Max results")


class EntityDiscoveryItem(BaseModel):
    entity_id: str
    friendly_name: str
    domain: str
    room: Optional[str] = None
    device_name: Optional[str] = None
    area: Optional[str] = None
    zone: Optional[str] = None
    state: Optional[str] = None
    available_services: Optional[List[str]] = None


class EntityDiscoveryResponse(BaseModel):
    location_id: str
    count: int
    entities: List[EntityDiscoveryItem] = []
    rooms_found: List[str] = []
    domains_found: List[str] = []


class TtsRequest(BaseModel):
    text: str = Field(..., description="Text to speak")
    speaker_entity: Optional[str] = Field(default=None, description="Target speaker entity_id")
    location_id: Optional[str] = None
    sound: Optional[str] = Field(default=None, description="Sound effect to play (e.g., 'positive', 'negative')")


class TtsResponse(BaseModel):
    success: bool
    message: str


class LocationInfo(BaseModel):
    location_id: str
    name: str
    hass_url: Optional[str] = None
    healthy: bool = False
    has_security: bool = False


class AuditLogRequest(BaseModel):
    event_type: str
    details: str
    user_id: Optional[str] = None
    source: str = Field(default="openclaw")
    severity: str = Field(default="info", description="'info', 'warning', 'error', 'critical'")


class AuditLogResponse(BaseModel):
    success: bool
    log_id: Optional[int] = None


class EntityBulkRequest(BaseModel):
    """Bulk query or action on multiple entities with group filtering."""
    mode: str = Field(..., description="'query' (get live states) or 'action' (execute command)")
    # Filters — all optional, combinable
    location_id: Optional[str] = None
    domain: Optional[str] = Field(default=None, description="Entity domain filter (e.g., 'light', 'cover', 'sensor')")
    room: Optional[str] = Field(default=None, description="Room/area name filter")
    zone: Optional[str] = Field(default=None, description="Zone name filter")
    floor: Optional[str] = Field(default=None, description="Floor/piano filter")
    search: Optional[str] = Field(default=None, description="Free text search in entity names")
    entity_ids: Optional[List[str]] = Field(default=None, description="Explicit entity_id list (bypasses discovery)")
    # Action-mode fields
    action: Optional[str] = Field(default=None, description="Service to call (e.g., 'turn_on', 'turn_off')")
    parameters: Optional[Dict[str, Any]] = Field(default=None, description="Service parameters (e.g., {'brightness': 128})")
    source_channel: str = Field(default="api_tools", description="Source channel for security check")


class EntityBulkItem(BaseModel):
    entity_id: str
    friendly_name: str
    domain: str
    room: Optional[str] = None
    state: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    action_result: Optional[str] = None


class EntityBulkResponse(BaseModel):
    location_id: str
    mode: str
    count: int
    entities: List[EntityBulkItem] = []
    summary: str = ""
    errors: List[str] = []


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/home_control", response_model=HomeControlResponse)
async def tool_home_control(
    req: HomeControlRequest,
    _: None = Depends(verify_openclaw_token)
):
    """
    Control a Home Assistant entity with L1-L4 security check.

    Resolves friendly names to entity_ids, checks security level
    based on domain and source channel, then executes the action.
    """
    try:
        from database import get_entity_map, get_user_location
        from integrations import call_hass_service_by_name

        # Resolve location
        location_id = req.location_id or config.DEFAULT_LOCATION_ID

        # Resolve entity
        entity_id = req.entity_name
        domain = None

        # If it's already an entity_id format (domain.name)
        if "." in req.entity_name:
            domain = req.entity_name.split(".")[0]
            entity_id = req.entity_name
        else:
            # Try to resolve friendly name
            entity_map = get_entity_map(location_id, include_entity_ids=True)
            if entity_map:
                resolved = _resolve_from_entity_map(req.entity_name, entity_map)
                if resolved:
                    entity_id = resolved["entity_id"]
                    domain = resolved.get("domain", entity_id.split(".")[0] if "." in entity_id else None)

        if not domain and "." in entity_id:
            domain = entity_id.split(".")[0]

        if not domain:
            return HomeControlResponse(
                success=False,
                message=f"Impossibile risolvere l'entita: {req.entity_name}",
                approval_required=False
            )

        # Security check
        allowed, reason, domain_level, channel_max = check_security(
            domain=domain,
            service=req.action,
            source_channel=req.source_channel,
            entity_id=entity_id,
        )

        if not allowed:
            if domain_level == SecurityLevel.L3_PROTECTED:
                # Send approval request via JARVIS approval bot
                await _send_approval_request(entity_id, req.action, req.source_channel, service_data=req.parameters)
                return HomeControlResponse(
                    success=False,
                    message=reason,
                    entity_id=entity_id,
                    security_level=f"L{domain_level.value}",
                    approval_required=True,
                )
            return HomeControlResponse(
                success=False,
                message=reason,
                entity_id=entity_id,
                security_level=f"L{domain_level.value}",
                approval_required=False,
            )

        # Execute the action
        result = await _execute_ha_action(entity_id, domain, req.action, req.parameters, location_id)

        return HomeControlResponse(
            success=result.get("success", False),
            message=result.get("message", "Azione eseguita"),
            entity_id=entity_id,
            security_level=f"L{domain_level.value}",
            approval_required=False,
        )

    except Exception as e:
        logger.error(f"home_control error: {e}")
        return HomeControlResponse(
            success=False,
            message=f"Errore: {str(e)}",
            approval_required=False,
        )


@router.post("/speaker_id", response_model=SpeakerIdResponse)
async def tool_speaker_id(
    req: SpeakerIdRequest,
    _: None = Depends(verify_openclaw_token)
):
    """Identify a speaker from audio data using Resemblyzer."""
    try:
        import base64
        import numpy as np
        from voice_recognition import voice_recognizer

        # Decode audio
        audio_bytes = base64.b64decode(req.audio_base64)
        audio_array = np.frombuffer(audio_bytes, dtype=np.float32)

        # Identify speaker
        result = voice_recognizer.identify(audio_array, req.sample_rate)

        if result and result.get("user_id"):
            return SpeakerIdResponse(
                identified=True,
                user_id=result["user_id"],
                user_name=result.get("user_name"),
                confidence=result.get("confidence", 0.0),
            )
        return SpeakerIdResponse(identified=False)

    except Exception as e:
        logger.error(f"speaker_id error: {e}")
        return SpeakerIdResponse(identified=False)


@router.get("/user_context", response_model=UserContextResponse)
async def tool_get_user_context(
    user_id: str,
    _: None = Depends(verify_openclaw_token)
):
    """Get user context: info, location, preferences."""
    try:
        from database import get_user_by_id, get_user_location, get_location, _get_conn

        user = get_user_by_id(user_id)
        user_loc = get_user_location(user_id)
        loc_id = user_loc.location_id if user_loc else config.DEFAULT_LOCATION_ID

        # Recupera nome location
        loc = get_location(loc_id)
        loc_name = loc.name if loc else None

        # Recupera tutte le global preferences come dict
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT pref_key, pref_value FROM global_preferences")
        prefs = {row["pref_key"]: row["pref_value"] for row in c.fetchall()}
        conn.close()

        return UserContextResponse(
            user_id=user_id,
            user_name=user.name if user else None,
            location_id=loc_id,
            location_name=loc_name,
            preferences=prefs or {},
            role=user.role if user else None,
        )

    except Exception as e:
        logger.error(f"user_context error: {e}")
        return UserContextResponse(user_id=user_id)


@router.post("/security", response_model=SecurityActionResponse)
async def tool_security_action(
    req: SecurityActionRequest,
    _: None = Depends(verify_openclaw_token)
):
    """Execute a security action (privacy mode, alarm control, etc.)."""
    try:
        from security import SecurityManager

        if req.action == "set_privacy_mode":
            enabled = req.parameters.get("enabled", True) if req.parameters else True
            SecurityManager.set_privacy_mode(enabled)
            return SecurityActionResponse(
                success=True,
                message=f"Privacy mode {'attivata' if enabled else 'disattivata'}"
            )

        elif req.action == "get_status":
            status = SecurityManager.get_status()
            return SecurityActionResponse(
                success=True,
                message=str(status)
            )

        return SecurityActionResponse(
            success=False,
            message=f"Azione security non riconosciuta: {req.action}"
        )

    except Exception as e:
        logger.error(f"security action error: {e}")
        return SecurityActionResponse(success=False, message=str(e))


@router.post("/memory_query", response_model=MemoryQueryResponse)
async def tool_memory_query(
    req: MemoryQueryRequest,
    _: None = Depends(verify_openclaw_token)
):
    """Query user and location memory for context."""
    try:
        from context_builder import build_full_context

        context_result = build_full_context(
            user_id=req.user_id,
            location_id=req.location_id,
            query=req.query,
            context_type=req.context_type,
        )

        return MemoryQueryResponse(
            context=context_result.get("context", ""),
            sources=context_result.get("sources", {}),
        )

    except Exception as e:
        logger.error(f"memory_query error: {e}")
        return MemoryQueryResponse(context="", sources={"error": str(e)})


@router.post("/entity_resolve", response_model=EntityResolveResponse)
async def tool_entity_resolve(
    req: EntityResolveRequest,
    _: None = Depends(verify_openclaw_token)
):
    """Resolve a friendly name to an entity_id, with capabilities and live state."""
    try:
        from database import get_entity_map

        location_id = req.location_id or config.DEFAULT_LOCATION_ID
        entity_map = get_entity_map(location_id, include_entity_ids=True)

        if not entity_map:
            return EntityResolveResponse(
                found=False,
                alternatives=[],
            )

        resolved = _resolve_from_entity_map(req.friendly_name, entity_map, req.domain_filter)

        if resolved:
            domain = resolved.get("domain", "")
            entity_id = resolved["entity_id"]

            # Capabilities per questo dominio
            caps = DOMAIN_SERVICES.get(domain, {})
            available_services = caps.get("services", [])
            service_params = caps.get("params", {})

            # Stato live da HA (best effort, singola entity)
            state = None
            device_class = None
            try:
                from database import get_location as _get_loc
                import aiohttp
                loc = _get_loc(location_id)
                if loc and loc.hass_url and loc.hass_token:
                    url = f"{loc.hass_url.rstrip('/')}/api/states/{entity_id}"
                    headers = {"Authorization": f"Bearer {loc.hass_token}"}
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                s = await resp.json()
                                state = s.get("state")
                                device_class = s.get("attributes", {}).get("device_class")
            except Exception:
                pass  # Non bloccare se HA non risponde

            return EntityResolveResponse(
                found=True,
                entity_id=entity_id,
                domain=domain,
                friendly_name=resolved.get("friendly_name"),
                area=resolved.get("area"),
                location_id=location_id,
                state=state,
                available_services=available_services if available_services else None,
                service_params=service_params if service_params else None,
                device_class=device_class,
            )

        # Try to find alternatives
        alternatives = _find_alternatives(req.friendly_name, entity_map, limit=5)
        return EntityResolveResponse(
            found=False,
            location_id=location_id,
            alternatives=alternatives,
        )

    except Exception as e:
        logger.error(f"entity_resolve error: {e}")
        return EntityResolveResponse(found=False)


@router.post("/entity_discover", response_model=EntityDiscoveryResponse)
async def tool_entity_discover(
    req: EntityDiscoveryRequest,
    _: None = Depends(verify_openclaw_token)
):
    """
    Discover entities by room, domain, floor, zone, or text search.

    Use this when you need to:
    - List all devices in a room ("cosa c'è in soggiorno?")
    - Find all cameras/lights/etc. ("quali telecamere ci sono?")
    - Search by partial name ("tutto quello che contiene 'cam'")
    - Browse a floor or zone ("dispositivi al Piano 1")
    """
    try:
        from database import _get_conn

        location_id = req.location_id or config.DEFAULT_LOCATION_ID

        conn = _get_conn()
        c = conn.cursor()

        # Costruisci query con filtri
        query = """
            SELECT entity_id, entity_name, entity_type, room, device_name, area, zone
            FROM entity_maps
            WHERE location_id = ?
        """
        params: list = [location_id]

        if req.room:
            query += " AND LOWER(room) LIKE LOWER(?)"
            params.append(f"%{req.room}%")

        if req.zone:
            query += " AND LOWER(area) LIKE LOWER(?)"
            params.append(f"%{req.zone}%")

        if req.floor:
            query += " AND LOWER(zone) LIKE LOWER(?)"
            params.append(f"%{req.floor}%")

        if req.domain:
            query += " AND entity_type = ?"
            params.append(req.domain)

        if req.search:
            query += " AND (LOWER(entity_name) LIKE LOWER(?) OR LOWER(entity_id) LIKE LOWER(?))"
            params.append(f"%{req.search}%")
            params.append(f"%{req.search}%")

        query += " ORDER BY room, device_name, entity_type LIMIT ?"
        params.append(req.limit)

        c.execute(query, params)
        rows = c.fetchall()
        conn.close()

        # Costruisci risultati
        entities = []
        rooms_set = set()
        domains_set = set()

        for row in rows:
            domain = row["entity_type"]
            caps = DOMAIN_SERVICES.get(domain, {})

            entities.append(EntityDiscoveryItem(
                entity_id=row["entity_id"] or "",
                friendly_name=row["entity_name"],
                domain=domain,
                room=row["room"],
                device_name=row["device_name"],
                area=row["area"],
                zone=row["zone"],
                available_services=caps.get("services") if caps.get("services") else None,
            ))

            if row["room"]:
                rooms_set.add(row["room"])
            domains_set.add(domain)

        return EntityDiscoveryResponse(
            location_id=location_id,
            count=len(entities),
            entities=entities,
            rooms_found=sorted(rooms_set),
            domains_found=sorted(domains_set),
        )

    except Exception as e:
        logger.error(f"entity_discover error: {e}")
        return EntityDiscoveryResponse(location_id=req.location_id or "", count=0)


# ── Key attributes to extract per domain (keeps response compact) ──────────
DOMAIN_KEY_ATTRIBUTES: Dict[str, List[str]] = {
    "light": ["brightness", "color_temp_kelvin", "rgb_color", "color_mode"],
    "climate": ["current_temperature", "temperature", "hvac_action", "hvac_modes"],
    "cover": ["current_position"],
    "media_player": ["media_title", "source", "volume_level", "app_name"],
    "sensor": ["unit_of_measurement", "device_class"],
    "binary_sensor": ["device_class"],
    "fan": ["percentage", "preset_mode"],
    "vacuum": ["status", "battery_level"],
}


@router.post("/entity_bulk", response_model=EntityBulkResponse)
async def tool_entity_bulk(
    req: EntityBulkRequest,
    _: None = Depends(verify_openclaw_token)
):
    """
    Bulk query or action on multiple entities.

    **Query mode**: Returns live states for all matching entities in a single HA call.
    Use for: "quali luci sono accese?", "temperatura di tutte le stanze", "stato tapparelle".

    **Action mode**: Executes a service on all matching entities in a single HA call.
    Use for: "spegni tutte le luci del soggiorno", "chiudi le tapparelle zona notte".

    Filters (room, zone, floor, domain, search) are all optional and combinable.
    """
    try:
        from database import _get_conn
        from multi_ha import multi_ha

        location_id = req.location_id or config.DEFAULT_LOCATION_ID
        errors: List[str] = []

        # ── 1. DISCOVERY: find matching entities ─────────────────────────
        if req.entity_ids:
            # Explicit entity_ids — fetch their info from DB
            conn = _get_conn()
            c = conn.cursor()
            placeholders = ",".join("?" for _ in req.entity_ids)
            c.execute(f"""
                SELECT entity_id, entity_name, entity_type, room, device_name, area, zone
                FROM entity_maps
                WHERE location_id = ? AND entity_id IN ({placeholders})
            """, [location_id] + req.entity_ids)
            rows = c.fetchall()
            conn.close()
        else:
            # Filter-based discovery (reuse entity_discover SQL pattern)
            conn = _get_conn()
            c = conn.cursor()
            query = """
                SELECT entity_id, entity_name, entity_type, room, device_name, area, zone
                FROM entity_maps
                WHERE location_id = ? AND entity_id IS NOT NULL
            """
            params: list = [location_id]

            if req.domain:
                query += " AND entity_type = ?"
                params.append(req.domain)

            if req.room:
                query += " AND LOWER(room) LIKE LOWER(?)"
                params.append(f"%{req.room}%")

            if req.zone:
                query += " AND LOWER(area) LIKE LOWER(?)"
                params.append(f"%{req.zone}%")

            if req.floor:
                query += " AND LOWER(zone) LIKE LOWER(?)"
                params.append(f"%{req.floor}%")

            if req.search:
                query += " AND (LOWER(entity_name) LIKE LOWER(?) OR LOWER(entity_id) LIKE LOWER(?))"
                params.append(f"%{req.search}%")
                params.append(f"%{req.search}%")

            query += " ORDER BY room, entity_type, entity_name LIMIT 200"
            c.execute(query, params)
            rows = c.fetchall()
            conn.close()

        if not rows:
            return EntityBulkResponse(
                location_id=location_id,
                mode=req.mode,
                count=0,
                summary="Nessuna entity trovata con i filtri specificati.",
            )

        # Build entity list with entity_ids
        discovered = []
        for row in rows:
            discovered.append({
                "entity_id": row["entity_id"],
                "friendly_name": row["entity_name"],
                "domain": row["entity_type"],
                "room": row["room"],
            })

        entity_ids = [e["entity_id"] for e in discovered]

        # ── 2. QUERY MODE: fetch live states ─────────────────────────────
        if req.mode == "query":
            states = await multi_ha.get_states_bulk(location_id, entity_ids)

            items = []
            for ent in discovered:
                eid = ent["entity_id"]
                live = states.get(eid, {})
                domain = ent["domain"]

                # Extract key attributes for this domain
                key_attrs = DOMAIN_KEY_ATTRIBUTES.get(domain, [])
                raw_attrs = live.get("attributes", {})
                filtered_attrs = {k: raw_attrs[k] for k in key_attrs if k in raw_attrs}

                items.append(EntityBulkItem(
                    entity_id=eid,
                    friendly_name=ent["friendly_name"],
                    domain=domain,
                    room=ent["room"],
                    state=live.get("state"),
                    attributes=filtered_attrs if filtered_attrs else None,
                ))

            # Generate summary
            summary = _build_query_summary(items, req)
            return EntityBulkResponse(
                location_id=location_id,
                mode="query",
                count=len(items),
                entities=items,
                summary=summary,
                errors=errors,
            )

        # ── 3. ACTION MODE: execute bulk service ─────────────────────────
        elif req.mode == "action":
            if not req.action:
                return EntityBulkResponse(
                    location_id=location_id,
                    mode="action",
                    count=0,
                    summary="Errore: campo 'action' obbligatorio in modalita action.",
                    errors=["Missing 'action' field"],
                )

            # Security check per domain — group by domain
            domains_in_batch = {}
            for ent in discovered:
                d = ent["domain"]
                if d not in domains_in_batch:
                    domains_in_batch[d] = []
                domains_in_batch[d].append(ent)

            allowed_entities = []
            for domain, ents in domains_in_batch.items():
                allowed, reason, domain_level, channel_max = check_security(
                    domain=domain,
                    service=req.action,
                    source_channel=req.source_channel,
                )

                if not allowed:
                    errors.append(f"{domain}: {reason}")
                    logger.warning(f"Bulk action blocked for domain {domain}: {reason}")
                    continue

                allowed_entities.extend(ents)

            if not allowed_entities:
                return EntityBulkResponse(
                    location_id=location_id,
                    mode="action",
                    count=0,
                    summary="Nessuna entity autorizzata per questa azione.",
                    errors=errors,
                )

            # Group by domain for execution (HA call_service needs same domain)
            exec_groups: Dict[str, List[str]] = {}
            for ent in allowed_entities:
                d = ent["domain"]
                if d not in exec_groups:
                    exec_groups[d] = []
                exec_groups[d].append(ent["entity_id"])

            total_ok = 0
            total_fail = 0
            items = []
            for domain, eids in exec_groups.items():
                success, message = await multi_ha.call_service_bulk(
                    location_id, domain, req.action, eids, req.parameters
                )

                for eid in eids:
                    ent_info = next((e for e in allowed_entities if e["entity_id"] == eid), {})
                    items.append(EntityBulkItem(
                        entity_id=eid,
                        friendly_name=ent_info.get("friendly_name", ""),
                        domain=domain,
                        room=ent_info.get("room"),
                        action_result="ok" if success else message,
                    ))
                    if success:
                        total_ok += 1
                    else:
                        total_fail += 1

            summary = _build_action_summary(
                total_ok, total_fail, req.action, allowed_entities, req
            )

            return EntityBulkResponse(
                location_id=location_id,
                mode="action",
                count=len(items),
                entities=items,
                summary=summary,
                errors=errors,
            )

        else:
            return EntityBulkResponse(
                location_id=location_id,
                mode=req.mode,
                count=0,
                summary=f"Modalita '{req.mode}' non valida. Usa 'query' o 'action'.",
                errors=[f"Invalid mode: {req.mode}"],
            )

    except Exception as e:
        logger.error(f"entity_bulk error: {e}", exc_info=True)
        return EntityBulkResponse(
            location_id=req.location_id or "",
            mode=req.mode,
            count=0,
            summary=f"Errore: {str(e)}",
            errors=[str(e)],
        )


@router.post("/tts", response_model=TtsResponse)
async def tool_tts(
    req: TtsRequest,
    _: None = Depends(verify_openclaw_token)
):
    """Text-to-speech passthrough — speak via Alexa/speaker."""
    try:
        from integrations import speak_with_sound

        # Resolve speaker
        speaker_entity = req.speaker_entity
        if not speaker_entity:
            # Use default speaker for location
            speaker_entity = config.DEFAULT_FALLBACK_SPEAKER

        sound = None
        if req.sound == "positive":
            sound = config.SOUND_POSITIVE_DEFAULT
        elif req.sound == "negative":
            sound = config.SOUND_NEGATIVE_DEFAULT
        elif req.sound == "neutral":
            sound = config.SOUND_NEUTRAL_DEFAULT
        elif req.sound:
            sound = req.sound

        await speak_with_sound(
            text=req.text,
            speaker=speaker_entity,
            sound=sound,
            location_id=req.location_id,
        )

        return TtsResponse(success=True, message="TTS inviato")

    except Exception as e:
        logger.error(f"tts error: {e}")
        return TtsResponse(success=False, message=str(e))


@router.get("/locations", response_model=List[LocationInfo])
async def tool_get_locations(
    _: None = Depends(verify_openclaw_token)
):
    """List all configured locations with health status."""
    try:
        from database import get_all_locations
        from multi_ha import multi_ha

        locations = get_all_locations()
        result = []

        for loc in locations:
            health = False
            if loc.hass_url:
                try:
                    healthy, _ = await multi_ha.health_check(loc.id)
                    health = healthy
                except Exception:
                    health = False

            result.append(LocationInfo(
                location_id=loc.id,
                name=loc.name or loc.id,
                hass_url=loc.hass_url,
                healthy=health,
                has_security=loc.has_security,
            ))

        return result

    except Exception as e:
        logger.error(f"locations error: {e}")
        return []


@router.post("/audit_log", response_model=AuditLogResponse)
async def tool_audit_log(
    req: AuditLogRequest,
    _: None = Depends(verify_openclaw_token)
):
    """Log an event to the audit trail."""
    try:
        from database import log_event

        log_id = log_event(
            event_type=req.event_type,
            details=req.details,
            user_id=req.user_id,
            source=req.source,
        )

        return AuditLogResponse(success=True, log_id=log_id)

    except Exception as e:
        logger.error(f"audit_log error: {e}")
        return AuditLogResponse(success=False)


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_entities_from_map(entity_map: Dict, path: str = "") -> List[Dict[str, str]]:
    """
    Raccoglie ricorsivamente tutte le entity dalla entity_map gerarchica.

    Supporta qualsiasi profondità:
    - Zone → Area → Room → EntityType → [entities]
    - Zone → Area → Room → Device → EntityType → [entities]

    Ritorna una lista flat di dict con: entity_id, name, domain, path.
    """
    results = []
    for key, value in entity_map.items():
        current_path = f"{path} > {key}" if path else key
        if isinstance(value, dict):
            # Livello intermedio — ricorsione
            results.extend(_collect_entities_from_map(value, current_path))
        elif isinstance(value, list):
            # Foglia — lista di entity (strings o dicts)
            # 'key' è il domain/entity_type (es. "light", "sensor")
            domain = key
            for entity in value:
                if isinstance(entity, dict):
                    results.append({
                        "entity_id": entity.get("entity_id", ""),
                        "name": entity.get("name", entity.get("friendly_name", "")),
                        "domain": domain,
                        "path": current_path,
                    })
                elif isinstance(entity, str):
                    results.append({
                        "entity_id": "",
                        "name": entity,
                        "domain": domain,
                        "path": current_path,
                    })
    return results


def _resolve_from_entity_map(
    friendly_name: str,
    entity_map: Dict,
    domain_filter: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """
    Resolve a friendly name from the entity map.
    Supporta strutture gerarchiche di qualsiasi profondità.
    """
    friendly_lower = friendly_name.lower().strip()
    all_entities = _collect_entities_from_map(entity_map)

    for ent in all_entities:
        if domain_filter and ent["domain"] != domain_filter:
            continue

        ent_name_lower = ent["name"].lower().strip()
        if friendly_lower == ent_name_lower or friendly_lower in ent_name_lower or ent_name_lower in friendly_lower:
            return {
                "entity_id": ent["entity_id"],
                "domain": ent["domain"],
                "friendly_name": ent["name"],
                "area": ent["path"],
            }

    return None


def _find_alternatives(
    friendly_name: str,
    entity_map: Dict,
    limit: int = 5
) -> List[Dict[str, str]]:
    """Find similar entity names for suggestions."""
    friendly_lower = friendly_name.lower().strip()
    words = set(friendly_lower.split())
    candidates = []

    all_entities = _collect_entities_from_map(entity_map)

    for ent in all_entities:
        if not ent["name"]:
            continue
        entity_words = set(ent["name"].lower().split())
        overlap = len(words & entity_words)
        if overlap > 0:
            candidates.append({
                "entity_id": ent["entity_id"],
                "friendly_name": ent["name"],
                "area": ent["path"],
                "score": overlap,
            })

    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    return [{"entity_id": c["entity_id"], "friendly_name": c["friendly_name"], "area": c["area"]}
            for c in candidates[:limit]]


async def _execute_ha_action(
    entity_id: str,
    domain: str,
    action: str,
    parameters: Optional[Dict[str, Any]],
    location_id: str
) -> Dict[str, Any]:
    """Execute a Home Assistant action."""
    try:
        from integrations import call_hass_service

        service_data = {"entity_id": entity_id}
        if parameters:
            service_data.update(parameters)

        success, message = await call_hass_service(
            location_id=location_id,
            domain=domain,
            service=action,
            service_data=service_data,
        )

        return {"success": success, "message": message if not success else f"Eseguito {domain}.{action} su {entity_id}"}

    except Exception as e:
        return {"success": False, "message": str(e)}


def _build_query_summary(items: List[EntityBulkItem], req) -> str:
    """Build Italian human-readable summary for query results."""
    if not items:
        return "Nessuna entity trovata."

    # Group by state
    on_items = [i for i in items if i.state and i.state.lower() in ("on", "heat", "cool", "auto", "open", "playing")]
    off_items = [i for i in items if i.state and i.state.lower() in ("off", "closed", "idle", "standby", "paused")]
    other_items = [i for i in items if i not in on_items and i not in off_items]

    domain = req.domain or "entity"
    domain_label = {
        "light": "luci", "cover": "tapparelle", "climate": "termostati",
        "media_player": "media player", "sensor": "sensori",
        "binary_sensor": "sensori binari", "switch": "switch",
        "fan": "ventilatori", "vacuum": "aspirapolvere",
    }.get(domain, domain)

    scope = req.room or req.zone or req.floor or "tutta la casa"

    if domain in ("sensor", "binary_sensor"):
        # For sensors, list values
        parts = []
        for i in items:
            unit = (i.attributes or {}).get("unit_of_measurement", "")
            val = f"{i.friendly_name}: {i.state}{' ' + unit if unit else ''}"
            parts.append(val)
        return f"{len(items)} {domain_label} in {scope}: " + ", ".join(parts[:20])

    on_names = [i.friendly_name for i in on_items]
    total = len(items)
    on_count = len(on_items)

    if on_count == 0:
        return f"Tutte le {total} {domain_label} in {scope} sono spente/chiuse."
    elif on_count == total:
        return f"Tutte le {total} {domain_label} in {scope} sono accese/aperte: {', '.join(on_names[:15])}."
    else:
        return (f"{on_count} {domain_label} accese/aperte su {total} in {scope}: "
                f"{', '.join(on_names[:15])}.")


def _build_action_summary(
    total_ok: int, total_fail: int, action: str,
    entities: list, req
) -> str:
    """Build Italian human-readable summary for action results."""
    action_labels = {
        "turn_on": "Accese", "turn_off": "Spente", "toggle": "Commutate",
        "open_cover": "Aperte", "close_cover": "Chiuse", "stop_cover": "Fermate",
        "lock": "Bloccate", "unlock": "Sbloccate",
    }
    action_label = action_labels.get(action, f"Eseguito {action} su")

    scope = req.room or req.zone or req.floor or "tutta la casa"

    if total_fail == 0:
        return f"{action_label} {total_ok} entity in {scope}."
    else:
        return f"{action_label} {total_ok} entity in {scope}. {total_fail} errori."


async def _send_approval_request(entity_id: str, action: str, source_channel: str, service_data: dict = None):
    """Send L3 approval request via JARVIS approval bot (separate from OpenClaw)."""
    try:
        import uuid
        from integrations import send_telegram_approval
        from database import save_action

        action_id = str(uuid.uuid4())[:8]

        # Save pending action so it can be executed after approval
        save_action(action_id, {
            "domain": entity_id.split(".")[0] if "." in entity_id else "unknown",
            "action": action,
            "entity_id": entity_id,
            "data": service_data or {},
            "source_channel": source_channel,
        })

        message = (
            f"Azione: {action}\n"
            f"Entita: {entity_id}\n"
            f"Canale: {source_channel}\n"
            f"Timestamp: {time.strftime('%H:%M:%S')}"
        )

        await send_telegram_approval(message, action_id)
        logger.info(f"L3 approval request sent for {entity_id} (action_id={action_id})")

    except Exception as e:
        logger.error(f"Failed to send approval request: {e}")
