"""
Ontology Graph API — REST interface for the Knowledge Graph.

Endpoints:
    CRUD:       POST/GET/PATCH/DELETE /entities, POST /entities/query, POST /entities/bulk
    Relations:  POST/DELETE /relations, GET /entities/{id}/related
    Helpers:    GET /helpers/blocked-tasks, /workload, /project-summary/{id}, /path
    Admin:      GET /health, POST /admin/validate
"""

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from pathlib import Path as FilePath

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import JSONResponse

import helpers
import ontology

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("ONTOLOGY_DB_PATH", ontology.DEFAULT_DB_PATH)
SCHEMA_PATH = os.getenv("ONTOLOGY_SCHEMA_PATH", ontology.DEFAULT_SCHEMA_PATH)
ONTOLOGY_API_TOKEN = os.getenv("ONTOLOGY_API_TOKEN", "")

# Cache for admin role lookups (speaker_id → is_admin bool)
# Cleared on entity updates to Person type
_admin_cache: dict[str, bool] = {}


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    ontology.init_db(DB_PATH)
    yield
    ontology.close_db()


app = FastAPI(
    title="Ontology Graph API",
    description="REST API per il Knowledge Graph personale degli Agenti AI.",
    version="2.2.0",
    lifespan=lifespan,
)


def resolve_admin(speaker: str | None) -> bool:
    """Check if speaker has admin role. Uses a simple cache to avoid repeated DB queries."""
    if not speaker:
        return False
    if speaker in _admin_cache:
        return _admin_cache[speaker]
    result = ontology.is_speaker_admin(speaker, DB_PATH)
    _admin_cache[speaker] = result
    return result


# ---------------------------------------------------------------------------
# Optional bearer token middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def bearer_auth_middleware(request: Request, call_next):
    if not ONTOLOGY_API_TOKEN:
        return await call_next(request)

    # Skip auth for health/docs/dashboard/schema/speakers
    if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc", "/schema", "/speakers") \
       or request.url.path.startswith("/dashboard"):
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != ONTOLOGY_API_TOKEN:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing bearer token"},
        )

    return await call_next(request)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class EntityCreate(BaseModel):
    type: str
    properties: Dict[str, Any] = {}


class EntityUpdate(BaseModel):
    properties: Dict[str, Any]


class RelationCreate(BaseModel):
    from_id: str
    rel_type: str
    to_id: str
    properties: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Admin"])
def health():
    try:
        conn = ontology.get_db()
        conn.execute("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# CRUD — Entities
# ---------------------------------------------------------------------------
@app.post("/entities", response_model=Dict[str, Any], tags=["Entities"])
def create_entity(
    entity: EntityCreate,
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)

    # Anonymous cannot create entities (unless admin)
    if not speaker and not admin:
        raise HTTPException(status_code=403, detail="X-Speaker-Id required to create entities")

    props = dict(entity.properties)

    # Auto-assign owner from speaker
    if speaker and "owner" not in props:
        props["owner"] = speaker

    # Schema validation
    errors = ontology.validate_entity_properties(
        entity.type, props, SCHEMA_PATH, is_update=False
    )
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    # Invalidate admin cache if creating/modifying a Person
    if entity.type == "Person":
        _admin_cache.clear()

    try:
        return ontology.create_entity(entity.type, props, DB_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/entities/{entity_id}", response_model=Dict[str, Any], tags=["Entities"])
def get_entity(
    entity_id: str,
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    result = ontology.get_entity(entity_id, DB_PATH, speaker, is_admin=admin)
    if not result:
        raise HTTPException(
            status_code=404, detail="Entity not found or access denied"
        )
    return result


@app.get("/entities", response_model=List[Dict[str, Any]], tags=["Entities"])
def list_entities(
    type: Optional[str] = Query(None, description="Filter by entity type"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    """List entities with optional type filter and pagination."""
    admin = resolve_admin(speaker)
    return ontology.list_entities(type, DB_PATH, speaker, limit=limit, offset=offset, is_admin=admin)


@app.patch("/entities/{entity_id}", response_model=Dict[str, Any], tags=["Entities"])
def update_entity(
    entity_id: str,
    entity: EntityUpdate,
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)

    # Get entity type for validation (need to know what we're updating)
    existing = ontology.get_entity(entity_id, DB_PATH, speaker, is_admin=admin)
    if existing:
        errors = ontology.validate_entity_properties(
            existing["type"], entity.properties, SCHEMA_PATH, is_update=True
        )
        if errors:
            raise HTTPException(
                status_code=422, detail={"validation_errors": errors}
            )

        # Invalidate admin cache if modifying a Person (role might change)
        if existing["type"] == "Person":
            _admin_cache.clear()

    try:
        result = ontology.update_entity(
            entity_id, entity.properties, DB_PATH, speaker, is_admin=admin
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    if not result:
        raise HTTPException(
            status_code=404, detail="Entity not found or access denied"
        )
    return result


@app.delete("/entities/{entity_id}", tags=["Entities"])
def delete_entity(
    entity_id: str,
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    success = ontology.delete_entity(entity_id, DB_PATH, speaker, is_admin=admin)
    if not success:
        raise HTTPException(
            status_code=404, detail="Entity not found or access denied"
        )
    # Invalidate admin cache on delete (might be a Person)
    _admin_cache.pop(speaker, None) if speaker else None
    return {"status": "deleted", "id": entity_id}


@app.post(
    "/entities/query", response_model=List[Dict[str, Any]], tags=["Entities"]
)
def query_entities(
    type: Optional[str] = Query(None, description="Filter by entity type"),
    where: Dict[str, Any] = Body({}),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    """Search entities by exact property match. Pass JSON filters in body."""
    admin = resolve_admin(speaker)
    try:
        return ontology.query_entities(
            type, where, DB_PATH, speaker, limit=limit, offset=offset, is_admin=admin
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post(
    "/entities/bulk", response_model=List[Dict[str, Any]], tags=["Entities"]
)
def create_entities_bulk(
    entities: List[EntityCreate],
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    """Create multiple entities at once."""
    if not speaker:
        raise HTTPException(status_code=403, detail="X-Speaker-Id required to create entities")

    results = []
    has_person = False
    for entity in entities:
        props = dict(entity.properties)
        if speaker and "owner" not in props:
            props["owner"] = speaker

        errors = ontology.validate_entity_properties(
            entity.type, props, SCHEMA_PATH, is_update=False
        )
        if errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "entity_type": entity.type,
                    "validation_errors": errors,
                },
            )
        if entity.type == "Person":
            has_person = True

        result = ontology.create_entity(entity.type, props, DB_PATH)
        results.append(result)

    if has_person:
        _admin_cache.clear()
    return results


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------
@app.post("/relations", tags=["Relations"])
def create_relation(
    rel: RelationCreate,
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    # ACL: verify speaker has access to both endpoint entities
    from_entity = ontology.get_entity(rel.from_id, DB_PATH, speaker, is_admin=admin)
    if not from_entity:
        raise HTTPException(
            status_code=404,
            detail=f"Entity {rel.from_id} not found or access denied",
        )
    to_entity = ontology.get_entity(rel.to_id, DB_PATH, speaker, is_admin=admin)
    if not to_entity:
        raise HTTPException(
            status_code=404,
            detail=f"Entity {rel.to_id} not found or access denied",
        )

    try:
        return ontology.create_relation(
            rel.from_id, rel.rel_type, rel.to_id, rel.properties, DB_PATH
        )
    except SystemExit as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/relations", tags=["Relations"])
def delete_relation(
    from_id: str = Query(...),
    rel_type: str = Query(...),
    to_id: str = Query(...),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    # ACL: verify speaker has access to both endpoint entities
    from_entity = ontology.get_entity(from_id, DB_PATH, speaker, is_admin=admin)
    if not from_entity:
        raise HTTPException(
            status_code=404,
            detail=f"Entity {from_id} not found or access denied",
        )
    to_entity = ontology.get_entity(to_id, DB_PATH, speaker, is_admin=admin)
    if not to_entity:
        raise HTTPException(
            status_code=404,
            detail=f"Entity {to_id} not found or access denied",
        )

    success = ontology.delete_relation(from_id, rel_type, to_id, DB_PATH)
    if not success:
        raise HTTPException(status_code=404, detail="Relation not found")
    return {"status": "deleted", "from": from_id, "rel": rel_type, "to": to_id}


@app.get("/relations", tags=["Relations"])
def list_relations(
    rel_type: Optional[str] = Query(None, description="Filter by relation type"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    """List all relations with resolved entity labels."""
    admin = resolve_admin(speaker)
    conn = ontology.get_db(DB_PATH)

    query = """
        SELECT r.from_id, r.rel_type, r.to_id, r.properties AS rel_properties, r.created,
               ef.type AS from_type, ef.properties AS from_props,
               et.type AS to_type, et.properties AS to_props
        FROM relations r
        JOIN entities ef ON ef.id = r.from_id
        JOIN entities et ON et.id = r.to_id
    """
    params: list = []
    clauses = []

    if rel_type:
        clauses.append("r.rel_type = ?")
        params.append(rel_type)

    # ACL: filter both endpoints
    if not admin:
        acl_from = ontology.get_acl_clause("ef", is_admin=False)
        acl_to = ontology.get_acl_clause("et", is_admin=False)
        clauses.append(acl_from)
        params.extend(ontology.get_acl_params(speaker, False))
        clauses.append(acl_to)
        params.extend(ontology.get_acl_params(speaker, False))

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY r.created DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    import json as _json

    def _entity_label(props: dict, etype: str, eid: str) -> str:
        """Derive a human-readable label from entity properties."""
        # Preference: "subject: value"
        if etype == "Preference" and props.get("subject") and props.get("value"):
            return f'{props["subject"]}: {props["value"]}'
        return (
            props.get("name") or props.get("title") or props.get("subject")
            or props.get("service") or props.get("scope") or eid
        )

    results = []
    for row in conn.execute(query, params):
        from_props = _json.loads(row["from_props"])
        to_props = _json.loads(row["to_props"])
        rel_props = _json.loads(row["rel_properties"]) if row["rel_properties"] else {}
        results.append({
            "from_id": row["from_id"],
            "from_type": row["from_type"],
            "from_label": _entity_label(from_props, row["from_type"], row["from_id"]),
            "rel_type": row["rel_type"],
            "to_id": row["to_id"],
            "to_type": row["to_type"],
            "to_label": _entity_label(to_props, row["to_type"], row["to_id"]),
            "properties": rel_props,
            "created": row["created"],
        })
    return results


@app.patch("/relations", tags=["Relations"])
def update_relation_properties(
    from_id: str = Query(...),
    rel_type: str = Query(...),
    to_id: str = Query(...),
    properties: Dict[str, Any] = Body({}),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    """Update properties on an existing relation (e.g. add relation_type_enum)."""
    admin = resolve_admin(speaker)
    from_entity = ontology.get_entity(from_id, DB_PATH, speaker, is_admin=admin)
    if not from_entity:
        raise HTTPException(status_code=404, detail=f"Entity {from_id} not found or access denied")

    conn = ontology.get_db(DB_PATH)
    import json as _json
    cursor = conn.execute(
        """UPDATE relations
           SET properties = json_patch(coalesce(properties, '{}'), ?)
           WHERE from_id = ? AND rel_type = ? AND to_id = ?""",
        (_json.dumps(properties), from_id, rel_type, to_id),
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Relation not found")
    conn.commit()
    return {"status": "updated", "from": from_id, "rel": rel_type, "to": to_id, "properties": properties}


@app.post("/relations/infer", tags=["Relations"])
def infer_relations(
    dry_run: bool = Query(True, description="If true, only report what would be created"),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    """Infer transitive family relations from existing 'related_to' edges.

    Rules applied:
    - A's parent + B's parent (same person) → A sibling B (if not already)
    - A parent→B, B parent→C → A grandparent→C (via 'parent' chain)
    - A child→B means B parent→A (creates missing reverse if needed)

    Returns list of inferred relations. Use dry_run=false to actually create them.
    """
    import json as _json
    admin = resolve_admin(speaker)
    if not admin:
        raise HTTPException(status_code=403, detail="Only admin can run inference")

    conn = ontology.get_db(DB_PATH)

    # Load all related_to relations with their properties
    rows = conn.execute(
        "SELECT from_id, to_id, properties FROM relations WHERE rel_type = 'related_to'"
    ).fetchall()

    # Build lookup: (from_id, to_id) -> relation_type
    edges = {}
    for row in rows:
        props = _json.loads(row["properties"]) if row["properties"] else {}
        rt = props.get("relation_type_enum", "")
        edges[(row["from_id"], row["to_id"])] = rt

    # Build parent/child maps
    parents_of = {}   # child_id -> set of parent_ids
    children_of = {}  # parent_id -> set of child_ids

    for (from_id, to_id), rt in edges.items():
        if rt == "parent":
            # "from_id parent→ to_id" means from_id IS parent OF to_id
            children_of.setdefault(from_id, set()).add(to_id)
            parents_of.setdefault(to_id, set()).add(from_id)
        elif rt == "child":
            # "from_id child→ to_id" means from_id IS child OF to_id
            parents_of.setdefault(from_id, set()).add(to_id)
            children_of.setdefault(to_id, set()).add(from_id)

    inferred = []

    def should_add(fid, tid, rtype):
        """Check if this relation already exists."""
        existing = edges.get((fid, tid), "")
        if existing == rtype:
            return False
        # Also check reverse for symmetric relations
        if rtype == "sibling":
            rev = edges.get((tid, fid), "")
            if rev == "sibling":
                return False
        return True

    # Rule 1: Missing reverse parent↔child
    for (fid, tid), rt in list(edges.items()):
        if rt == "parent" and should_add(tid, fid, "child"):
            inferred.append({"from_id": tid, "to_id": fid, "relation_type_enum": "child",
                             "reason": f"{tid} is child of {fid} (reverse of parent)"})
        elif rt == "child" and should_add(tid, fid, "parent"):
            inferred.append({"from_id": tid, "to_id": fid, "relation_type_enum": "parent",
                             "reason": f"{tid} is parent of {fid} (reverse of child)"})

    # Rule 2: Siblings (same parents)
    for parent_id, kids in children_of.items():
        kid_list = list(kids)
        for i in range(len(kid_list)):
            for j in range(i + 1, len(kid_list)):
                a, b = kid_list[i], kid_list[j]
                if should_add(a, b, "sibling"):
                    inferred.append({"from_id": a, "to_id": b, "relation_type_enum": "sibling",
                                     "reason": f"share parent {parent_id}"})
                if should_add(b, a, "sibling"):
                    inferred.append({"from_id": b, "to_id": a, "relation_type_enum": "sibling",
                                     "reason": f"share parent {parent_id}"})

    # Rule 3: Grandparent/grandchild (parent chain of length 2)
    for child_id, pset in parents_of.items():
        for parent_id in pset:
            grandparents = parents_of.get(parent_id, set())
            for gp_id in grandparents:
                if should_add(child_id, gp_id, "grandparent"):
                    inferred.append({"from_id": child_id, "to_id": gp_id,
                                     "relation_type_enum": "grandparent",
                                     "reason": f"{child_id} → parent → {parent_id} → parent → {gp_id}"})
                if should_add(gp_id, child_id, "grandchild"):
                    inferred.append({"from_id": gp_id, "to_id": child_id,
                                     "relation_type_enum": "grandchild",
                                     "reason": f"{gp_id} → child → {parent_id} → child → {child_id}"})

    # Rule 4: Spouse inference — if A spouse B, ensure B spouse A
    for (fid, tid), rt in list(edges.items()):
        if rt == "spouse" and should_add(tid, fid, "spouse"):
            inferred.append({"from_id": tid, "to_id": fid, "relation_type_enum": "spouse",
                             "reason": f"reverse of {fid} spouse {tid}"})

    if not dry_run and inferred:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for inf in inferred:
            props = _json.dumps({"relation_type_enum": inf["relation_type_enum"]})
            try:
                conn.execute(
                    "INSERT INTO relations (from_id, rel_type, to_id, properties, created) VALUES (?, 'related_to', ?, ?, ?)",
                    (inf["from_id"], inf["to_id"], props, now),
                )
            except Exception:
                inf["skipped"] = True
        conn.commit()

    return {
        "dry_run": dry_run,
        "inferred_count": len([i for i in inferred if not i.get("skipped")]),
        "inferred": inferred,
    }


@app.get("/entities/{entity_id}/related", tags=["Relations"])
def get_related(
    entity_id: str,
    rel: Optional[str] = Query(None, description="Relation type filter"),
    dir: str = Query("outgoing", enum=["outgoing", "incoming", "both"]),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    return ontology.get_related(entity_id, rel, DB_PATH, dir, speaker, is_admin=admin)


# ---------------------------------------------------------------------------
# Helpers (advanced queries)
# ---------------------------------------------------------------------------
@app.get("/helpers/blocked-tasks", tags=["Helpers"])
def get_blocked_tasks(
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    return helpers.find_blocked_tasks(DB_PATH, speaker, is_admin=admin)


@app.get("/helpers/workload", tags=["Helpers"])
def get_workload(
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    return helpers.workload_by_person(DB_PATH, speaker, is_admin=admin)


@app.get("/helpers/project-summary/{project_id}", tags=["Helpers"])
def get_project_summary(
    project_id: str,
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    return helpers.task_status_summary(project_id, DB_PATH, speaker, is_admin=admin)


@app.get("/helpers/path", tags=["Helpers"])
def get_path(
    from_id: str = Query(...),
    to_id: str = Query(...),
    depth: int = Query(5, le=10),
    speaker: Optional[str] = Header(None, alias="X-Speaker-Id"),
):
    admin = resolve_admin(speaker)
    return helpers.find_path(from_id, to_id, DB_PATH, speaker, depth, is_admin=admin)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.post("/admin/validate", tags=["Admin"])
def validate_graph():
    errors = ontology.validate_graph(DB_PATH, SCHEMA_PATH)
    if errors:
        return {"valid": False, "errors": errors}
    return {"valid": True, "errors": []}


@app.get("/schema", tags=["Admin"])
def get_schema():
    """Return parsed schema as JSON for dashboard dynamic forms."""
    schema = ontology.load_schema(SCHEMA_PATH)
    return schema


@app.get("/speakers", tags=["Admin"])
def list_speakers():
    """Return Person entities as speaker options for dashboard.

    Returns [{id, name, speaker_id, type, role}] for all Person entities.
    speaker_id is the value to send as X-Speaker-Id header.
    It uses owner (if set and not the creator of all entities), otherwise entity id.
    """
    conn = ontology.get_db(DB_PATH)
    rows = conn.execute(
        "SELECT id, properties FROM entities WHERE type = 'Person' ORDER BY updated DESC"
    ).fetchall()

    import json as _json

    # Collect all owners to detect if they are distinct
    all_owners = set()
    parsed = []
    for row in rows:
        props = _json.loads(row["properties"])
        parsed.append({"id": row["id"], "props": props})
        if props.get("owner"):
            all_owners.add(props["owner"])

    # Count how many entities share each owner value
    owner_counts: dict[str, int] = {}
    for item in parsed:
        o = item["props"].get("owner", "")
        if o:
            owner_counts[o] = owner_counts.get(o, 0) + 1

    speakers = []
    for item in parsed:
        props = item["props"]
        eid = item["id"]
        owner = props.get("owner", "")
        name = props.get("name", eid)
        etype = props.get("type_enum", props.get("type", "Human"))
        role = props.get("role_enum", props.get("role", "user"))

        # Use owner as speaker_id only if this owner value is unique
        # (i.e. only one Person entity has that owner — it's their own).
        # Otherwise fall back to entity id to avoid collisions.
        if owner and owner_counts.get(owner, 0) == 1:
            speaker_id = owner
        else:
            speaker_id = eid

        speakers.append({
            "id": eid,
            "name": name,
            "speaker_id": speaker_id,
            "type": etype,
            "role": role,
        })
    return speakers


# ---------------------------------------------------------------------------
# Dashboard (static files — must be mounted LAST)
# ---------------------------------------------------------------------------
_static_dir = FilePath(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/dashboard", StaticFiles(directory=str(_static_dir), html=True), name="dashboard")
