#!/usr/bin/env python3
"""
Ontology graph operations via SQLite: create, query, relate, validate.

Usage (CLI):
    python ontology.py create --type Person --props '{"name":"Alice"}'
    python ontology.py get --id p_001 --speaker user_ada
    python ontology.py query --type Task --where '{"status":"open"}' --speaker user_ada
    python ontology.py relate --from proj_001 --rel has_task --to task_001
    python ontology.py related --id proj_001 --rel has_task
    python ontology.py list --type Person
    python ontology.py delete --id p_001
    python ontology.py validate
"""

import argparse
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (environment-driven for Docker)
# ---------------------------------------------------------------------------
DEFAULT_DB_PATH = os.getenv("ONTOLOGY_DB_PATH", "data/graph.db")
DEFAULT_SCHEMA_PATH = os.getenv("ONTOLOGY_SCHEMA_PATH", "schema.yaml")

# ---------------------------------------------------------------------------
# Property key validation (prevents SQL injection via json_extract)
# ---------------------------------------------------------------------------
_SAFE_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _validate_property_key(key: str) -> str:
    """Whitelist property key names to prevent SQL injection via json_extract."""
    if not _SAFE_KEY_RE.match(key):
        raise ValueError(f"Invalid property key: {key!r}")
    return key


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
def resolve_safe_path(
    user_path: str,
    *,
    root: Path | None = None,
    must_exist: bool = False,
    label: str = "path",
) -> Path:
    """Resolve user path within root and reject traversal outside it."""
    if not user_path or not user_path.strip():
        raise SystemExit(f"Invalid {label}: empty path")

    safe_root = (root or Path.cwd()).resolve()
    candidate = Path(user_path).expanduser()
    if not candidate.is_absolute():
        candidate = safe_root / candidate

    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise SystemExit(f"Invalid {label}: {exc}") from exc

    try:
        resolved.relative_to(safe_root)
    except ValueError:
        raise SystemExit(
            f"Invalid {label}: must stay within workspace root '{safe_root}'"
        )

    if must_exist and not resolved.exists():
        raise SystemExit(f"Invalid {label}: file not found '{resolved}'")

    return resolved


# ---------------------------------------------------------------------------
# Database connection management
# ---------------------------------------------------------------------------
_connection: sqlite3.Connection | None = None


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise DB once at startup: create tables, set PRAGMAs, enable WAL."""
    global _connection
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    # Tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            properties JSON NOT NULL,
            created TEXT NOT NULL,
            updated TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relations (
            from_id TEXT,
            rel_type TEXT NOT NULL,
            to_id TEXT,
            properties JSON,
            created TEXT NOT NULL,
            FOREIGN KEY(from_id) REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY(to_id) REFERENCES entities(id) ON DELETE CASCADE,
            PRIMARY KEY (from_id, rel_type, to_id)
        )
    """)

    # Indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(rel_type)"
    )

    conn.commit()
    _connection = conn
    return conn


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """Return the cached connection. Falls back to init if not yet initialised."""
    global _connection
    if _connection is None:
        return init_db(db_path or DEFAULT_DB_PATH)
    return _connection


def close_db():
    """Close DB connection at shutdown."""
    global _connection
    if _connection:
        _connection.close()
        _connection = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def generate_id(type_name: str) -> str:
    prefix = type_name.lower()[:4]
    suffix = uuid.uuid4().hex[:8]
    return f"{prefix}_{suffix}"


def row_to_entity(row: sqlite3.Row) -> dict | None:
    """Convert SQLite Row to dictionary matching the entity schema."""
    if not row:
        return None
    return {
        "id": row["id"],
        "type": row["type"],
        "properties": json.loads(row["properties"]),
        "created": row["created"],
        "updated": row["updated"],
    }


# ---------------------------------------------------------------------------
# ACL (Access Control List)
# ---------------------------------------------------------------------------
def is_speaker_admin(speaker: str | None, db_path: str | None = None) -> bool:
    """Check if speaker has elevated privileges (admin role OR agent type).

    Returns True if the speaker's Person entity has:
    - role_enum = 'admin' (human admin, full access)
    - type_enum = 'Agent' (service account, trusted — full read/write)

    Returns False for NULL/anonymous speakers.
    """
    if not speaker:
        return False
    conn = get_db(db_path)
    row = conn.execute(
        """SELECT 1
           FROM entities
           WHERE type = 'Person'
             AND (id = ? OR json_extract(properties, '$.owner') = ?)
             AND (json_extract(properties, '$.role_enum') = 'admin'
                  OR json_extract(properties, '$.role') = 'admin'
                  OR json_extract(properties, '$.type_enum') = 'Agent'
                  OR json_extract(properties, '$.type') = 'Agent')
           LIMIT 1""",
        (speaker, speaker),
    ).fetchone()
    return row is not None


def get_acl_clause(table_alias: str = "", is_admin: bool = False) -> str:
    """SQL clause for READ access — filters entities based on speaker visibility.

    Requires 2 bind parameters (both the same speaker value):
        (speaker, speaker)

    Args:
        table_alias: Optional table alias prefix (e.g. "e") for disambiguating
                     JOINs where both tables have 'id' and 'properties' columns.
        is_admin: If True, bypasses all ACL checks (returns 1=1 with 0 params).

    Logic:
    - If is_admin → no filtering
    - If speaker is NULL → only 'public' entities
    - If entity owner == speaker → always visible (owner sees their own data)
    - If visibility is 'family' and speaker is not NULL → visible (authenticated household member)
    - If visibility is 'public' → visible to everyone
    - If visibility is 'private' → only visible to owner
    """
    if is_admin:
        return "1=1"

    prefix = f"{table_alias}." if table_alias else ""
    return f"""
        (json_extract({prefix}properties, '$.owner') = ?
         OR coalesce(json_extract({prefix}properties, '$.visibility'), 'public') = 'public'
         OR (coalesce(json_extract({prefix}properties, '$.visibility'), 'public') = 'family'
             AND ? IS NOT NULL))
    """


def get_acl_params(speaker: str | None, is_admin: bool = False) -> list:
    """Return the bind parameters for get_acl_clause."""
    if is_admin:
        return []
    return [speaker, speaker]


def get_write_acl_clause(table_alias: str = "", is_admin: bool = False) -> str:
    """SQL clause for WRITE access — only owner or admin can modify/delete.

    Requires 1 bind parameter (speaker) unless is_admin.

    Logic:
    - If is_admin → allowed
    - If speaker is NULL → denied (anonymous cannot write)
    - If entity owner == speaker → allowed
    """
    if is_admin:
        return "1=1"

    prefix = f"{table_alias}." if table_alias else ""
    return f"""
        (? IS NOT NULL
         AND json_extract({prefix}properties, '$.owner') = ?)
    """


def get_write_acl_params(speaker: str | None, is_admin: bool = False) -> list:
    """Return the bind parameters for get_write_acl_clause."""
    if is_admin:
        return []
    return [speaker, speaker]


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
_schema_cache: dict | None = None


def load_schema(schema_path: str) -> dict:
    schema_file = Path(schema_path)
    if schema_file.exists():
        import yaml

        with open(schema_file) as f:
            return yaml.safe_load(f) or {}
    return {}


def _get_schema(schema_path: str) -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = load_schema(schema_path)
    return _schema_cache


def validate_entity_properties(
    type_name: str,
    properties: dict,
    schema_path: str,
    is_update: bool = False,
) -> list[str]:
    """Validate properties against schema. Returns list of error messages."""
    errors = []
    schema = _get_schema(schema_path)
    type_schema = schema.get("types", {}).get(type_name, {})
    if not type_schema:
        return errors  # Unknown type — permissive

    # Required fields (only on create, not partial update)
    # Convention: required field "status" is satisfied by either "status" or "status_enum"
    if not is_update:
        for prop in type_schema.get("required", []):
            if prop not in properties and f"{prop}_enum" not in properties:
                errors.append(f"Missing required property: '{prop}'")

    # Forbidden properties
    for prop in type_schema.get("forbidden_properties", []):
        if prop in properties:
            errors.append(f"Forbidden property: '{prop}'")

    # Enum validation
    # Convention: schema defines "status_enum: [open, done]" — check both
    # "status" and "status_enum" property names in the data
    for schema_key, allowed_values in type_schema.items():
        if schema_key.endswith("_enum") and isinstance(allowed_values, list):
            bare_field = schema_key.replace("_enum", "")
            # Check both "status" and "status_enum" in properties
            value = properties.get(bare_field) or properties.get(schema_key)
            if value is not None and value not in allowed_values:
                errors.append(
                    f"'{bare_field}' must be one of {allowed_values}, got '{value}'"
                )

    return errors


# ---------------------------------------------------------------------------
# CRUD — Entities
# ---------------------------------------------------------------------------
def create_entity(
    type_name: str,
    properties: dict,
    db_path: str,
    entity_id: str | None = None,
) -> dict:
    entity_id = entity_id or generate_id(type_name)
    timestamp = datetime.now(timezone.utc).isoformat()

    conn = get_db(db_path)
    conn.execute(
        "INSERT INTO entities (id, type, properties, created, updated) VALUES (?, ?, ?, ?, ?)",
        (entity_id, type_name, json.dumps(properties), timestamp, timestamp),
    )
    conn.commit()

    # Return without ACL filtering — caller just created it, so they own it
    return {
        "id": entity_id,
        "type": type_name,
        "properties": properties,
        "created": timestamp,
        "updated": timestamp,
    }


def get_entity(
    entity_id: str, db_path: str, speaker: str | None = None,
    is_admin: bool = False,
) -> dict | None:
    conn = get_db(db_path)
    acl = get_acl_clause(is_admin=is_admin)
    params = [entity_id] + get_acl_params(speaker, is_admin)
    cursor = conn.execute(
        f"SELECT * FROM entities WHERE id = ? AND {acl}", params,
    )
    return row_to_entity(cursor.fetchone())


def query_entities(
    type_name: str | None,
    where: dict,
    db_path: str,
    speaker: str | None = None,
    limit: int = 100,
    offset: int = 0,
    is_admin: bool = False,
) -> list:
    query = "SELECT * FROM entities WHERE 1=1"
    params: list = []

    if type_name:
        query += " AND type = ?"
        params.append(type_name)

    for key, value in where.items():
        _validate_property_key(key)
        query += f" AND json_extract(properties, '$.{key}') = ?"
        params.append(value)

    query += f" AND {get_acl_clause(is_admin=is_admin)}"
    params.extend(get_acl_params(speaker, is_admin))

    query += " ORDER BY updated DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_db(db_path)
    cursor = conn.execute(query, params)
    return [row_to_entity(row) for row in cursor.fetchall()]


def list_entities(
    type_name: str | None,
    db_path: str,
    speaker: str | None = None,
    limit: int = 100,
    offset: int = 0,
    is_admin: bool = False,
) -> list:
    query = "SELECT * FROM entities WHERE 1=1"
    params: list = []

    if type_name:
        query += " AND type = ?"
        params.append(type_name)

    query += f" AND {get_acl_clause(is_admin=is_admin)}"
    params.extend(get_acl_params(speaker, is_admin))

    query += " ORDER BY updated DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_db(db_path)
    cursor = conn.execute(query, params)
    return [row_to_entity(row) for row in cursor.fetchall()]


def update_entity(
    entity_id: str,
    properties: dict,
    db_path: str,
    speaker: str | None = None,
    is_admin: bool = False,
) -> dict | None:
    conn = get_db(db_path)

    # Anonymous cannot write
    if not speaker and not is_admin:
        return None

    # Transaction immutability check
    row = conn.execute(
        "SELECT type, properties FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if row and row["type"] == "Transaction":
        existing_props = json.loads(row["properties"])
        if existing_props.get("status_enum") == "confirmed":
            raise ValueError(
                "Confirmed transactions are immutable and cannot be modified"
            )

    timestamp = datetime.now(timezone.utc).isoformat()
    write_acl = get_write_acl_clause(is_admin=is_admin)
    params = [json.dumps(properties), timestamp, entity_id] + get_write_acl_params(speaker, is_admin)
    cursor = conn.execute(
        f"""UPDATE entities
            SET properties = json_patch(properties, ?), updated = ?
            WHERE id = ? AND {write_acl}""",
        params,
    )
    if cursor.rowcount == 0:
        return None
    conn.commit()

    return get_entity(entity_id, db_path, speaker, is_admin=is_admin)


def delete_entity(
    entity_id: str, db_path: str, speaker: str | None = None,
    is_admin: bool = False,
) -> bool:
    # Anonymous cannot write
    if not speaker and not is_admin:
        return False

    conn = get_db(db_path)
    write_acl = get_write_acl_clause(is_admin=is_admin)
    params = [entity_id] + get_write_acl_params(speaker, is_admin)
    cursor = conn.execute(
        f"DELETE FROM entities WHERE id = ? AND {write_acl}", params,
    )
    conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD — Relations
# ---------------------------------------------------------------------------
def create_relation(
    from_id: str,
    rel_type: str,
    to_id: str,
    properties: dict,
    db_path: str,
) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_db(db_path)
        conn.execute(
            "INSERT INTO relations (from_id, rel_type, to_id, properties, created) VALUES (?, ?, ?, ?, ?)",
            (from_id, rel_type, to_id, json.dumps(properties), timestamp),
        )
        conn.commit()
        return {
            "from": from_id,
            "rel": rel_type,
            "to": to_id,
            "properties": properties,
        }
    except sqlite3.IntegrityError as e:
        raise SystemExit(
            f"Relation error: missing entity or relation already exists. Detail: {e}"
        )


def delete_relation(
    from_id: str, rel_type: str, to_id: str, db_path: str
) -> bool:
    conn = get_db(db_path)
    cursor = conn.execute(
        "DELETE FROM relations WHERE from_id = ? AND rel_type = ? AND to_id = ?",
        (from_id, rel_type, to_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_related(
    entity_id: str,
    rel_type: str | None,
    db_path: str,
    direction: str = "outgoing",
    speaker: str | None = None,
    is_admin: bool = False,
) -> list:
    results = []

    base_query = """
        SELECT r.rel_type, r.from_id, r.to_id, r.properties AS rel_properties,
               e.id, e.type, e.properties, e.created, e.updated
        FROM relations r
        JOIN entities e ON
    """

    # Build per-direction params explicitly (fixes "both" ordering bug)
    if direction == "outgoing":
        base_query += "e.id = r.to_id WHERE r.from_id = ?"
        params = [entity_id]
    elif direction == "incoming":
        base_query += "e.id = r.from_id WHERE r.to_id = ?"
        params = [entity_id]
    else:  # both
        base_query += (
            "(e.id = r.to_id OR e.id = r.from_id) "
            "WHERE (r.from_id = ? OR r.to_id = ?) AND e.id != ?"
        )
        params = [entity_id, entity_id, entity_id]

    if rel_type:
        base_query += " AND r.rel_type = ?"
        params.append(rel_type)

    base_query += f" AND {get_acl_clause('e', is_admin=is_admin)}"
    params.extend(get_acl_params(speaker, is_admin))

    conn = get_db(db_path)
    for row in conn.execute(base_query, params):
        rel_dir = (
            "outgoing" if row["from_id"] == entity_id else "incoming"
        )
        rel_props = json.loads(row["rel_properties"]) if row["rel_properties"] else {}
        results.append(
            {
                "relation": row["rel_type"],
                "direction": rel_dir if direction == "both" else direction,
                "entity": row_to_entity(row),
                "properties": rel_props,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Validation & Schema management
# ---------------------------------------------------------------------------
def validate_graph(db_path: str, schema_path: str) -> list:
    """Validate graph against schema constraints directly from SQLite."""
    errors = []
    schema = load_schema(schema_path)
    if not schema:
        return ["Schema YAML not found. Validation skipped."]

    type_schemas = schema.get("types", {})
    relation_schemas = schema.get("relations", {})
    global_constraints = schema.get("constraints", [])

    conn = get_db(db_path)
    entities = {
        row["id"]: row_to_entity(row)
        for row in conn.execute("SELECT * FROM entities")
    }
    relations_raw = conn.execute("SELECT * FROM relations").fetchall()
    relations = [
        {"from": r["from_id"], "rel": r["rel_type"], "to": r["to_id"]}
        for r in relations_raw
    ]

    # Entity validation
    for entity_id, entity in entities.items():
        type_name = entity["type"]
        type_schema = type_schemas.get(type_name, {})

        required = type_schema.get("required", [])
        for prop in required:
            if prop not in entity["properties"]:
                errors.append(f"{entity_id}: missing required property '{prop}'")

        forbidden = type_schema.get("forbidden_properties", [])
        for prop in forbidden:
            if prop in entity["properties"]:
                errors.append(f"{entity_id}: contains forbidden property '{prop}'")

        for prop, allowed in type_schema.items():
            if prop.endswith("_enum"):
                field = prop.replace("_enum", "")
                value = entity["properties"].get(field)
                if value and value not in allowed:
                    errors.append(
                        f"{entity_id}: '{field}' must be one of {allowed}, got '{value}'"
                    )

    # Relation validation
    rel_index: dict[str, list] = {}
    for rel in relations:
        rel_index.setdefault(rel["rel"], []).append(rel)

    for rel_type, rel_schema in relation_schemas.items():
        rels = rel_index.get(rel_type, [])
        from_types = rel_schema.get("from_types", [])
        to_types = rel_schema.get("to_types", [])
        cardinality = rel_schema.get("cardinality")
        acyclic = rel_schema.get("acyclic", False)

        for rel in rels:
            from_entity = entities.get(rel["from"])
            to_entity = entities.get(rel["to"])
            if not from_entity or not to_entity:
                continue
            if from_types and from_entity["type"] not in from_types:
                errors.append(
                    f"{rel_type}: from entity {rel['from']} type "
                    f"{from_entity['type']} not in {from_types}"
                )
            if to_types and to_entity["type"] not in to_types:
                errors.append(
                    f"{rel_type}: to entity {rel['to']} type "
                    f"{to_entity['type']} not in {to_types}"
                )

        if cardinality in ("one_to_one", "one_to_many", "many_to_one"):
            from_counts: dict[str, int] = {}
            to_counts: dict[str, int] = {}
            for rel in rels:
                from_counts[rel["from"]] = from_counts.get(rel["from"], 0) + 1
                to_counts[rel["to"]] = to_counts.get(rel["to"], 0) + 1

            if cardinality in ("one_to_one", "many_to_one"):
                for fid, count in from_counts.items():
                    if count > 1:
                        errors.append(
                            f"{rel_type}: from entity {fid} violates "
                            f"cardinality {cardinality}"
                        )
            if cardinality in ("one_to_one", "one_to_many"):
                for tid, count in to_counts.items():
                    if count > 1:
                        errors.append(
                            f"{rel_type}: to entity {tid} violates "
                            f"cardinality {cardinality}"
                        )

        if acyclic:
            graph: dict[str, list[str]] = {}
            for rel in rels:
                graph.setdefault(rel["from"], []).append(rel["to"])

            visited: dict[str, bool] = {}

            def dfs(node: str, stack: set) -> bool:
                visited[node] = True
                stack.add(node)
                for nxt in graph.get(node, []):
                    if nxt in stack:
                        return True
                    if not visited.get(nxt, False) and dfs(nxt, stack):
                        return True
                stack.remove(node)
                return False

            for node in graph:
                if not visited.get(node, False) and dfs(node, set()):
                    errors.append(f"{rel_type}: cyclic dependency detected")
                    break

    # Global constraints
    for constraint in global_constraints:
        ctype = constraint.get("type")
        rule = (constraint.get("rule") or "").strip().lower()
        if ctype == "Event" and "end" in rule and "start" in rule:
            for entity_id, entity in entities.items():
                if (
                    entity["type"] == "Event"
                    and "start" in entity["properties"]
                    and "end" in entity["properties"]
                ):
                    start = entity["properties"]["start"]
                    end = entity["properties"]["end"]
                    try:
                        if datetime.fromisoformat(end) < datetime.fromisoformat(
                            start
                        ):
                            errors.append(f"{entity_id}: end must be >= start")
                    except ValueError:
                        errors.append(f"{entity_id}: invalid datetime format")

    return errors


def write_schema(schema_path: str, schema: dict) -> None:
    schema_file = Path(schema_path)
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    with open(schema_file, "w") as f:
        yaml.safe_dump(schema, f, sort_keys=False)


def merge_schema(base: dict, incoming: dict) -> dict:
    for key, value in (incoming or {}).items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            base[key] = merge_schema(base[key], value)
        elif key in base and isinstance(base[key], list) and isinstance(value, list):
            base[key] = base[key] + [v for v in value if v not in base[key]]
        else:
            base[key] = value
    return base


def append_schema(schema_path: str, incoming: dict) -> dict:
    base = load_schema(schema_path)
    merged = merge_schema(base, incoming)
    write_schema(schema_path, merged)
    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Ontology graph operations via SQLite"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    speaker_parser = argparse.ArgumentParser(add_help=False)
    speaker_parser.add_argument(
        "--speaker", "-s", help="Speaker ID for ACL filtering"
    )

    create_p = subparsers.add_parser("create", help="Create entity")
    create_p.add_argument("--type", "-t", required=True)
    create_p.add_argument("--props", "-p", default="{}")
    create_p.add_argument("--id")
    create_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    get_p = subparsers.add_parser(
        "get", help="Get entity by ID", parents=[speaker_parser]
    )
    get_p.add_argument("--id", required=True)
    get_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    query_p = subparsers.add_parser(
        "query", help="Query entities", parents=[speaker_parser]
    )
    query_p.add_argument("--type", "-t")
    query_p.add_argument("--where", "-w", default="{}")
    query_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    list_p = subparsers.add_parser(
        "list", help="List entities", parents=[speaker_parser]
    )
    list_p.add_argument("--type", "-t")
    list_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    update_p = subparsers.add_parser("update", help="Update entity")
    update_p.add_argument("--id", required=True)
    update_p.add_argument("--props", "-p", required=True)
    update_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    delete_p = subparsers.add_parser("delete", help="Delete entity")
    delete_p.add_argument("--id", required=True)
    delete_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    relate_p = subparsers.add_parser("relate", help="Create relation")
    relate_p.add_argument("--from", dest="from_id", required=True)
    relate_p.add_argument("--rel", "-r", required=True)
    relate_p.add_argument("--to", dest="to_id", required=True)
    relate_p.add_argument("--props", "-p", default="{}")
    relate_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    related_p = subparsers.add_parser(
        "related", help="Get related entities", parents=[speaker_parser]
    )
    related_p.add_argument("--id", required=True)
    related_p.add_argument("--rel", "-r")
    related_p.add_argument(
        "--dir", choices=["outgoing", "incoming", "both"], default="outgoing"
    )
    related_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    validate_p = subparsers.add_parser("validate", help="Validate graph")
    validate_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)
    validate_p.add_argument("--schema", default=DEFAULT_SCHEMA_PATH)

    schema_p = subparsers.add_parser("schema-append")
    schema_p.add_argument("--schema", default=DEFAULT_SCHEMA_PATH)
    schema_p.add_argument("--data")
    schema_p.add_argument("--file", "-f")

    args = parser.parse_args()
    workspace_root = Path.cwd().resolve()

    if hasattr(args, "db"):
        args.db = str(
            resolve_safe_path(args.db, root=workspace_root, label="db path")
        )
    if hasattr(args, "schema"):
        args.schema = str(
            resolve_safe_path(args.schema, root=workspace_root, label="schema path")
        )
    if hasattr(args, "file") and args.file:
        args.file = str(
            resolve_safe_path(
                args.file, root=workspace_root, must_exist=True, label="schema file"
            )
        )

    if args.command == "create":
        props = json.loads(args.props)
        entity = create_entity(args.type, props, args.db, args.id)
        print(json.dumps(entity, indent=2))
    elif args.command == "get":
        entity = get_entity(args.id, args.db, args.speaker)
        if entity:
            print(json.dumps(entity, indent=2))
        else:
            print(f"Entity not found or access denied: {args.id}")
    elif args.command == "query":
        where = json.loads(args.where)
        results = query_entities(args.type, where, args.db, args.speaker)
        print(json.dumps(results, indent=2))
    elif args.command == "list":
        results = list_entities(args.type, args.db, args.speaker)
        print(json.dumps(results, indent=2))
    elif args.command == "update":
        props = json.loads(args.props)
        entity = update_entity(args.id, props, args.db)
        if entity:
            print(json.dumps(entity, indent=2))
        else:
            print(f"Entity not found: {args.id}")
    elif args.command == "delete":
        if delete_entity(args.id, args.db):
            print(f"Deleted: {args.id}")
        else:
            print(f"Entity not found: {args.id}")
    elif args.command == "relate":
        props = json.loads(args.props)
        rel = create_relation(args.from_id, args.rel, args.to_id, props, args.db)
        print(json.dumps(rel, indent=2))
    elif args.command == "related":
        results = get_related(args.id, args.rel, args.db, args.dir, args.speaker)
        print(json.dumps(results, indent=2))
    elif args.command == "validate":
        errors = validate_graph(args.db, args.schema)
        if errors:
            print("Validation errors:")
            for err in errors:
                print(f"  - {err}")
        else:
            print("Graph is valid.")
    elif args.command == "schema-append":
        if not args.data and not args.file:
            raise SystemExit("schema-append requires --data or --file")
        if args.data:
            incoming = json.loads(args.data)
        else:
            if Path(args.file).suffix.lower() == ".json":
                incoming = json.load(open(args.file))
            else:
                import yaml

                incoming = yaml.safe_load(open(args.file))
        merged = append_schema(args.schema, incoming)
        print(json.dumps(merged, indent=2))


if __name__ == "__main__":
    main()
