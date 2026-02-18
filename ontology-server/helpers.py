#!/usr/bin/env python3
"""
Ontology Helpers: Complex queries, aggregations, and graph traversal.
Requires ontology.py in the same directory.

Usage from CLI:
    python helpers.py blocked-tasks --speaker user_ada
    python helpers.py workload --speaker user_ada
    python helpers.py project-summary --id proj_001 --speaker user_ada
    python helpers.py find-path --from p_001 --to task_001 --speaker user_ada
"""

import argparse
import json
from pathlib import Path

from ontology import (
    DEFAULT_DB_PATH,
    get_acl_clause,
    get_acl_params,
    get_db,
    get_related,
    query_entities,
)


def find_blocked_tasks(db_path: str, speaker: str | None, is_admin: bool = False) -> list:
    """Find all open tasks that are blocked by incomplete tasks."""
    open_tasks = query_entities("Task", {"status_enum": "open"}, db_path, speaker, is_admin=is_admin)
    blocked_results = []

    for task in open_tasks:
        blockers = get_related(task["id"], "blocks", db_path, "incoming", speaker, is_admin=is_admin)

        incomplete_blockers = []
        for b in blockers:
            status = b["entity"]["properties"].get("status_enum")
            if status not in ("done", "cancelled"):
                incomplete_blockers.append(b["entity"])

        if incomplete_blockers:
            blocked_results.append({"task": task, "blockers": incomplete_blockers})

    return blocked_results


def task_status_summary(
    project_id: str, db_path: str, speaker: str | None, is_admin: bool = False,
) -> dict:
    """Count tasks by status for a specific project."""
    tasks = get_related(project_id, "part_of", db_path, "incoming", speaker, is_admin=is_admin)

    summary: dict[str, int] = {}
    for t in tasks:
        if t["entity"]["type"] == "Task":
            status = t["entity"]["properties"].get("status_enum", "unknown")
            summary[status] = summary.get(status, 0) + 1

    return summary


def workload_by_person(db_path: str, speaker: str | None, is_admin: bool = False) -> dict:
    """Count open tasks per person using a direct SQLite JSON query."""
    acl = get_acl_clause(is_admin=is_admin)
    query = f"""
        SELECT json_extract(properties, '$.assignee') as assignee_id,
               COUNT(*) as count
        FROM entities
        WHERE type = 'Task'
          AND json_extract(properties, '$.status_enum') IN ('open', 'in_progress')
          AND {acl}
        GROUP BY assignee_id
    """

    workload: dict[str, int] = {}
    conn = get_db(db_path)
    params = get_acl_params(speaker, is_admin)
    for row in conn.execute(query, params):
        assignee = row["assignee_id"] or "unassigned"
        workload[assignee] = row["count"]

    return workload


def find_path(
    from_id: str,
    to_id: str,
    db_path: str,
    speaker: str | None,
    max_depth: int = 5,
    is_admin: bool = False,
) -> list:
    """Find the shortest path between two entities using BFS."""
    visited: set[str] = set()
    queue: list[tuple[str, list]] = [(from_id, [])]

    while queue:
        current, path = queue.pop(0)

        if current == to_id:
            return path

        if current in visited or len(path) >= max_depth:
            continue

        visited.add(current)

        neighbors = get_related(current, None, db_path, "both", speaker, is_admin=is_admin)

        for neighbor in neighbors:
            nxt = neighbor["entity"]["id"]
            if nxt not in visited:
                step = {
                    "relation": neighbor["relation"],
                    "direction": neighbor["direction"],
                    "entity_id": nxt,
                    "entity_type": neighbor["entity"]["type"],
                }
                queue.append((nxt, path + [step]))

    return []


def main():
    parser = argparse.ArgumentParser(description="Ontology Query Helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    speaker_parser = argparse.ArgumentParser(add_help=False)
    speaker_parser.add_argument(
        "--speaker", "-s", help="Speaker ID for ACL filtering"
    )

    bt_p = subparsers.add_parser(
        "blocked-tasks",
        help="Find open tasks blocked by incomplete ones",
        parents=[speaker_parser],
    )
    bt_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    wl_p = subparsers.add_parser(
        "workload",
        help="Get count of open/in_progress tasks per person",
        parents=[speaker_parser],
    )
    wl_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    ps_p = subparsers.add_parser(
        "project-summary",
        help="Get count of tasks by status for a project",
        parents=[speaker_parser],
    )
    ps_p.add_argument("--id", required=True, help="Project ID")
    ps_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    fp_p = subparsers.add_parser(
        "find-path",
        help="Find shortest path between two entities",
        parents=[speaker_parser],
    )
    fp_p.add_argument("--from", dest="from_id", required=True)
    fp_p.add_argument("--to", dest="to_id", required=True)
    fp_p.add_argument("--depth", type=int, default=5, help="Maximum search depth")
    fp_p.add_argument("--db", "-d", default=DEFAULT_DB_PATH)

    args = parser.parse_args()

    workspace_root = Path.cwd().resolve()
    db_path = (
        str((workspace_root / args.db).resolve())
        if not Path(args.db).is_absolute()
        else args.db
    )

    if args.command == "blocked-tasks":
        results = find_blocked_tasks(db_path, args.speaker)
        print(json.dumps(results, indent=2))

    elif args.command == "workload":
        results = workload_by_person(db_path, args.speaker)
        print(json.dumps(results, indent=2))

    elif args.command == "project-summary":
        results = task_status_summary(args.id, db_path, args.speaker)
        print(json.dumps(results, indent=2))

    elif args.command == "find-path":
        results = find_path(
            args.from_id, args.to_id, db_path, args.speaker, args.depth
        )
        if results:
            print(json.dumps(results, indent=2))
        else:
            print(
                f"No path found between {args.from_id} and {args.to_id} "
                f"within depth {args.depth}."
            )


if __name__ == "__main__":
    main()
