#!/usr/bin/env python3
"""
ChromaDB Migration Script
=========================
Migrates data from embedded PersistentClient ChromaDB instances
to the shared ChromaDB server (HttpClient).

Can run from ANY machine — just point --source-path to local ChromaDB data
and --target-host to the remote ChromaDB server.

Sources:
  1. Orchestrator: ./data/chroma/ → collections: user_messages, user_facts
  2. HA Memory Service: /data/chroma/ → collections: location_events_*

Usage:
  === On LXC Jarvis (orchestrator data is local, ChromaDB server is local) ===
  python3 migrate_chromadb.py --dry-run
  python3 migrate_chromadb.py

  === On HAOS (HA memory data is local, ChromaDB server is remote) ===
  python3 migrate_chromadb.py \
    --source-path /data/chroma \
    --target-host 100.88.84.81

  === On LXC OpenClaw (no local data, just verify remote server) ===
  python3 migrate_chromadb.py \
    --target-host 100.88.84.81 \
    --verify-only

  === Migrate specific paths ===
  python3 migrate_chromadb.py \
    --source-path /opt/jarvis/data/chroma \
    --source-path /other/chroma/data \
    --target-host 100.88.84.81

Prerequisites:
  - ChromaDB server running at target host:port
  - pip install chromadb
"""

import argparse
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MIGRATE")

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    logger.error("chromadb not installed. Run: pip install chromadb")
    sys.exit(1)


def migrate_collection(
    source_client: chromadb.ClientAPI,
    target_client: chromadb.ClientAPI,
    collection_name: str,
    batch_size: int = 100,
    dry_run: bool = False,
) -> int:
    """Migrate a single collection from source to target."""
    try:
        source_col = source_client.get_collection(name=collection_name)
    except Exception as e:
        logger.warning(f"  Collection '{collection_name}' not found in source: {e}")
        return 0

    count = source_col.count()
    if count == 0:
        logger.info(f"  Collection '{collection_name}': empty, skipping")
        return 0

    logger.info(f"  Collection '{collection_name}': {count} documents")

    if dry_run:
        return count

    # Get or create target collection (without embedding function —
    # we'll copy the pre-computed embeddings directly)
    target_col = target_client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    existing_count = target_col.count()
    if existing_count > 0:
        logger.warning(f"  Target collection already has {existing_count} documents. Skipping duplicates via upsert.")

    # Migrate in batches
    migrated = 0
    offset = 0

    while offset < count:
        try:
            results = source_col.get(
                limit=batch_size,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )

            if not results["ids"]:
                break

            ids = results["ids"]
            documents = results["documents"] or [None] * len(ids)
            metadatas = results["metadatas"] or [None] * len(ids)
            embeddings = results["embeddings"]

            # Upsert to target (handles duplicates gracefully)
            upsert_kwargs = {
                "ids": ids,
                "documents": documents,
                "metadatas": metadatas,
            }
            if embeddings is not None:
                upsert_kwargs["embeddings"] = embeddings

            target_col.upsert(**upsert_kwargs)
            migrated += len(ids)
            offset += batch_size

            if migrated % 500 == 0:
                logger.info(f"    Migrated {migrated}/{count}...")

        except Exception as e:
            logger.error(f"    Error at offset {offset}: {e}")
            offset += batch_size
            continue

    logger.info(f"  Migrated {migrated} documents to '{collection_name}'")
    return migrated


def migrate_source(source_path: str, target, batch_size: int, dry_run: bool) -> int:
    """Migrate all collections from a local PersistentClient path."""
    abs_path = os.path.abspath(source_path)

    if not os.path.isdir(abs_path):
        logger.warning(f"Path does not exist: {abs_path} — skipping")
        return 0

    logger.info(f"\n--- Source: {abs_path} ---")
    try:
        source = chromadb.PersistentClient(
            path=abs_path,
            settings=Settings(anonymized_telemetry=False),
        )
        collections = source.list_collections()
        if not collections:
            logger.info("  No collections found.")
            return 0

        logger.info(f"Found {len(collections)} collections: {[c.name for c in collections]}")

        total = 0
        for col in collections:
            total += migrate_collection(
                source, target, col.name,
                batch_size=batch_size, dry_run=dry_run,
            )
        return total

    except Exception as e:
        logger.error(f"Error opening ChromaDB at {abs_path}: {e}")
        return 0


def verify_server(target):
    """Show all collections and counts on the target server."""
    logger.info("\n=== Server contents ===")
    collections = target.list_collections()
    if not collections:
        logger.info("  (empty — no collections)")
        return
    for col in collections:
        count = target.get_collection(col.name).count()
        logger.info(f"  {col.name}: {count} documents")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate ChromaDB from PersistentClient to shared server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # LXC Jarvis (local orchestrator data → local ChromaDB server):
  python3 migrate_chromadb.py

  # HAOS (local HA data → remote ChromaDB server on LXC Jarvis):
  python3 migrate_chromadb.py --source-path /data/chroma --target-host 100.88.84.81

  # Just verify what's on the server:
  python3 migrate_chromadb.py --verify-only --target-host 100.88.84.81
        """,
    )
    parser.add_argument("--source-path", action="append", default=None,
                        help="Local PersistentClient path(s) to migrate. Can be repeated. "
                             "If not specified, uses legacy defaults (orchestrator + ha).")
    parser.add_argument("--orchestrator-path", default="/opt/jarvis/data/chroma",
                        help="Legacy: orchestrator ChromaDB path (default: /opt/jarvis/data/chroma)")
    parser.add_argument("--ha-path", default=None,
                        help="Legacy: HA memory service ChromaDB path (optional)")
    parser.add_argument("--target-host", default="localhost",
                        help="ChromaDB server host (default: localhost)")
    parser.add_argument("--target-port", type=int, default=8000,
                        help="ChromaDB server port (default: 8000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be migrated without doing it")
    parser.add_argument("--verify-only", action="store_true",
                        help="Only check server contents, don't migrate anything")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Batch size for migration (default: 100)")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN MODE ===")

    # Connect to target server
    logger.info(f"Connecting to ChromaDB server at {args.target_host}:{args.target_port}...")
    try:
        target = chromadb.HttpClient(
            host=args.target_host,
            port=args.target_port,
            settings=Settings(anonymized_telemetry=False),
        )
        target.heartbeat()
        logger.info("Target server connected.")
    except Exception as e:
        logger.error(f"Cannot connect to ChromaDB server: {e}")
        logger.error("Make sure the server is running: docker compose up -d chromadb")
        sys.exit(1)

    if args.verify_only:
        verify_server(target)
        return

    total_migrated = 0

    if args.source_path:
        # New mode: explicit source paths
        for path in args.source_path:
            total_migrated += migrate_source(
                path, target, args.batch_size, args.dry_run,
            )
    else:
        # Legacy mode: orchestrator + ha paths (skip if not found)
        total_migrated += migrate_source(
            args.orchestrator_path, target, args.batch_size, args.dry_run,
        )
        if args.ha_path:
            total_migrated += migrate_source(
                args.ha_path, target, args.batch_size, args.dry_run,
            )

    # Summary
    action = "would be migrated" if args.dry_run else "migrated"
    logger.info(f"\n=== Total: {total_migrated} documents {action} ===")

    if not args.dry_run and total_migrated > 0:
        verify_server(target)
        logger.info("\nMigration complete!")


if __name__ == "__main__":
    main()
