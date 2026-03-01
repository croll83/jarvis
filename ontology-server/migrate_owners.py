#!/usr/bin/env python3
"""One-time migration: normalize owners to entity IDs and add speaker_id to Persons."""

import json
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "data/graph.db"

# Person speaker_id assignments
SPEAKER_IDS = {
    "pers_137ac654": "marco",
    "pers_b2e2a84a": "jarvis-agent",
    "pers_d795b5ed": "agent-trading",
    "pers_e7815c45": "ada",
    "pers_c890b6b0": "giorgio",
    "pers_43beac7c": "sofia",
    "pers_51f5af05": "martina",
    "pers_fdfa5203": "nonno-giorgio",
    "pers_45b72885": "nonna-loredana",
    "pers_b1692652": "susetta",
    "pers_098e8d66": "simona",
    "pers_7c4244ba": "rosaria",
    "pers_9180957a": "imelda",
}

# Owner string → Person entity ID mapping
OWNER_TO_ENTITY = {
    "marco": "pers_137ac654",
    "jarvis-agent": "pers_b2e2a84a",
}

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()

print("=== Owner Migration ===\n")

# 1. Person entities: self-ownership + speaker_id
print("--- Person entities ---")
rows = conn.execute("SELECT id, properties FROM entities WHERE type = 'Person'").fetchall()
for row in rows:
    eid = row["id"]
    props = json.loads(row["properties"])
    old_owner = props.get("owner", "")
    changes = []

    # Set owner = self
    if props.get("owner") != eid:
        props["owner"] = eid
        changes.append(f"owner: {old_owner!r} → {eid!r}")

    # Set speaker_id
    if eid in SPEAKER_IDS and props.get("speaker_id") != SPEAKER_IDS[eid]:
        props["speaker_id"] = SPEAKER_IDS[eid]
        changes.append(f"speaker_id: {SPEAKER_IDS[eid]!r}")

    if changes:
        conn.execute(
            "UPDATE entities SET properties = ?, updated = ? WHERE id = ?",
            (json.dumps(props), now, eid),
        )
        name = props.get("name", eid)
        print(f"  {name} ({eid}): {', '.join(changes)}")
    else:
        print(f"  {props.get('name', eid)} ({eid}): no changes needed")

# 2. Non-Person entities: normalize owner strings to entity IDs
print("\n--- Non-Person entities ---")
for old_owner, new_owner in OWNER_TO_ENTITY.items():
    cursor = conn.execute(
        """SELECT id, type, properties FROM entities
           WHERE type != 'Person'
             AND json_extract(properties, '$.owner') = ?""",
        (old_owner,),
    )
    rows = cursor.fetchall()
    count = 0
    for row in rows:
        props = json.loads(row["properties"])
        props["owner"] = new_owner
        conn.execute(
            "UPDATE entities SET properties = ?, updated = ? WHERE id = ?",
            (json.dumps(props), now, row["id"]),
        )
        count += 1
    if count:
        print(f"  owner {old_owner!r} → {new_owner!r}: {count} entities updated")

conn.commit()
print("\nDone! Migration complete.")
conn.close()
