"""
JARVIS Admin API
- System state & preferences management
- Audit log viewer
- Device failures management
- Pending actions viewer
- Query cache management
- Health checks for all services
- Live metrics
"""

import asyncio
import time
import json
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Query, Depends, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import config
from auth_api import require_admin, get_current_user
from backup import create_backup, validate_backup, restore_backup, get_backup_info
from database import (
    _get_conn, DB_PATH,
    get_all_users,
    get_all_locations, get_location, create_location, update_location, delete_location,
    get_entity_map, import_entity_map_json, export_entity_map_json,
    import_hierarchy_json, get_hierarchy_mapping,
    get_entity_map_stats, clear_entity_map, get_rooms_for_location,
    get_entity_tree, update_entity_visibility,
    save_visibility_snapshot, restore_visibility_snapshot,
    # Voice devices functions
    get_all_voice_devices, get_voice_device, upsert_voice_device,
    delete_voice_device, get_unconfigured_voice_devices,
    get_configured_voice_devices, get_voice_device_stats,
    get_voice_devices_by_location,
    # Access log functions
    get_access_log, get_access_log_stats, get_auth_attempts_log
)

# ===========================================================================
# PYDANTIC MODELS
# ===========================================================================

class KeyValueUpdate(BaseModel):
    value: str


class PreferenceUpdate(BaseModel):
    value: str
    description: Optional[str] = None


class CacheEntryCreate(BaseModel):
    query_text: str
    response: str


class CacheEntryUpdate(BaseModel):
    response: str


class LocationCreate(BaseModel):
    id: str
    name: str
    city: Optional[str] = None
    hass_url: str
    hass_token: str
    entity_map_path: Optional[str] = None
    has_security: bool = False
    keywords: Optional[List[str]] = None  # Es: ["wagmi", "villa", "napoli"]


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    hass_url: Optional[str] = None
    hass_token: Optional[str] = None
    entity_map_path: Optional[str] = None
    keywords: Optional[List[str]] = None
    has_security: Optional[bool] = None
    enabled: Optional[bool] = None


class VoiceDeviceCreate(BaseModel):
    device_id: str
    friendly_name: str
    location_id: str
    output_speaker: Optional[str] = None
    fallback_speaker: Optional[str] = None
    fallback_telegram: bool = True
    fallback_local_speaker: bool = True
    use_internal_speaker: bool = False
    enabled: bool = True
    notes: Optional[str] = None
    wake_word_sensitivity: Optional[float] = 0.82
    speaker_volume: Optional[int] = 80
    volume_speaker: Optional[str] = None  # separate entity for volume control


class VoiceDeviceUpdate(BaseModel):
    friendly_name: Optional[str] = None
    location_id: Optional[str] = None
    output_speaker: Optional[str] = None
    fallback_speaker: Optional[str] = None
    fallback_telegram: Optional[bool] = None
    fallback_local_speaker: Optional[bool] = None
    use_internal_speaker: Optional[bool] = None
    enabled: Optional[bool] = None
    notes: Optional[str] = None
    wake_word_sensitivity: Optional[float] = None
    speaker_volume: Optional[int] = None
    volume_speaker: Optional[str] = None  # separate entity for volume control


# ===========================================================================
# METRICS COLLECTOR (Singleton)
# ===========================================================================

@dataclass
class MetricsCollector:
    """Collects and stores latency metrics for various operations."""

    # Store last N samples for each metric
    routing_latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    reasoning_latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    stt_latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    speaker_id_latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    hass_latencies: deque = field(default_factory=lambda: deque(maxlen=100))

    # Counters
    total_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

    # Last health check results
    last_health_check: Dict[str, Any] = field(default_factory=dict)
    last_health_check_time: float = 0

    def record_routing(self, latency_ms: float):
        self.routing_latencies.append(latency_ms)
        self.total_requests += 1

    def record_reasoning(self, latency_ms: float):
        self.reasoning_latencies.append(latency_ms)

    def record_stt(self, latency_ms: float):
        self.stt_latencies.append(latency_ms)

    def record_speaker_id(self, latency_ms: float):
        self.speaker_id_latencies.append(latency_ms)

    def record_hass(self, latency_ms: float):
        self.hass_latencies.append(latency_ms)

    def record_cache_hit(self):
        self.cache_hits += 1
        self.total_requests += 1

    def record_cache_miss(self):
        self.cache_misses += 1

    def _calc_stats(self, samples: deque) -> Dict[str, float]:
        if not samples:
            return {"avg": 0, "min": 0, "max": 0, "count": 0}
        samples_list = list(samples)
        return {
            "avg": round(sum(samples_list) / len(samples_list), 2),
            "min": round(min(samples_list), 2),
            "max": round(max(samples_list), 2),
            "count": len(samples_list)
        }

    def get_stats(self) -> Dict[str, Any]:
        cache_total = self.cache_hits + self.cache_misses
        return {
            "routing": self._calc_stats(self.routing_latencies),
            "reasoning": self._calc_stats(self.reasoning_latencies),
            "stt": self._calc_stats(self.stt_latencies),
            "speaker_id": self._calc_stats(self.speaker_id_latencies),
            "hass": self._calc_stats(self.hass_latencies),
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "hit_rate": round(self.cache_hits / cache_total * 100, 1) if cache_total > 0 else 0
            },
            "total_requests": self.total_requests
        }


# Global metrics instance
metrics = MetricsCollector()


# ===========================================================================
# ROUTER
# ===========================================================================

# Nota: require_admin come dependency globale protegge tutti gli endpoint
# Alcuni endpoint come /health e /metrics potrebbero essere accessibili senza auth
# se necessario per monitoring esterno - in quel caso rimuovere la dependency globale
# e aggiungere require_admin singolarmente agli endpoint sensibili

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)]
)


# ===========================================================================
# SSE (Server-Sent Events) — Real-time dashboard updates
# ===========================================================================

@router.get("/events")
async def sse_events(request: Request):
    """
    Endpoint SSE per eventi real-time alla dashboard.
    Protetto da require_admin (dependency globale del router).
    Il client riceve eventi tipizzati: metrics_update, health_update,
    new_request, audit_event, pending_action, heartbeat.
    """
    from event_bus import event_bus

    user = require_admin(request)
    client = await event_bus.connect(user_id=user.id)

    async def event_generator():
        try:
            # Evento iniziale di connessione
            yield f"event: connected\ndata: {json.dumps({'client_count': event_bus.client_count})}\n\n"

            while True:
                try:
                    # Attendi evento con timeout per heartbeat
                    message = await asyncio.wait_for(client.queue.get(), timeout=30.0)
                    event_type = message["type"]
                    data_json = json.dumps(message["data"], default=str)
                    yield f"event: {event_type}\ndata: {data_json}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat ogni 30s per mantenere la connessione
                    yield f"event: heartbeat\ndata: {json.dumps({'ts': time.time()})}\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            await event_bus.disconnect(client)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ===========================================================================
# SYSTEM STATE ENDPOINTS
# ===========================================================================

@router.get("/system-state")
async def get_all_system_state() -> List[Dict[str, str]]:
    """Get all system state key-value pairs."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT key, value FROM system_state ORDER BY key")
    rows = c.fetchall()
    conn.close()
    return [{"key": row["key"], "value": row["value"]} for row in rows]


@router.get("/system-state/{key}")
async def get_system_state(key: str) -> Dict[str, str]:
    """Get a specific system state value."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM system_state WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"Key '{key}' not found")
    return {"key": key, "value": row["value"]}


@router.put("/system-state/{key}")
async def set_system_state(key: str, data: KeyValueUpdate) -> Dict[str, str]:
    """Set a system state value."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
              (key, data.value))
    conn.commit()
    conn.close()
    return {"key": key, "value": data.value}


@router.delete("/system-state/{key}")
async def delete_system_state(key: str) -> Dict[str, str]:
    """Delete a system state key."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM system_state WHERE key = ?", (key,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    if affected == 0:
        raise HTTPException(404, f"Key '{key}' not found")
    return {"status": "deleted", "key": key}


# ===========================================================================
# GLOBAL PREFERENCES ENDPOINTS
# ===========================================================================

# Preference metadata (descriptions for UI)
# NOTA: Questi sono i parametri configurabili dall'utente via dashboard.
# I valori di default sono definiti qui e in config.py (per backward compatibility).
PREFERENCE_METADATA = {
    # --- MODALITÀ ---
    "dnd_mode": {
        "description": "Modalita Non Disturbare - disabilita risposte vocali",
        "type": "boolean",
        "default": "False"
    },
    "silent_hour_start": {
        "description": "Ora inizio silenzio notturno (0-23)",
        "type": "integer",
        "min": 0,
        "max": 23,
        "default": "23"
    },
    "silent_hour_end": {
        "description": "Ora fine silenzio notturno (0-23)",
        "type": "integer",
        "min": 0,
        "max": 23,
        "default": "7"
    },

    # --- AI THRESHOLDS ---
    "confidence_threshold_high": {
        "description": "Soglia confidenza ALTA per azioni automatiche (0.0-1.0). Sopra questa soglia le azioni HOME_CONTROL vengono eseguite direttamente.",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": "0.85"
    },
    "confidence_threshold_low": {
        "description": "Soglia confidenza BASSA (0.0-1.0). Sotto questa soglia la richiesta viene inoltrata a OpenClaw/Gemini.",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": "0.50"
    },

    # --- AUDIO FEEDBACK ---
    "ha_quick_feedback": {
        "description": "Feedback audio rapido per comandi HA vocali (suono breve invece di risposta vocale completa)",
        "type": "boolean",
        "default": "True"
    },
    "sound_positive": {
        "description": "Suono Alexa POSITIVO (conferma OK). ID dalla Sound Library",
        "type": "string",
        "default": "amzn_ui_sfx_gameshow_positive_response_01"
    },
    "sound_neutral": {
        "description": "Suono Alexa NEUTRALE (risposta reasoning). ID dalla Sound Library",
        "type": "string",
        "default": "amzn_ui_sfx_gameshow_neutral_response_03"
    },
    "sound_negative": {
        "description": "Suono Alexa NEGATIVO (errori). ID dalla Sound Library",
        "type": "string",
        "default": "amzn_ui_sfx_gameshow_negative_response_01"
    },

    # --- SECURITY ---
    "security_suppression_time": {
        "description": "Tempo di soppressione allarmi dopo riconoscimento familiare (secondi)",
        "type": "integer",
        "min": 60,
        "max": 3600,
        "default": "600"
    },

    # --- TIMEOUTS ---
    "approval_timeout": {
        "description": "Timeout per azioni in attesa di approvazione (secondi)",
        "type": "integer",
        "min": 300,
        "max": 86400,
        "default": "3600"
    }
}


@router.get("/preferences")
async def get_all_preferences() -> List[Dict[str, Any]]:
    """Get all global preferences with metadata."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT pref_key, pref_value FROM global_preferences ORDER BY pref_key")
    rows = c.fetchall()
    conn.close()

    result = []
    for row in rows:
        key = row["pref_key"]
        meta = PREFERENCE_METADATA.get(key, {"description": "", "type": "string"})
        pref_data = {
            "key": key,
            "value": row["pref_value"],
            "description": meta.get("description", ""),
            "type": meta.get("type", "string"),
            "editable": key in PREFERENCE_METADATA
        }
        # Include options for select type
        if meta.get("type") == "select" and "options" in meta:
            pref_data["options"] = meta["options"]
        result.append(pref_data)

    return result


@router.get("/preferences/{key}")
async def get_preference(key: str) -> Dict[str, Any]:
    """Get a specific preference."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT pref_value FROM global_preferences WHERE pref_key = ?", (key,))
    row = c.fetchone()
    conn.close()

    meta = PREFERENCE_METADATA.get(key, {"description": "", "type": "string"})
    value = row["pref_value"] if row else meta.get("default", "")

    return {
        "key": key,
        "value": value,
        "description": meta.get("description", ""),
        "type": meta.get("type", "string")
    }


@router.put("/preferences/{key}")
async def set_preference(key: str, data: PreferenceUpdate) -> Dict[str, Any]:
    """Set a preference value."""
    # Validate if it's a known preference
    meta = PREFERENCE_METADATA.get(key)
    if meta:
        # Type validation
        pref_type = meta.get("type", "string")
        try:
            if pref_type == "integer":
                val = int(data.value)
                if "min" in meta and val < meta["min"]:
                    raise ValueError(f"Value must be >= {meta['min']}")
                if "max" in meta and val > meta["max"]:
                    raise ValueError(f"Value must be <= {meta['max']}")
            elif pref_type == "float":
                val = float(data.value)
                if "min" in meta and val < meta["min"]:
                    raise ValueError(f"Value must be >= {meta['min']}")
                if "max" in meta and val > meta["max"]:
                    raise ValueError(f"Value must be <= {meta['max']}")
            elif pref_type == "boolean":
                if data.value.lower() not in ["true", "false", "0", "1"]:
                    raise ValueError("Value must be true/false")
            elif pref_type == "select":
                options = meta.get("options", [])
                if data.value not in options:
                    raise ValueError(f"Value must be one of: {', '.join(options)}")
        except ValueError as e:
            raise HTTPException(400, str(e))

    conn = _get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO global_preferences (pref_key, pref_value) VALUES (?, ?)",
              (key, data.value))
    conn.commit()
    conn.close()

    return {"key": key, "value": data.value, "status": "updated"}


# ===========================================================================
# AUDIT LOG ENDPOINTS
# ===========================================================================

@router.get("/audit-log")
async def get_audit_log(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    category: Optional[str] = None,
    speaker_name: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None
) -> Dict[str, Any]:
    """Get audit log with filtering and pagination."""
    conn = _get_conn()
    c = conn.cursor()

    # Build query
    query = "SELECT id, timestamp, category, message, speaker_name FROM audit_log"
    count_query = "SELECT COUNT(*) as total FROM audit_log"
    params = []
    conditions = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if speaker_name:
        conditions.append("speaker_name LIKE ?")
        params.append(f"%{speaker_name}%")
    if start_time:
        conditions.append("timestamp >= ?")
        params.append(start_time)
    if end_time:
        conditions.append("timestamp <= ?")
        params.append(end_time)

    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)
        query += where_clause
        count_query += where_clause

    # Get total count
    c.execute(count_query, params)
    count_row = c.fetchone()
    total = count_row["total"] if count_row else 0

    # Get paginated results
    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    # Get unique categories for filter dropdown
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT category FROM audit_log ORDER BY category")
    categories = [row["category"] for row in c.fetchall()]
    conn.close()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "categories": categories,
        "entries": [{
            "id": row["id"],
            "timestamp": row["timestamp"],
            "datetime": datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
            "category": row["category"],
            "message": row["message"],
            "speaker": row["speaker_name"] or "Sistema"
        } for row in rows]
    }


@router.delete("/audit-log")
async def clear_audit_log(
    before_timestamp: Optional[float] = None,
    category: Optional[str] = None
) -> Dict[str, Any]:
    """Clear audit log entries (with optional filters)."""
    conn = _get_conn()
    c = conn.cursor()

    query = "DELETE FROM audit_log"
    params = []
    conditions = []

    if before_timestamp:
        conditions.append("timestamp < ?")
        params.append(before_timestamp)
    if category:
        conditions.append("category = ?")
        params.append(category)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    c.execute(query, params)
    deleted = c.rowcount
    conn.commit()
    conn.close()

    return {"status": "cleared", "deleted_count": deleted}


# ===========================================================================
# DEVICE FAILURES ENDPOINTS
# ===========================================================================

@router.get("/device-failures")
async def get_device_failures() -> List[Dict[str, Any]]:
    """Get all devices with failures."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT entity_id, count, last_error, timestamp
        FROM device_failures
        ORDER BY count DESC, timestamp DESC
    """)
    rows = c.fetchall()
    conn.close()

    return [{
        "entity_id": row["entity_id"],
        "failure_count": row["count"],
        "last_error": row["last_error"],
        "last_failure": row["timestamp"],
        "last_failure_datetime": datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
    } for row in rows]


@router.delete("/device-failures/{entity_id:path}")
async def reset_device_failure(entity_id: str) -> Dict[str, str]:
    """Reset failure counter for a device."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM device_failures WHERE entity_id = ?", (entity_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected == 0:
        raise HTTPException(404, f"Device '{entity_id}' not found in failures")
    return {"status": "reset", "entity_id": entity_id}


@router.delete("/device-failures")
async def reset_all_device_failures() -> Dict[str, Any]:
    """Reset all device failures."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM device_failures")
    deleted = c.rowcount
    conn.commit()
    conn.close()

    return {"status": "reset_all", "deleted_count": deleted}


# ===========================================================================
# PENDING ACTIONS ENDPOINTS
# ===========================================================================

@router.get("/pending-actions")
async def get_pending_actions() -> List[Dict[str, Any]]:
    """Get all pending actions awaiting approval."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT pa.action_id, pa.payload, pa.timestamp, pa.requested_by, u.name as requester_name
        FROM pending_actions pa
        LEFT JOIN users u ON pa.requested_by = u.id
        ORDER BY pa.timestamp DESC
    """)
    rows = c.fetchall()
    conn.close()

    return [{
        "action_id": row["action_id"],
        "payload": json.loads(row["payload"]),
        "timestamp": row["timestamp"],
        "datetime": datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
        "requested_by": row["requester_name"] or "Sistema",
        "age_seconds": int(time.time() - row["timestamp"])
    } for row in rows]


@router.delete("/pending-actions/{action_id}")
async def cancel_pending_action(action_id: str) -> Dict[str, str]:
    """Cancel/delete a pending action."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM pending_actions WHERE action_id = ?", (action_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected == 0:
        raise HTTPException(404, f"Action '{action_id}' not found")
    return {"status": "cancelled", "action_id": action_id}


# ===========================================================================
# QUERY CACHE ENDPOINTS
# ===========================================================================

@router.get("/cache")
async def get_cache_entries(
    limit: int = Query(50, ge=1, le=500),
    auto_learned_only: bool = False
) -> Dict[str, Any]:
    """Get cached query-response pairs."""
    conn = _get_conn()
    c = conn.cursor()

    query = "SELECT query_hash, query_text, response, hit_count, last_used, auto_learned FROM query_cache"
    if auto_learned_only:
        query += " WHERE auto_learned = TRUE"
    query += " ORDER BY hit_count DESC, last_used DESC LIMIT ?"

    c.execute(query, (limit,))
    rows = c.fetchall()

    # Get stats
    c.execute("SELECT COUNT(*) as total, SUM(hit_count) as total_hits FROM query_cache")
    stats = c.fetchone()

    conn.close()

    return {
        "total_entries": stats["total"] if stats else 0,
        "total_hits": (stats["total_hits"] or 0) if stats else 0,
        "entries": [{
            "hash": row["query_hash"],
            "query": row["query_text"],
            "response": row["response"],
            "hit_count": row["hit_count"],
            "last_used": datetime.fromtimestamp(row["last_used"]).strftime("%Y-%m-%d %H:%M:%S"),
            "auto_learned": bool(row["auto_learned"])
        } for row in rows]
    }


@router.post("/cache")
async def add_cache_entry(data: CacheEntryCreate) -> Dict[str, str]:
    """Manually add a cache entry."""
    import hashlib

    query_hash = hashlib.md5(data.query_text.lower().strip().encode()).hexdigest()

    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO query_cache
        (query_hash, query_text, response, hit_count, last_used, auto_learned)
        VALUES (?, ?, ?, 0, ?, FALSE)
    """, (query_hash, data.query_text, data.response, time.time()))
    conn.commit()
    conn.close()

    return {"status": "created", "hash": query_hash}


@router.put("/cache/{query_hash}")
async def update_cache_entry(query_hash: str, data: CacheEntryUpdate) -> Dict[str, str]:
    """Update a cache entry's response."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("UPDATE query_cache SET response = ? WHERE query_hash = ?",
              (data.response, query_hash))
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected == 0:
        raise HTTPException(404, f"Cache entry '{query_hash}' not found")
    return {"status": "updated", "hash": query_hash}


@router.delete("/cache/{query_hash}")
async def delete_cache_entry(query_hash: str) -> Dict[str, str]:
    """Delete a cache entry."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM query_cache WHERE query_hash = ?", (query_hash,))
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected == 0:
        raise HTTPException(404, f"Cache entry '{query_hash}' not found")
    return {"status": "deleted", "hash": query_hash}


@router.delete("/cache")
async def clear_cache(auto_learned_only: bool = False) -> Dict[str, Any]:
    """Clear all cache entries."""
    conn = _get_conn()
    c = conn.cursor()

    if auto_learned_only:
        c.execute("DELETE FROM query_cache WHERE auto_learned = TRUE")
    else:
        c.execute("DELETE FROM query_cache")

    deleted = c.rowcount
    conn.commit()
    conn.close()

    return {"status": "cleared", "deleted_count": deleted}


# ===========================================================================
# HEALTH CHECK ENDPOINTS
# ===========================================================================

async def check_service_health(name: str, url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Check if a service is healthy."""
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            latency = (time.time() - start) * 1000
            return {
                "name": name,
                "status": "up" if response.status_code < 400 else "degraded",
                "latency_ms": round(latency, 2),
                "status_code": response.status_code
            }
    except httpx.TimeoutException:
        return {"name": name, "status": "timeout", "latency_ms": timeout * 1000}
    except Exception as e:
        return {"name": name, "status": "down", "error": str(e)}


@router.get("/health")
async def get_all_health_status() -> Dict[str, Any]:
    """Get health status of all services."""

    # Define services to check
    services = [
        ("Ollama", f"{config.OLLAMA_URL}/api/tags"),
        ("Whisper", f"{config.WHISPER_URL}/health"),
        ("Home Assistant", f"{config.HASS_URL_DEFAULT}/api/"),
        ("OpenClaw", f"{config.OPENCLAW_URL}/health"),
    ]

    # Optional services (might not be configured)
    optional_services = [
        ("Frigate", "http://frigate:5000/api/stats"),
        ("DoubleTake", "http://doubletake:3000/api/status"),
    ]

    # Run health checks in parallel
    tasks = [check_service_health(name, url) for name, url in services]
    optional_tasks = [check_service_health(name, url, timeout=2.0) for name, url in optional_services]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    optional_results = await asyncio.gather(*optional_tasks, return_exceptions=True)

    # Process results
    service_status = []
    for result in results:
        if isinstance(result, Exception):
            service_status.append({"name": "Unknown", "status": "error", "error": str(result)})
        else:
            service_status.append(result)

    for result in optional_results:
        if isinstance(result, Exception):
            continue  # Skip failed optional services
        if result.get("status") != "down":
            service_status.append(result)

    # Add orchestrator itself
    service_status.insert(0, {
        "name": "Orchestrator",
        "status": "up",
        "latency_ms": 0
    })

    # Calculate overall health
    statuses = [s["status"] for s in service_status]
    if all(s == "up" for s in statuses):
        overall = "healthy"
    elif any(s == "down" for s in statuses[:4]):  # Core services
        overall = "degraded"
    else:
        overall = "partial"

    # Cache results
    metrics.last_health_check = {
        "overall": overall,
        "services": service_status,
        "timestamp": time.time()
    }
    metrics.last_health_check_time = time.time()

    return metrics.last_health_check


# ===========================================================================
# METRICS ENDPOINTS
# ===========================================================================

@router.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    """Get live metrics and statistics."""
    stats = metrics.get_stats()

    # Add uptime info
    # We'll calculate from first request if not stored

    # Add DB stats
    conn = _get_conn()
    c = conn.cursor()

    # Chat memory count
    c.execute("SELECT COUNT(*) as count FROM chat_memory")
    chat_count = c.fetchone()["count"]

    # Audit log count
    c.execute("SELECT COUNT(*) as count FROM audit_log")
    audit_count = c.fetchone()["count"]

    # Users count
    c.execute("SELECT COUNT(*) as count FROM users")
    user_count = c.fetchone()["count"]

    # Voice enrolled count
    c.execute("SELECT COUNT(*) as count FROM users WHERE voice_enrolled = TRUE")
    voice_enrolled = c.fetchone()["count"]

    conn.close()

    return {
        "latencies": {
            "routing": stats["routing"],
            "reasoning": stats["reasoning"],
            "stt": stats["stt"],
            "speaker_id": stats["speaker_id"],
            "hass": stats["hass"]
        },
        "cache": stats["cache"],
        "requests": {
            "total": stats["total_requests"]
        },
        "database": {
            "chat_messages": chat_count,
            "audit_entries": audit_count,
            "users": user_count,
            "voice_enrolled": voice_enrolled
        },
        "timestamp": time.time()
    }


# ===========================================================================
# CONFIG ENDPOINTS (Read-only for most, editable for some)
# ===========================================================================

@router.get("/config")
async def get_config() -> Dict[str, Any]:
    """Get current configuration (mostly read-only)."""

    # Get editable preferences from DB
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT pref_key, pref_value FROM global_preferences")
    prefs = {row["pref_key"]: row["pref_value"] for row in c.fetchall()}
    conn.close()

    # Get users for family members (replaces FAMILY_MEMBERS config)
    users = get_all_users()
    family_members = [u.name for u in users]

    return {
        "editable": {
            "confidence_threshold_high": float(prefs.get("confidence_threshold_high", config.CONFIDENCE_THRESHOLD_HIGH)),
            "confidence_threshold_low": float(prefs.get("confidence_threshold_low", config.CONFIDENCE_THRESHOLD_LOW)),
            "silent_hour_start": int(prefs.get("silent_hour_start", config.SILENT_START)),
            "silent_hour_end": int(prefs.get("silent_hour_end", config.SILENT_END)),
            "dnd_mode": prefs.get("dnd_mode", "False") == "True"
        },
        "readonly": {
            "ollama_url": config.OLLAMA_URL,
            "whisper_url": config.WHISPER_URL,
            "hass_url": config.HASS_URL_DEFAULT,
            "openclaw_url": config.OPENCLAW_URL,
            "router_model": config.ROUTER_MODEL,
            "reasoning_model": "gemini (via OpenClaw)",
            "approval_timeout": config.APPROVAL_TIMEOUT,
            "memory_window_seconds": config.MEMORY_WINDOW_SECONDS,
            "router_memory": {
                "turns": config.ROUTER_MEMORY_TURNS,
                "window_seconds": config.ROUTER_MEMORY_WINDOW_SECONDS
            },
            "critical_domains": config.CRITICAL_DOMAINS
        },
        "from_database": {
            "note": "Le seguenti configurazioni sono gestite nel database (global_preferences e entity_maps)",
            "security_suppression_time": "global_preferences.security_suppression_time",
            "cameras": "entity_maps (per-location, tipo 'camera', zone 'interno'/'esterno')",
            "location_keywords": "locations.keywords (per-location)"
        },
        "derived": {
            "family_members": family_members
        }
    }


# ===========================================================================
# LOCATIONS ENDPOINTS (Multi-Home Assistant)
# ===========================================================================

@router.get("/locations")
async def list_locations(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """Lista tutte le location configurate."""
    from service_status import service_status

    locations = get_all_locations(enabled_only=enabled_only)

    result = []
    for loc in locations:
        loc_dict = loc.to_dict()
        # Maschera il token per sicurezza
        if loc_dict.get("hass_token"):
            loc_dict["hass_token"] = "***" + loc_dict["hass_token"][-4:] if len(loc_dict["hass_token"]) > 4 else "***"

        # Aggiungi stato health
        service_key = f"home_assistant_{loc.id}"
        if service_key in service_status.services:
            svc = service_status.services[service_key]
            loc_dict["health"] = {
                "state": svc.state.value,
                "latency_ms": svc.response_time_ms,
                "last_error": svc.last_error
            }
        else:
            loc_dict["health"] = {"state": "unknown"}

        result.append(loc_dict)

    return result


@router.post("/locations")
async def create_new_location(data: LocationCreate) -> Dict[str, Any]:
    """Crea una nuova location."""
    from multi_ha import multi_ha
    from service_status import service_status

    # Verifica che l'ID sia valido (slug)
    if not data.id.isalnum() or not data.id.islower():
        raise HTTPException(400, "ID deve essere lowercase alfanumerico (es: 'albani')")

    # Genera keywords di default se non fornite
    default_keywords = [data.id]
    if data.city:
        default_keywords.append(data.city.lower())

    success = create_location(
        id=data.id,
        name=data.name,
        city=data.city or "",
        hass_url=data.hass_url,
        hass_token=data.hass_token,
        entity_map_path=None,  # Le entity maps sono ora nel database
        has_security=data.has_security,
        keywords=data.keywords or default_keywords
    )

    if not success:
        raise HTTPException(409, f"Location '{data.id}' già esistente")

    # Ricarica multi_ha e service_status
    multi_ha.reload()
    service_status.reload_ha_services()

    return {"status": "created", "id": data.id}


@router.get("/locations/{location_id}")
async def get_location_detail(location_id: str) -> Dict[str, Any]:
    """Dettaglio location con status."""
    from service_status import service_status

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    loc_dict = loc.to_dict()
    # Maschera token
    if loc_dict.get("hass_token"):
        loc_dict["hass_token"] = "***" + loc_dict["hass_token"][-4:] if len(loc_dict["hass_token"]) > 4 else "***"

    # Aggiungi stato health
    service_key = f"home_assistant_{loc.id}"
    if service_key in service_status.services:
        svc = service_status.services[service_key]
        loc_dict["health"] = {
            "state": svc.state.value,
            "latency_ms": svc.response_time_ms,
            "last_check": svc.last_check,
            "last_success": svc.last_success,
            "last_error": svc.last_error,
            "consecutive_failures": svc.consecutive_failures
        }
    else:
        loc_dict["health"] = {"state": "unknown"}

    return loc_dict


@router.put("/locations/{location_id}")
async def update_location_data(location_id: str, data: LocationUpdate) -> Dict[str, Any]:
    """Aggiorna una location."""
    from multi_ha import multi_ha
    from service_status import service_status

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    # Costruisci dict dei campi da aggiornare
    update_data = {}
    if data.name is not None:
        update_data["name"] = data.name
    if data.city is not None:
        update_data["city"] = data.city
    if data.hass_url is not None:
        update_data["hass_url"] = data.hass_url
    if data.hass_token is not None:
        update_data["hass_token"] = data.hass_token
    if data.entity_map_path is not None:
        update_data["entity_map_path"] = data.entity_map_path
    if data.has_security is not None:
        update_data["has_security"] = data.has_security
    if data.enabled is not None:
        update_data["enabled"] = data.enabled

    if not update_data:
        raise HTTPException(400, "Nessun campo da aggiornare")

    success = update_location(location_id, **update_data)
    if not success:
        raise HTTPException(500, "Aggiornamento fallito")

    # Ricarica multi_ha e service_status
    multi_ha.reload()
    service_status.reload_ha_services()

    return {"status": "updated", "id": location_id}


@router.delete("/locations/{location_id}")
async def disable_location(location_id: str) -> Dict[str, Any]:
    """Disabilita una location (soft delete)."""
    from multi_ha import multi_ha
    from service_status import service_status

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    success = delete_location(location_id)
    if not success:
        raise HTTPException(500, "Disabilitazione fallita")

    # Ricarica multi_ha e service_status
    multi_ha.reload()
    service_status.reload_ha_services()

    return {"status": "disabled", "id": location_id}


@router.post("/locations/{location_id}/test")
async def test_location_connection(location_id: str) -> Dict[str, Any]:
    """Testa connessione a HA per questa location."""
    from multi_ha import multi_ha

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    if not loc.hass_token:
        return {
            "status": "error",
            "message": "Token non configurato",
            "healthy": False,
            "latency_ms": 0
        }

    # Esegui health check
    multi_ha.ensure_loaded()
    client = multi_ha.get_client(location_id)
    if not client:
        return {
            "status": "error",
            "message": "Client non trovato",
            "healthy": False,
            "latency_ms": 0
        }

    healthy, latency = await client.health_check()

    return {
        "status": "ok" if healthy else "error",
        "message": "Connessione riuscita" if healthy else "Connessione fallita",
        "healthy": healthy,
        "latency_ms": round(latency, 1)
    }


@router.post("/locations/reload")
async def reload_locations() -> Dict[str, str]:
    """Ricarica configurazione location (dopo modifiche)."""
    from multi_ha import multi_ha
    from service_status import service_status

    multi_ha.reload()
    service_status.reload_ha_services()

    return {"status": "reloaded"}


# ===========================================================================
# ENTITY MAPS ENDPOINTS
# ===========================================================================

class EntityMapImport(BaseModel):
    """Schema per import entity map da JSON."""
    entity_map: Dict[str, Any]


@router.get("/locations/{location_id}/entity-map")
async def get_location_entity_map(location_id: str) -> Dict[str, Any]:
    """Recupera l'entity map di una location."""
    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    entity_map = get_entity_map(location_id)
    stats = get_entity_map_stats(location_id)
    rooms = get_rooms_for_location(location_id)

    return {
        "location_id": location_id,
        "entity_map": entity_map,
        "stats": stats,
        "rooms": rooms
    }


@router.post("/locations/{location_id}/entity-map/import")
async def import_location_entity_map(location_id: str, data: EntityMapImport) -> Dict[str, Any]:
    """Importa entity map da JSON nel database.

    Il JSON deve avere la struttura gerarchica:
    Zone → Area → Room → EntityType → [Entities]

    Esempio:
    {
      "Interno": {
        "Zona_Giorno": {
          "Salotto": {
            "light": ["Luci Salotto", "Lampada"],
            "media_player": ["TV Salotto"]
          }
        }
      },
      "Impianti": {
        "climate": ["Clima Principale"],
        "switch": ["Boiler"]
      }
    }
    """
    from multi_ha import multi_ha

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    if not data.entity_map:
        raise HTTPException(400, "Entity map vuota")

    try:
        count = import_entity_map_json(location_id, data.entity_map)
    except Exception as e:
        raise HTTPException(500, f"Errore durante l'import: {str(e)}")

    # Aggiorna il campo entity_map_path per indicare che è importato
    update_location(location_id, entity_map_path="[database]")

    # Ricarica multi_ha per aggiornare le entity maps in memoria
    multi_ha.reload()

    stats = get_entity_map_stats(location_id)

    return {
        "status": "imported",
        "location_id": location_id,
        "entities_imported": count,
        "stats": stats
    }


@router.get("/locations/{location_id}/entity-map/export")
async def export_location_entity_map(location_id: str) -> Dict[str, Any]:
    """Esporta entity map di una location in formato JSON.

    Utile per backup o per duplicare configurazione su altra location.
    """
    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    entity_map = export_entity_map_json(location_id)

    if not entity_map:
        return {
            "location_id": location_id,
            "entity_map": {},
            "message": "Nessuna entity configurata per questa location"
        }

    return {
        "location_id": location_id,
        "entity_map": entity_map
    }


class HierarchyImport(BaseModel):
    """Schema per import gerarchia personalizzata."""
    hierarchy: Dict[str, Any]


@router.post("/locations/{location_id}/hierarchy/import")
async def import_location_hierarchy(location_id: str, data: HierarchyImport) -> Dict[str, Any]:
    """
    Importa gerarchia personalizzata (Piani -> Zone -> Aree HA).

    Crea solo la struttura — le entity vengono poi popolate dal sync HA.

    Formato:
    {
      "floors": [
        {"floor_name": "Piano 1", "zones": [
          {"zone_name": "Zona Giorno", "ha_areas": ["cucina", "soggiorno"]}
        ]}
      ]
    }
    """
    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    if not data.hierarchy or "floors" not in data.hierarchy:
        raise HTTPException(400, "Formato gerarchia non valido (manca 'floors')")

    try:
        result = import_hierarchy_json(location_id, data.hierarchy)
    except Exception as e:
        raise HTTPException(500, f"Errore durante l'import: {str(e)}")

    return {
        "status": "imported",
        "location_id": location_id,
        **result
    }


@router.get("/locations/{location_id}/hierarchy")
async def get_location_hierarchy(location_id: str) -> Dict[str, Any]:
    """Ritorna la gerarchia importata per una location (se presente)."""
    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    mapping = get_hierarchy_mapping(location_id)

    return {
        "location_id": location_id,
        "has_hierarchy": bool(mapping),
        "ha_area_mapping": mapping
    }


@router.delete("/locations/{location_id}/entity-map")
async def clear_location_entity_map(location_id: str) -> Dict[str, Any]:
    """Cancella tutte le entity di una location."""
    from multi_ha import multi_ha

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    deleted = clear_entity_map(location_id)

    # Aggiorna il campo entity_map_path
    update_location(location_id, entity_map_path=None)

    # Ricarica multi_ha
    multi_ha.reload()

    return {
        "status": "cleared",
        "location_id": location_id,
        "entities_deleted": deleted
    }


@router.get("/locations/{location_id}/entity-map/stats")
async def get_location_entity_map_stats(location_id: str) -> Dict[str, Any]:
    """Statistiche sull'entity map di una location."""
    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    stats = get_entity_map_stats(location_id)
    rooms = get_rooms_for_location(location_id)

    return {
        "location_id": location_id,
        "stats": stats,
        "rooms": rooms
    }


# ===========================================================================
# ENTITY ID RESOLUTION ENDPOINTS
# ===========================================================================

@router.get("/locations/{location_id}/entities/resolve")
async def resolve_entity(
    location_id: str,
    name: str,
    entity_type: Optional[str] = None,
    room: Optional[str] = None
) -> Dict[str, Any]:
    """
    Risolve un friendly name in entity_id.

    Query params:
        name: Friendly name del dispositivo (es: "Echo Camera")
        entity_type: Tipo opzionale (media_player, light, etc.)
        room: Stanza opzionale per disambiguare
    """
    from database import resolve_entity_id, resolve_entity_id_fuzzy

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    # Prova match esatto
    entity_id = resolve_entity_id(location_id, name, entity_type, room)

    if entity_id:
        return {
            "found": True,
            "friendly_name": name,
            "entity_id": entity_id,
            "match_type": "exact"
        }

    # Prova match fuzzy
    fuzzy_matches = resolve_entity_id_fuzzy(location_id, name, entity_type)
    if fuzzy_matches:
        return {
            "found": False,
            "friendly_name": name,
            "entity_id": None,
            "match_type": "fuzzy",
            "suggestions": fuzzy_matches
        }

    return {
        "found": False,
        "friendly_name": name,
        "entity_id": None,
        "match_type": "none",
        "suggestions": []
    }


@router.get("/locations/{location_id}/entities/missing-ids")
async def get_entities_missing_ids(location_id: str) -> Dict[str, Any]:
    """
    Lista tutte le entità senza entity_id configurato.
    Utile per identificare mapping incompleti da completare.
    """
    from database import get_entities_without_id

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    missing = get_entities_without_id(location_id)

    return {
        "location_id": location_id,
        "count": len(missing),
        "entities": missing
    }


class UpdateEntityIdRequest(BaseModel):
    """Request per aggiornare entity_id."""
    entity_name: str
    entity_id: str
    entity_type: Optional[str] = None
    room: Optional[str] = None


@router.post("/locations/{location_id}/entities/update-id")
async def update_entity_entity_id(
    location_id: str,
    request: UpdateEntityIdRequest
) -> Dict[str, Any]:
    """
    Aggiorna l'entity_id per un'entità esistente.

    Body:
        entity_name: Friendly name dell'entità
        entity_id: Nuovo entity_id di Home Assistant
        entity_type: Tipo opzionale per disambiguare
        room: Stanza opzionale per disambiguare
    """
    from database import update_entity_id

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    updated = update_entity_id(
        location_id=location_id,
        entity_name=request.entity_name,
        entity_id=request.entity_id,
        entity_type=request.entity_type,
        room=request.room
    )

    if updated:
        return {
            "success": True,
            "message": f"Entity '{request.entity_name}' aggiornata con ID '{request.entity_id}'"
        }
    else:
        raise HTTPException(404, f"Entity '{request.entity_name}' non trovata")


class BulkUpdateEntityIdsRequest(BaseModel):
    """Request per aggiornamento bulk entity_id."""
    mappings: List[Dict[str, str]]  # [{"name": "Echo Camera", "entity_id": "media_player.xxx"}, ...]


@router.post("/locations/{location_id}/entities/bulk-update-ids")
async def bulk_update_entity_ids(
    location_id: str,
    request: BulkUpdateEntityIdsRequest
) -> Dict[str, Any]:
    """
    Aggiorna entity_id per multiple entità in una singola richiesta.

    Body:
        mappings: Lista di {"name": "...", "entity_id": "...", "entity_type": "...", "room": "..."}
    """
    from database import update_entity_id

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    updated = 0
    failed = []

    for mapping in request.mappings:
        name = mapping.get("name") or mapping.get("entity_name")
        entity_id = mapping.get("entity_id") or mapping.get("id")

        if not name or not entity_id:
            failed.append({"mapping": mapping, "error": "Missing name or entity_id"})
            continue

        success = update_entity_id(
            location_id=location_id,
            entity_name=name,
            entity_id=entity_id,
            entity_type=mapping.get("entity_type"),
            room=mapping.get("room")
        )

        if success:
            updated += 1
        else:
            failed.append({"mapping": mapping, "error": "Entity not found"})

    return {
        "success": len(failed) == 0,
        "updated": updated,
        "failed_count": len(failed),
        "failed": failed if failed else None
    }


@router.get("/locations/{location_id}/entities/by-id/{entity_id:path}")
async def get_entity_by_entity_id(location_id: str, entity_id: str) -> Dict[str, Any]:
    """
    Reverse lookup: trova friendly name e posizione da entity_id.
    """
    from database import get_entity_by_id

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    entity = get_entity_by_id(location_id, entity_id)

    if entity:
        return {"found": True, "entity": entity}
    else:
        return {"found": False, "entity_id": entity_id}


# ===========================================================================
# ENTITY TREE & VISIBILITY
# ===========================================================================

class EntityVisibilityRequest(BaseModel):
    """Request per aggiornare visibility di entity."""
    entity_ids: List[int]
    visible: bool


@router.get("/locations/{location_id}/entity-tree")
async def get_entity_tree_endpoint(location_id: str) -> Dict[str, Any]:
    """
    Ritorna lista flat di tutte le entity per costruire il tree view.
    Ogni entity include id, zone, area, room, device_name, entity_type,
    entity_name, entity_id, visible.
    """
    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    entities = get_entity_tree(location_id)
    stats = get_entity_map_stats(location_id)

    return {
        "location_id": location_id,
        "entities": entities,
        "stats": stats
    }


@router.patch("/locations/{location_id}/entity-visibility")
async def patch_entity_visibility(
    location_id: str,
    req: EntityVisibilityRequest
) -> Dict[str, Any]:
    """
    Aggiorna la visibility di un set di entity.
    Body: {entity_ids: [1,2,3], visible: true/false}
    """
    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    updated = update_entity_visibility(req.entity_ids, req.visible)

    return {
        "updated": updated,
        "visible": req.visible,
        "stats": get_entity_map_stats(location_id)
    }


# ===========================================================================
# HOME ASSISTANT ENTITY SYNC
# ===========================================================================

class HASyncRequest(BaseModel):
    """Request per sync entity da HA."""
    entity_types: Optional[List[str]] = None  # None = tutti i tipi supportati
    overwrite_existing: bool = True  # Sovrascrivi sempre per default


@router.get("/locations/{location_id}/ha-sync/preview")
async def preview_ha_sync(location_id: str) -> Dict[str, Any]:
    """
    Preview delle entity che verrebbero importate da Home Assistant (dry-run).

    Ritorna statistiche e lista completa senza modificare il database.
    """
    from ha_sync import preview_ha_sync as _preview

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    if not loc.hass_url or not loc.hass_token:
        raise HTTPException(400, f"Location '{location_id}' non ha credenziali HA configurate")

    result = await _preview(loc.hass_url, loc.hass_token)

    if "error" in result:
        raise HTTPException(502, result["error"])

    return {
        "location_id": location_id,
        "preview": result
    }


@router.post("/locations/{location_id}/ha-sync")
async def sync_entities_from_ha(
    location_id: str,
    request: HASyncRequest = None
) -> Dict[str, Any]:
    """
    Sincronizza entity da Home Assistant al database JARVIS.

    - Cancella entity esistenti per un fresh start
    - Fetch tutte le entity da HA via WebSocket + REST API
    - Parse friendly_name, entity_id, area, device
    - Import nel database con mapping automatico

    Body (opzionale):
        entity_types: Lista domini da sincronizzare (default: tutti)
        overwrite_existing: Se True (default), cancella e reimporta tutto
    """
    from ha_sync import sync_entities_from_ha as _sync

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    if not loc.hass_url or not loc.hass_token:
        raise HTTPException(400, f"Location '{location_id}' non ha credenziali HA configurate")

    req = request or HASyncRequest()

    # Snapshot visibility prima del clear (per preservare selezioni utente)
    visibility_snapshot = None
    if req.overwrite_existing:
        visibility_snapshot = save_visibility_snapshot(location_id)
        deleted = clear_entity_map(location_id)
        import logging
        logging.getLogger("JARVIS_HA_SYNC").info(
            f"Cleared {deleted} existing entities for fresh sync "
            f"(visibility snapshot: {len(visibility_snapshot)} entries)"
        )

    added, updated, errors = await _sync(
        location_id=location_id,
        hass_url=loc.hass_url,
        hass_token=loc.hass_token,
        entity_types=req.entity_types,
        overwrite_existing=req.overwrite_existing
    )

    # Ripristina visibility dallo snapshot
    restored = 0
    if visibility_snapshot:
        restored = restore_visibility_snapshot(location_id, visibility_snapshot)
        import logging
        logging.getLogger("JARVIS_HA_SYNC").info(
            f"Restored visibility for {restored} entities after re-sync"
        )

    # Aggiorna entity_map_path per indicare che è importata da HA
    update_location(location_id, entity_map_path="[database]")

    return {
        "success": len(errors) == 0,
        "location_id": location_id,
        "added": added,
        "updated": updated,
        "visibility_restored": restored,
        "errors": errors if errors else None
    }


@router.get("/locations/{location_id}/ha-sync/media-players")
async def get_ha_media_players_for_location(location_id: str) -> Dict[str, Any]:
    """
    Lista media_player disponibili su Home Assistant.
    Utile per configurare output speaker dei voice devices.
    """
    from ha_sync import get_ha_media_players

    loc = get_location(location_id)
    if not loc:
        raise HTTPException(404, f"Location '{location_id}' non trovata")

    if not loc.hass_url or not loc.hass_token:
        raise HTTPException(400, f"Location '{location_id}' non ha credenziali HA configurate")

    media_players = await get_ha_media_players(loc.hass_url, loc.hass_token)

    return {
        "location_id": location_id,
        "count": len(media_players),
        "media_players": media_players
    }


# ===========================================================================
# OPENCLAW STATUS ENDPOINT
# ===========================================================================

@router.get("/openclaw/status")
async def get_openclaw_status() -> Dict[str, Any]:
    """Get OpenClaw service health status."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            start = time.time()
            response = await client.get(f"{config.OPENCLAW_URL}/health")
            latency = (time.time() - start) * 1000

            if response.status_code < 400:
                try:
                    data = response.json()
                except Exception:
                    data = {}
                return {
                    "status": "up",
                    "url": config.OPENCLAW_URL,
                    "latency_ms": round(latency, 2),
                    "details": data
                }
            else:
                return {
                    "status": "degraded",
                    "url": config.OPENCLAW_URL,
                    "status_code": response.status_code,
                    "latency_ms": round(latency, 2)
                }
    except httpx.TimeoutException:
        return {
            "status": "timeout",
            "url": config.OPENCLAW_URL,
            "error": "Health check timed out after 5s"
        }
    except Exception as e:
        return {
            "status": "down",
            "url": config.OPENCLAW_URL,
            "error": str(e)
        }


# ===========================================================================
# VOICE DEVICES MANAGEMENT
# ===========================================================================

@router.get("/voice-devices")
async def list_voice_devices(
    configured_only: bool = False,
    unconfigured_only: bool = False,
    location_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Lista tutti i voice devices.

    Filtri opzionali:
    - configured_only: solo device con friendly_name configurato
    - unconfigured_only: solo device in attesa di configurazione
    - location_id: solo device di una specifica location
    """
    if unconfigured_only:
        devices = get_unconfigured_voice_devices()
    elif configured_only:
        devices = get_configured_voice_devices()
    elif location_id:
        devices = get_voice_devices_by_location(location_id)
    else:
        devices = get_all_voice_devices()

    stats = get_voice_device_stats()

    # Enrich with real-time WebSocket connection status
    try:
        from ws_audio_handler import get_connected_devices
        ws_connected = set(await get_connected_devices())
    except Exception:
        ws_connected = set()

    device_dicts = []
    for d in devices:
        dd = d.to_dict()
        # Device is online if heartbeat is recent OR WebSocket is connected
        if dd["device_id"] in ws_connected:
            dd["is_online"] = True
            dd["ws_connected"] = True
        else:
            dd["ws_connected"] = False
        device_dicts.append(dd)

    return {
        "devices": device_dicts,
        "stats": stats
    }


@router.get("/voice-devices/stats")
async def get_voice_devices_stats() -> Dict[str, Any]:
    """Statistiche sui voice devices."""
    return get_voice_device_stats()


@router.get("/voice-devices/{device_id}")
async def get_voice_device_detail(device_id: str) -> Dict[str, Any]:
    """Dettaglio di un voice device."""
    device = get_voice_device(device_id)
    if not device:
        raise HTTPException(404, f"Device {device_id} non trovato")

    return device.to_dict()


# ---------------------------------------------------------------------------
# Wakeword-server LXC helpers
# ---------------------------------------------------------------------------

def _get_wakeword_server_url(device_id: str) -> Optional[str]:
    """Get wakeword-server URL for a device based on its location."""
    from config import WAKEWORD_SERVER_URLS, DEVICE_API_TOKEN
    if not WAKEWORD_SERVER_URLS:
        return None
    try:
        device = get_voice_device(device_id)
        if device and device.location_id:
            return WAKEWORD_SERVER_URLS.get(device.location_id)
    except Exception:
        pass
    # Fallback: if only one URL configured, use it
    if len(WAKEWORD_SERVER_URLS) == 1:
        return next(iter(WAKEWORD_SERVER_URLS.values()))
    return None


async def _push_wakeword_config(device_id: str, sensitivity: float):
    """Push wake_word_threshold to wakeword-server LXC via REST."""
    wakeword_url = _get_wakeword_server_url(device_id)
    if not wakeword_url:
        return
    try:
        import httpx
        from config import DEVICE_API_TOKEN
        headers = {}
        if DEVICE_API_TOKEN:
            headers["Authorization"] = f"Bearer {DEVICE_API_TOKEN}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{wakeword_url}/api/config/{device_id}",
                json={"wake_word_threshold": sensitivity},
                headers=headers,
            )
    except Exception as e:
        logger.debug(f"Push wakeword config to LXC failed for {device_id}: {e}")


@router.post("/voice-devices")
async def create_or_update_voice_device(data: VoiceDeviceCreate) -> Dict[str, Any]:
    """
    Crea o aggiorna un voice device.

    Il device_id deve essere il MAC address in formato AABBCCDDEEFF.
    """
    # Valida formato device_id
    device_id = data.device_id.upper().strip()
    if len(device_id) != 12 or not all(c in "0123456789ABCDEF" for c in device_id):
        raise HTTPException(400, "device_id deve essere un MAC address in formato AABBCCDDEEFF (12 caratteri hex)")

    # Verifica che la location esista
    location = get_location(data.location_id)
    if not location:
        raise HTTPException(404, f"Location {data.location_id} non trovata")

    # Upsert device
    upsert_voice_device(
        device_id=device_id,
        friendly_name=data.friendly_name,
        location_id=data.location_id,
        output_speaker=data.output_speaker,
        fallback_speaker=data.fallback_speaker,
        fallback_telegram=data.fallback_telegram,
        fallback_local_speaker=data.fallback_local_speaker,
        use_internal_speaker=data.use_internal_speaker,
        enabled=data.enabled,
        notes=data.notes,
        wake_word_sensitivity=data.wake_word_sensitivity,
        speaker_volume=data.speaker_volume,
        volume_speaker=data.volume_speaker
    )

    # Push config update to device via WebSocket
    config_push = {}
    if data.wake_word_sensitivity is not None:
        config_push["wake_word_sensitivity"] = data.wake_word_sensitivity
    if data.speaker_volume is not None:
        config_push["speaker_volume"] = data.speaker_volume
    config_push["speaker_type"] = "internal" if data.use_internal_speaker else "alexa"
    if config_push:
        try:
            from ws_audio_handler import push_config_to_device
            await push_config_to_device(device_id, config_push)
        except Exception:
            pass  # Device may not be connected

    if data.wake_word_sensitivity is not None:
        # Also push to wakeword-server LXC if configured
        await _push_wakeword_config(device_id, data.wake_word_sensitivity)

    device = get_voice_device(device_id)

    return {
        "status": "saved",
        "device": device.to_dict() if device else None
    }


@router.patch("/voice-devices/{device_id}")
async def update_voice_device_endpoint(
    device_id: str,
    data: VoiceDeviceUpdate
) -> Dict[str, Any]:
    """Aggiorna un voice device esistente."""
    device_id = device_id.upper().strip()

    existing = get_voice_device(device_id)
    if not existing:
        raise HTTPException(404, f"Device {device_id} non trovato")

    # Se cambia location, verifica che esista
    if data.location_id:
        location = get_location(data.location_id)
        if not location:
            raise HTTPException(404, f"Location {data.location_id} non trovata")

    # Costruisci i parametri di update
    update_params = {}
    if data.friendly_name is not None:
        update_params['friendly_name'] = data.friendly_name
    if data.location_id is not None:
        update_params['location_id'] = data.location_id
    if data.output_speaker is not None:
        update_params['output_speaker'] = data.output_speaker
    if data.fallback_speaker is not None:
        update_params['fallback_speaker'] = data.fallback_speaker
    if data.fallback_telegram is not None:
        update_params['fallback_telegram'] = data.fallback_telegram
    if data.fallback_local_speaker is not None:
        update_params['fallback_local_speaker'] = data.fallback_local_speaker
    if data.use_internal_speaker is not None:
        update_params['use_internal_speaker'] = data.use_internal_speaker
    if data.enabled is not None:
        update_params['enabled'] = data.enabled
    if data.notes is not None:
        update_params['notes'] = data.notes
    if data.wake_word_sensitivity is not None:
        update_params['wake_word_sensitivity'] = data.wake_word_sensitivity
    if data.speaker_volume is not None:
        update_params['speaker_volume'] = data.speaker_volume
    if data.volume_speaker is not None:
        update_params['volume_speaker'] = data.volume_speaker

    if update_params:
        upsert_voice_device(device_id=device_id, **update_params)

    # Push config update to device via WebSocket
    config_push = {}
    if data.wake_word_sensitivity is not None:
        config_push["wake_word_sensitivity"] = data.wake_word_sensitivity
    if data.speaker_volume is not None:
        config_push["speaker_volume"] = data.speaker_volume
    if data.use_internal_speaker is not None:
        config_push["speaker_type"] = "internal" if data.use_internal_speaker else "alexa"
    if config_push:
        try:
            from ws_audio_handler import push_config_to_device
            await push_config_to_device(device_id, config_push)
        except Exception:
            pass  # Device may not be connected

    if data.wake_word_sensitivity is not None:
        # Also push to wakeword-server LXC if configured
        await _push_wakeword_config(device_id, data.wake_word_sensitivity)

    device = get_voice_device(device_id)

    return {
        "status": "updated",
        "device": device.to_dict() if device else None
    }


@router.delete("/voice-devices/{device_id}")
async def delete_voice_device_endpoint(device_id: str) -> Dict[str, Any]:
    """Elimina un voice device."""
    device_id = device_id.upper().strip()

    existing = get_voice_device(device_id)
    if not existing:
        raise HTTPException(404, f"Device {device_id} non trovato")

    success = delete_voice_device(device_id)

    return {
        "status": "deleted" if success else "error",
        "device_id": device_id
    }


@router.get("/ha-media-players")
async def get_ha_media_players(location_id: str) -> Dict[str, Any]:
    """
    Lista i media_player disponibili per una location.

    Usato dalla dashboard per popolare il dropdown degli speaker.
    """
    from integrations import get_hass_states_bulk

    location = get_location(location_id)
    if not location:
        raise HTTPException(404, f"Location {location_id} non trovata")

    try:
        # Recupera tutti gli stati da Home Assistant
        states = await get_hass_states_bulk(location_id)

        # Filtra solo i media_player
        media_players = []
        for entity_id, entity_data in states.items():
            if entity_id.startswith("media_player."):
                friendly_name = entity_data.get("attributes", {}).get("friendly_name", entity_id)
                state = entity_data.get("state", "unknown")

                media_players.append({
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "state": state
                })

        # Ordina per friendly_name
        media_players.sort(key=lambda x: x["friendly_name"].lower())

        return {
            "location_id": location_id,
            "media_players": media_players,
            "count": len(media_players)
        }

    except Exception as e:
        return {
            "location_id": location_id,
            "media_players": [],
            "count": 0,
            "error": str(e)
        }


# ===========================================================================
# MEMORY STATS
# ===========================================================================

@router.get("/memory-stats")
async def get_memory_stats() -> Dict[str, Any]:
    """
    Statistiche complete del sistema di memoria.
    Aggrega dati da ChromaDB, SQLite e HA Memory Services.
    """
    import aiohttp
    from location_memory import MEMORY_SERVICE_URLS

    conn = _get_conn()
    c = conn.cursor()

    # === CHROMADB STATS ===
    chromadb_stats = {"status": "unavailable", "user_messages_count": 0, "user_facts_count": 0, "total_vectors": 0}
    try:
        from vector_store import user_vector_store, COLLECTION_USER_MESSAGES, COLLECTION_USER_FACTS
        if user_vector_store._initialized:
            msg_col = user_vector_store.collections.get(COLLECTION_USER_MESSAGES)
            fact_col = user_vector_store.collections.get(COLLECTION_USER_FACTS)
            msg_count = msg_col.count() if msg_col else 0
            fact_count = fact_col.count() if fact_col else 0
            chromadb_stats = {
                "status": "online",
                "user_messages_count": msg_count,
                "user_facts_count": fact_count,
                "total_vectors": msg_count + fact_count
            }
    except Exception as e:
        chromadb_stats = {"status": "error", "error": str(e), "user_messages_count": 0, "user_facts_count": 0, "total_vectors": 0}

    # === SQL MEMORY STATS ===
    c.execute("SELECT COUNT(*) as cnt FROM user_memory_hourly")
    hourly_count = c.fetchone()["cnt"]

    c.execute("SELECT COUNT(*) as cnt FROM user_memory_daily")
    daily_count = c.fetchone()["cnt"]

    c.execute("SELECT COUNT(*) as cnt FROM user_memory_longterm")
    longterm_count = c.fetchone()["cnt"]

    c.execute("SELECT COUNT(*) as cnt FROM chat_memory")
    chat_total = c.fetchone()["cnt"]

    # === PER-USER MEMORY BREAKDOWN ===
    c.execute("""
        SELECT u.id, u.name,
            (SELECT COUNT(*) FROM user_memory_hourly WHERE user_id = u.id) as hourly_count,
            (SELECT COUNT(*) FROM user_memory_daily WHERE user_id = u.id) as daily_count,
            (SELECT COUNT(*) FROM user_memory_longterm WHERE user_id = u.id) as longterm_count,
            (SELECT COUNT(*) FROM chat_memory WHERE speaker_id = u.id) as chat_count
        FROM users u
        ORDER BY u.name
    """)
    per_user_memory = [dict(row) for row in c.fetchall()]

    conn.close()

    # === HA MEMORY SERVICE STATS (async) ===
    ha_memory_stats = {}
    if MEMORY_SERVICE_URLS:
        async with aiohttp.ClientSession() as session:
            for loc_id, url in MEMORY_SERVICE_URLS.items():
                try:
                    async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        if resp.status == 200:
                            ha_memory_stats[loc_id] = {"status": "online"}
                            # Prova a ottenere conteggi memoria
                            try:
                                async with session.get(
                                    f"{url}/memory",
                                    params={"hot_minutes": 0, "warm_hours": 99999, "cold_days": 99999},
                                    timeout=aiohttp.ClientTimeout(total=5)
                                ) as mem_resp:
                                    if mem_resp.status == 200:
                                        mem_data = await mem_resp.json()
                                        ha_memory_stats[loc_id].update({
                                            "warm_summaries": len(mem_data.get("warm", [])),
                                            "cold_summaries": len(mem_data.get("cold", [])),
                                            "longterm_facts": len(mem_data.get("longterm", [])),
                                            "hot_events": len(mem_data.get("hot", [])),
                                            "token_estimate": mem_data.get("token_estimate", 0)
                                        })
                            except Exception:
                                ha_memory_stats[loc_id]["details"] = "conteggi non disponibili"
                        else:
                            ha_memory_stats[loc_id] = {"status": "offline"}
                except Exception:
                    ha_memory_stats[loc_id] = {"status": "unreachable"}

    # === RETENTION INFO ===
    retention = {
        "chat_memory_max_age_hours": config.CHAT_MEMORY_MAX_AGE / 3600,
        "hourly_max_age_hours": config.COMPACTOR_HOURLY_MAX_AGE_HOURS,
        "daily_max_age_days": config.COMPACTOR_DAILY_MAX_AGE_DAYS,
        "vector_retention_days": config.VECTOR_RETENTION_DAYS,
        "scheduler_hourly_minute": config.MEMORY_HOURLY_TRIGGER_MINUTE,
        "scheduler_daily_hour": config.MEMORY_DAILY_TRIGGER_HOUR
    }

    return {
        "chromadb": chromadb_stats,
        "sql_memory": {
            "hourly_summaries": hourly_count,
            "daily_summaries": daily_count,
            "longterm_facts": longterm_count,
            "chat_messages_total": chat_total
        },
        "per_user_memory": per_user_memory,
        "ha_memory_services": ha_memory_stats,
        "retention": retention,
        "timestamp": time.time()
    }


# ===========================================================================
# ACCESS LOG & AUTH ATTEMPTS (Security Monitoring)
# ===========================================================================

@router.get("/access-log")
async def get_access_log_endpoint(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    ip: Optional[str] = None,
    status_code: Optional[int] = None,
    path_pattern: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    user_id: Optional[int] = None,
    method: Optional[str] = None
) -> Dict[str, Any]:
    """Access log con filtri avanzati e paginazione."""
    return get_access_log(
        limit=limit, offset=offset,
        ip=ip, status_code=status_code,
        path_pattern=path_pattern,
        start_time=start_time, end_time=end_time,
        user_id=user_id, method=method
    )


@router.get("/access-log/stats")
async def get_access_log_stats_endpoint() -> Dict[str, Any]:
    """Statistiche aggregate: top IP, top paths, distribuzione status, errori."""
    return get_access_log_stats()


@router.get("/auth-attempts")
async def get_auth_attempts_endpoint(
    email: Optional[str] = None,
    ip: Optional[str] = None,
    success: Optional[bool] = None,
    limit: int = Query(50, ge=1, le=500)
) -> Dict[str, Any]:
    """Lista tentativi di autenticazione con filtri."""
    items = get_auth_attempts_log(limit=limit, email=email, ip=ip, success_only=success)
    return {"items": items, "count": len(items)}


# ===========================================================================
# BACKUP & RESTORE
# ===========================================================================

@router.get("/backup/info")
async def get_backup_info_endpoint() -> Dict[str, Any]:
    """Info sui componenti che verrebbero inclusi in un backup."""
    return get_backup_info()


@router.get("/backup")
async def download_backup():
    """
    Crea e scarica un backup completo del sistema (DB + ChromaDB + Voice Models).
    Ritorna un file .tar.gz.
    """
    import logging
    logger = logging.getLogger("JARVIS_BACKUP")

    try:
        result = create_backup()
        if not result["success"]:
            raise HTTPException(status_code=500, detail=f"Backup fallito: {result.get('error', 'unknown')}")

        backup_path = result["path"]
        filename = Path(backup_path).name

        return FileResponse(
            path=backup_path,
            media_type="application/gzip",
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Backup-Size": str(result["size_bytes"]),
                "X-Backup-Components": json.dumps(list(result["components"].keys()))
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Errore endpoint backup: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/backup/validate")
async def validate_backup_endpoint(file: UploadFile = File(...)) -> Dict[str, Any]:
    """
    Valida un archivio di backup senza ripristinarlo.
    Verifica: formato, metadata, database, componenti.
    """
    import tempfile
    tmp_path = None
    try:
        # Salva file uploadato su disco temporaneo
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        result = validate_backup(tmp_path)
        result["filename"] = file.filename
        result["uploaded_size_bytes"] = len(content)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore validazione: {e}")
    finally:
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()


@router.post("/restore")
async def restore_backup_endpoint(file: UploadFile = File(...)) -> Dict[str, Any]:
    """
    Ripristina un backup completo del sistema.
    Crea automaticamente un safety backup prima del ripristino.
    """
    import tempfile
    import logging
    logger = logging.getLogger("JARVIS_BACKUP")

    tmp_path = None
    try:
        # Salva file uploadato
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        logger.info(f"Restore avviato da upload: {file.filename} ({len(content)} bytes)")

        result = restore_backup(
            archive_path=tmp_path,
            stop_callback=None,   # TODO: collegare a stop dei task periodici
            restart_callback=None  # TODO: collegare a restart servizi
        )

        if not result["success"]:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Restore fallito",
                    "errors": result["errors"],
                    "steps_completed": result["steps_completed"],
                    "safety_backup_path": result.get("safety_backup_path")
                }
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Errore endpoint restore: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()
