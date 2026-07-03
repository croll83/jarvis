"""
Semantic entity discovery for the router / AI-agent.

Replaces literal SQL LIKE matching with embedding-based top-k retrieval over the
entity map, so queries match across language (temperatura/temperature), synonyms
(pompa -> MAXA) and non-mnemonic names. Uses the local fastembed server
(nomic-embed-text-v1.5, 768-dim) already deployed for the stack.

No entity renames, no re-sync: the index is built from existing entity_maps rows
(+ device_class read live from HA), so manual visibility selections are untouched.
"""

import os
import time
import json
import pickle
import hashlib
import asyncio
import logging
import urllib.request
from typing import Optional, List, Dict, Set

import numpy as np

import config
from database import _get_conn
from multi_ha import multi_ha

logger = logging.getLogger("JARVIS_SEMANTIC")

_EMBED_MODEL = "nomic-embed-text"
_EMBED_URL = (config.EMBEDDING_URL or "http://localhost:11435").rstrip("/")
_INDEX_TTL = 3600  # s — index considered fresh for 1h (periodic loop refreshes hourly)
_DIM = 768

# Persisted vector cache dir (mounted volume → survives restarts, avoids re-embed)
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
try:
    os.makedirs(_CACHE_DIR, exist_ok=True)
except Exception:
    _CACHE_DIR = "/tmp"

# location_id -> {"vecs": np.ndarray (N, D), "items": List[dict], "built_at": float}
_INDEX: Dict[str, dict] = {}
# location_id -> {entity_id: (doc_hash, np.ndarray(D,))}  — per-entity vector cache
_VEC_CACHE: Dict[str, Dict[str, tuple]] = {}
# locations with a background (re)build in flight (dedupe)
_REFRESHING: Set[str] = set()

# Italian attribute word-stems -> HA device_class, used to disambiguate sensor
# queries (e.g. "temperatura del salotto" -> only device_class=temperature).
# Stems (substring match) so "consuma/consumando/consumi", "umidità/umido", etc.
# all resolve. Ordered: more specific stems first.
_DEVICE_CLASS_HINTS = [
    ("temperat", "temperature"), ("gradi", "temperature"),
    ("umid", "humidity"),
    ("energ", "energy"), ("kwh", "energy"),
    ("potenz", "power"), ("watt", "power"), ("consum", "power"), ("assorb", "power"),
    ("co2", "carbon_dioxide"),
    ("luminos", "illuminance"), ("lux", "illuminance"),
    ("pression", "pressure"),
    ("batteri", "battery"), ("caric", "battery"),
    ("moviment", "motion"), ("presenza", "occupancy"),
    ("porta", "door"), ("finestra", "window"), ("apertura", "opening"),
]


def device_class_hint(text: str) -> Optional[str]:
    """Infer a device_class from common Italian attribute word-stems in the query."""
    t = (text or "").lower()
    for stem, dc in _DEVICE_CLASS_HINTS:
        if stem in t:
            return dc
    return None


def _embed(texts: List[str], is_query: bool) -> Optional[np.ndarray]:
    """Embed texts via fastembed; returns L2-normalized (N, D) array or None on error."""
    if not texts:
        return np.zeros((0, _DIM), dtype=np.float32)
    # nomic-embed asymmetric retrieval prefixes
    prefix = "search_query: " if is_query else "search_document: "
    payload = {"model": _EMBED_MODEL, "input": [prefix + t for t in texts]}
    try:
        req = urllib.request.Request(
            f"{_EMBED_URL}/api/embed",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        embs = data.get("embeddings")
        if not embs:
            return None
        arr = np.asarray(embs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms
    except Exception as e:
        logger.warning(f"Embedding call failed ({_EMBED_URL}): {e}")
        return None


def _doc_for(row: dict, device_class: str = "") -> str:
    """Build the document string embedded for an entity (rich context, not just name)."""
    parts = [row.get("entity_name") or ""]
    if row.get("room"):
        parts.append(f"stanza {row['room']}")
    if row.get("area"):
        parts.append(f"zona {row['area']}")
    if row.get("zone"):
        parts.append(f"piano {row['zone']}")
    if row.get("entity_type"):
        parts.append(f"tipo {row['entity_type']}")
    if device_class:
        parts.append(device_class)
    if row.get("device_name"):
        parts.append(f"dispositivo {row['device_name']}")
    # entity_id tokens: often encode room/name not present in the friendly_name
    # (e.g. cover.tapparella_veranda_salotto → "salotto" is only here)
    eid = row.get("entity_id") or ""
    if eid:
        parts.append(eid.split(".", 1)[-1].replace("_", " "))
    return " | ".join(p for p in parts if p)


def _doc_hash(doc: str) -> str:
    return hashlib.md5(doc.encode("utf-8")).hexdigest()


def _cache_path(location_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in location_id)
    return os.path.join(_CACHE_DIR, f"sem_index_{safe}.pkl")


def _load_disk(location_id: str) -> Dict[str, tuple]:
    try:
        with open(_cache_path(location_id), "rb") as f:
            return pickle.load(f)
    except Exception:
        return {}


def _save_disk(location_id: str, vec_cache: Dict[str, tuple]):
    try:
        with open(_cache_path(location_id), "wb") as f:
            pickle.dump(vec_cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        logger.debug(f"semantic index persist failed for {location_id}: {e}")


async def _rebuild(location_id: str):
    """Full (incremental) rebuild: only re-embeds entities whose doc changed."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        """
        SELECT entity_id, entity_name, entity_type, room, area, zone, device_name,
               COALESCE(visible, 1) AS visible
        FROM entity_maps
        WHERE location_id = ? AND entity_id IS NOT NULL
        """,
        (location_id,),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    if not rows:
        _INDEX[location_id] = {"vecs": np.zeros((0, _DIM), np.float32), "items": [], "built_at": time.time()}
        return

    # device_class live da HA (1 bulk fetch, best-effort)
    dc_map: Dict[str, str] = {}
    states: Dict[str, dict] = {}
    try:
        states = await multi_ha.get_states_bulk(location_id) or {}
        for eid, st in states.items():
            dc = (st.get("attributes") or {}).get("device_class")
            if dc:
                dc_map[eid] = dc
    except Exception as e:
        logger.debug(f"device_class enrich failed for {location_id}: {e}")

    # Supplement with number.* setpoints/targets — the entity_maps sync excludes the
    # `number` domain, but users ask about setpoints ("setpoint umidità camera").
    _existing_ids = {r["entity_id"] for r in rows}
    for _eid, _st in states.items():
        if not _eid.startswith("number.") or _eid in _existing_ids:
            continue
        _attrs = _st.get("attributes") or {}
        _fname = _attrs.get("friendly_name") or _eid
        _dc = _attrs.get("device_class") or ""
        if not (_dc or any(k in _eid.lower() or k in _fname.lower()
                           for k in ("setpoint", "target", "soglia"))):
            continue
        rows.append({"entity_id": _eid, "entity_name": _fname, "entity_type": "number",
                     "room": "", "area": "", "zone": "", "device_name": ""})

    vec_cache = _VEC_CACHE.get(location_id) or _load_disk(location_id)
    items: List[dict] = []
    vec_rows: List[Optional[np.ndarray]] = [None] * len(rows)
    new_cache: Dict[str, tuple] = {}
    to_embed_docs: List[str] = []
    to_embed_pos: List[tuple] = []  # (row_index, entity_id, hash)

    for i, r in enumerate(rows):
        dc = dc_map.get(r["entity_id"], "")
        r["device_class"] = dc
        items.append(r)
        doc = _doc_for(r, dc)
        h = _doc_hash(doc)
        cached = vec_cache.get(r["entity_id"])
        if cached and cached[0] == h:
            new_cache[r["entity_id"]] = cached
            vec_rows[i] = cached[1]
        else:
            to_embed_docs.append(doc)
            to_embed_pos.append((i, r["entity_id"], h))

    if to_embed_docs:
        embs = _embed(to_embed_docs, is_query=False)
        if embs is None or len(embs) != len(to_embed_docs):
            logger.warning(f"Semantic rebuild aborted for '{location_id}' (embedder unavailable)")
            return  # keep whatever index we already had
        for (i, eid, h), v in zip(to_embed_pos, embs):
            vec_rows[i] = v
            new_cache[eid] = (h, v)

    vecs = np.vstack(vec_rows).astype(np.float32) if vec_rows else np.zeros((0, _DIM), np.float32)
    _VEC_CACHE[location_id] = new_cache
    _INDEX[location_id] = {"vecs": vecs, "items": items, "built_at": time.time()}
    _save_disk(location_id, new_cache)
    logger.info(
        f"Semantic index ready '{location_id}': {len(items)} entities, "
        f"{len(to_embed_docs)} (re)embedded, {len(items) - len(to_embed_docs)} cached"
    )


async def _bg_refresh(location_id: str):
    try:
        await _rebuild(location_id)
    finally:
        _REFRESHING.discard(location_id)


def _schedule_refresh(location_id: str):
    if location_id in _REFRESHING:
        return
    _REFRESHING.add(location_id)
    try:
        asyncio.create_task(_bg_refresh(location_id))
    except RuntimeError:
        # no running loop (e.g. sync context) — drop the guard so a later call retries
        _REFRESHING.discard(location_id)


async def build_index(location_id: str, force: bool = False) -> int:
    """Ensure an index exists. NEVER blocks a request on a full embed:

    - fresh index in memory  → use it
    - stale index            → serve it, refresh in background
    - cold (no memory index) → seed from disk cache if present (fast) and refresh
                               in background; if no disk cache either, do ONE
                               awaited build (first ever) so we have something.
    """
    cur = _INDEX.get(location_id)
    if force:
        await _rebuild(location_id)
        return len(_INDEX.get(location_id, {}).get("items", []))

    if cur:
        if time.time() - cur["built_at"] >= _INDEX_TTL:
            _schedule_refresh(location_id)
        return len(cur["items"])

    # Cold: try disk cache to avoid the ~18s first-embed on the request path
    disk = _load_disk(location_id)
    if disk:
        _VEC_CACHE[location_id] = disk
        _schedule_refresh(location_id)  # background reconcile with current entity map
        # publish a provisional index immediately from the disk vectors
        await _rebuild(location_id)  # fast: all hashes hit cache, ~no embedding
        return len(_INDEX.get(location_id, {}).get("items", []))

    # Truly first ever (no memory, no disk): one awaited build, then persisted.
    await _rebuild(location_id)
    return len(_INDEX.get(location_id, {}).get("items", []))


async def warm(location_id: str):
    """Background warm at startup (fire-and-forget)."""
    try:
        await _rebuild(location_id)
    except Exception as e:
        logger.warning(f"Semantic warm failed for {location_id}: {e}")


def invalidate(location_id: Optional[str] = None):
    """Drop in-memory index (call after entity map sync / visibility changes).

    Keeps the on-disk per-entity vector cache so the next build is incremental.
    """
    if location_id:
        _INDEX.pop(location_id, None)
    else:
        _INDEX.clear()


def search_sync(
    location_id: str,
    query: str,
    room: Optional[str] = None,
    domain: Optional[str] = None,
    device_class: Optional[str] = None,
    top_k: int = 8,
    include_hidden: bool = True,
) -> List[dict]:
    """Semantic top-k entity search with optional hard filters.

    Returns a list of entity dicts (entity_id, entity_name, entity_type, room,
    area, zone, device_name, device_class, score), best first. Empty on failure.

    Never blocks on a full embed: if the index isn't ready yet it kicks off a
    background build and returns [] so the caller falls back to SQL this once.
    """
    idx = _INDEX.get(location_id)
    if not idx:
        _schedule_refresh(location_id)  # background build (disk-seeded if available)
        return []
    if time.time() - idx["built_at"] >= _INDEX_TTL:
        _schedule_refresh(location_id)  # serve current, refresh in background
    if not idx["items"]:
        return []

    qv = _embed([query], is_query=True)
    if qv is None:
        return []
    sims = idx["vecs"] @ qv[0]  # cosine (vectors are normalized)

    items = idx["items"]
    n = len(items)

    # Setpoint de-prioritisation: "temperatura salotto" wants the READING, not the
    # temperature setpoint. Unless the query explicitly asks for a setpoint/target,
    # penalise setpoint/target entities so live readings win.
    _ql = query.lower()
    if not any(w in _ql for w in ("setpoint", "impostat", "target", "richiest", "soglia")):
        # `number.*` entities are settable setpoints/targets/config — a reading query
        # ("temperatura salotto", "mandata pompa") wants the sensor, not these.
        _sp = np.array(
            [it.get("entity_type") == "number"
             or "setpoint" in it["entity_id"].lower() or "target" in it["entity_id"].lower()
             for it in items],
            dtype=np.float32,
        )
        sims = sims - 0.15 * _sp

    # Hard filters: domain + device_class (reliable). Room is SOFT: applied only if
    # it leaves results, because the router often misfills it with a zone/location.
    base = np.ones(n, dtype=bool)
    if not include_hidden:
        # path di CONTROLLO: mai risolvere comandi su entità nascoste dalla mappa
        base &= np.array([bool(it.get("visible", 1)) for it in items])
    if domain:
        base &= np.array([it.get("entity_type") == domain for it in items])
    if device_class:
        base &= np.array([it.get("device_class") == device_class for it in items])

    mask = base
    if room:
        rl = room.lower()
        # Match room/area only (NOT the floor/zone): the router tends to drop a
        # floor name like "Piano Giorno" here for room-less devices (heat pump,
        # meter), which would otherwise wrongly filter out the right entity.
        # Room-agnostic entities (no room/area, e.g. number setpoints) are NOT
        # excluded — semantic rank decides among them.
        room_m = base & np.array([
            (not ((it.get("room") or "") or (it.get("area") or "")))
            or rl in (it.get("room") or "").lower()
            or rl in (it.get("area") or "").lower()
            for it in items
        ])
        if room_m.any():
            mask = room_m

    # Last resort: if hard filters emptied everything, fall back to pure semantic rank
    if not mask.any():
        mask = np.ones(n, dtype=bool)

    out = []
    for i in np.argsort(-sims):
        if not mask[i]:
            continue
        it = dict(items[i])
        it["score"] = float(sims[i])
        out.append(it)
        if len(out) >= top_k:
            break
    return out


async def search(
    location_id: str,
    query: str,
    room: Optional[str] = None,
    domain: Optional[str] = None,
    device_class: Optional[str] = None,
    top_k: int = 8,
    include_hidden: bool = True,
) -> List[dict]:
    """Async wrapper around search_sync (the core has no awaits)."""
    return search_sync(location_id, query, room=room, domain=domain,
                       device_class=device_class, top_k=top_k,
                       include_hidden=include_hidden)
