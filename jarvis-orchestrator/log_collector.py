"""
JARVIS Log Collector
- Receives log entries from remote log-agents via HTTP
- Pulls logs from HAOS instances via Supervisor API
- Stores in SQLite ring buffer (separate logs.db)
- Publishes to SSE for real-time dashboard streaming
- Provides query API with filtering
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
from auth_api import require_admin
from event_bus import event_bus

logger = logging.getLogger("JARVIS_LOGS")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LOG_DB_PATH = Path(os.getenv(
    "LOG_DB_PATH",
    str(Path(config.BASE_DIR).parent / "data" / "logs.db")
))
LOG_RETENTION_HOURS = int(os.getenv("LOG_RETENTION_HOURS", "72"))
LOG_AGENT_TOKEN = os.getenv("LOG_AGENT_TOKEN", "")
LOG_PRUNE_INTERVAL = 300  # seconds between prune runs

# HAOS instances to pull logs from (JSON: {"name": {"url": "...", "token": "..."}})
HAOS_INSTANCES_RAW = os.getenv("HAOS_INSTANCES", "")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def _get_log_conn() -> sqlite3.Connection:
    LOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LOG_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_log_db():
    """Create logs table if not exists."""
    conn = _get_log_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                host TEXT NOT NULL,
                service TEXT NOT NULL,
                level TEXT NOT NULL,
                msg TEXT NOT NULL,
                src TEXT NOT NULL DEFAULT 'agent'
            );
            CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
            CREATE INDEX IF NOT EXISTS idx_logs_host ON logs(host);
            CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service);
            CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
            CREATE INDEX IF NOT EXISTS idx_logs_host_service ON logs(host, service);
        """)
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Log DB initialized at {LOG_DB_PATH}")


def insert_log_batch(entries: list[dict]):
    """Insert a batch of log entries."""
    if not entries:
        return
    conn = _get_log_conn()
    try:
        conn.executemany(
            "INSERT INTO logs (ts, host, service, level, msg, src) VALUES (?, ?, ?, ?, ?, ?)",
            [(e["ts"], e["host"], e["service"], e["level"], e["msg"], e.get("src", "agent")) for e in entries]
        )
        conn.commit()
    finally:
        conn.close()


def query_logs(
    hosts: Optional[list[str]] = None,
    services: Optional[list[str]] = None,
    levels: Optional[list[str]] = None,
    search: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Query logs with filters."""
    conn = _get_log_conn()
    try:
        clauses = []
        params = []

        if hosts:
            placeholders = ",".join("?" * len(hosts))
            clauses.append(f"host IN ({placeholders})")
            params.extend(hosts)
        if services:
            placeholders = ",".join("?" * len(services))
            clauses.append(f"service IN ({placeholders})")
            params.extend(services)
        if levels:
            placeholders = ",".join("?" * len(levels))
            clauses.append(f"level IN ({placeholders})")
            params.extend(levels)
        if search:
            clauses.append("msg LIKE ?")
            params.append(f"%{search}%")
        if before:
            clauses.append("ts < ?")
            params.append(before)
        if after:
            clauses.append("ts > ?")
            params.append(after)

        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT id, ts, host, service, level, msg, src FROM logs WHERE {where} ORDER BY ts DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_log_sources() -> dict:
    """Get list of known hosts and services."""
    conn = _get_log_conn()
    try:
        hosts = [r[0] for r in conn.execute("SELECT DISTINCT host FROM logs ORDER BY host").fetchall()]
        services = [
            dict(r) for r in conn.execute(
                "SELECT DISTINCT host, service FROM logs ORDER BY host, service"
            ).fetchall()
        ]
        return {"hosts": hosts, "services": services}
    finally:
        conn.close()


def get_log_stats() -> dict:
    """Get log statistics."""
    conn = _get_log_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        by_level = {
            r[0]: r[1] for r in conn.execute(
                "SELECT level, COUNT(*) FROM logs GROUP BY level"
            ).fetchall()
        }
        by_host = {
            r[0]: r[1] for r in conn.execute(
                "SELECT host, COUNT(*) FROM logs GROUP BY host"
            ).fetchall()
        }
        oldest = conn.execute("SELECT MIN(ts) FROM logs").fetchone()[0]
        newest = conn.execute("SELECT MAX(ts) FROM logs").fetchone()[0]
        return {
            "total": total,
            "by_level": by_level,
            "by_host": by_host,
            "oldest": oldest,
            "newest": newest,
            "retention_hours": LOG_RETENTION_HOURS,
        }
    finally:
        conn.close()


def prune_old_logs():
    """Delete logs older than retention period."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOG_RETENTION_HOURS)).isoformat()
    conn = _get_log_conn()
    try:
        result = conn.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        deleted = result.rowcount
        if deleted > 0:
            conn.commit()
            conn.execute("PRAGMA optimize")
            logger.info(f"Pruned {deleted} log entries older than {LOG_RETENTION_HOURS}h")
        return deleted
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HAOS Log Puller
# ---------------------------------------------------------------------------
HAOS_LOG_ENDPOINTS = [
    ("core", "/api/hassio/core/logs"),
    ("supervisor", "/api/hassio/supervisor/logs"),
    ("host", "/api/hassio/host/logs"),
]

# Parse systemd journal lines: "2026-04-22 14:30:00.123 hostname tag[pid]: message"
JOURNAL_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*)\s+\S+\s+(\S+?)(?:\[\d+\])?:\s+(.*)"
)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

LEVEL_KEYWORDS = {
    "ERROR": "ERROR", "WARNING": "WARN", "CRITICAL": "FATAL",
    "DEBUG": "DEBUG", "INFO": "INFO", "FATAL": "FATAL",
}

HAOS_LEVEL_ORDER = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")
HAOS_MIN_LEVEL = os.getenv("HAOS_MIN_LEVEL", "WARN")


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _haos_level_gte(level: str, minimum: str) -> bool:
    try:
        return HAOS_LEVEL_ORDER.index(level) >= HAOS_LEVEL_ORDER.index(minimum)
    except ValueError:
        return True


def _parse_haos_level(line: str) -> str:
    """Extract level from HAOS log line."""
    upper = line.upper()
    for kw, level in LEVEL_KEYWORDS.items():
        if kw in upper:
            return level
    return "INFO"


def _parse_haos_instances() -> dict:
    """Parse HAOS instances from env var."""
    if not HAOS_INSTANCES_RAW:
        return {}
    try:
        return json.loads(HAOS_INSTANCES_RAW)
    except json.JSONDecodeError:
        logger.error("Invalid HAOS_INSTANCES JSON")
        return {}


async def pull_haos_logs_once(
    name: str, url: str, token: str, endpoint_tag: str, endpoint_path: str,
    last_lines: dict,
) -> list[dict]:
    """Pull logs from one HAOS endpoint, returning only new lines."""
    entries = []
    full_url = f"{url.rstrip('/')}{endpoint_path}"
    headers = {
        "Authorization": f"Bearer {token}",
    }

    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(full_url, headers=headers, params={"lines": 100, "no_colors": "true"})
            if resp.status_code != 200:
                logger.warning(f"HAOS {name}/{endpoint_tag}: HTTP {resp.status_code}")
                return entries

            text = _strip_ansi(resp.text)
            lines = text.strip().split("\n") if text.strip() else []

            # Track what we've seen to avoid duplicates
            cache_key = f"{name}:{endpoint_tag}"
            prev_last = last_lines.get(cache_key, "")
            new_start = False

            for line in lines:
                if not line.strip():
                    continue
                if not new_start:
                    if line == prev_last:
                        new_start = True
                    continue

                level = _parse_haos_level(line)
                if _haos_level_gte(level, HAOS_MIN_LEVEL):
                    entries.append({
                        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                        "host": f"haos-{name}",
                        "service": f"ha-{endpoint_tag}",
                        "level": level,
                        "msg": line,
                        "src": "haos-api",
                    })

            # If we never found prev_last, all lines are new (first run or log rotated)
            if not new_start and not prev_last:
                # First run: don't emit historical lines, just record position
                pass
            elif not new_start:
                # prev_last not found (log rotated), emit all
                for line in lines:
                    if not line.strip():
                        continue
                    level = _parse_haos_level(line)
                    if not _haos_level_gte(level, HAOS_MIN_LEVEL):
                        continue
                    entries.append({
                        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                        "host": f"haos-{name}",
                        "service": f"ha-{endpoint_tag}",
                        "level": level,
                        "msg": line,
                        "src": "haos-api",
                    })

            if lines:
                last_lines[cache_key] = lines[-1]

    except Exception as e:
        logger.error(f"HAOS {name}/{endpoint_tag} pull error: {e}")

    return entries


async def haos_log_puller():
    """Background task: periodically pull logs from all HAOS instances."""
    instances = _parse_haos_instances()
    if not instances:
        logger.info("No HAOS instances configured, HAOS log puller disabled")
        return

    logger.info(f"HAOS log puller starting for {len(instances)} instance(s): {list(instances.keys())}")
    last_lines: dict[str, str] = {}

    while True:
        for name, cfg in instances.items():
            url = cfg.get("url", "")
            token = cfg.get("token", "")
            if not url or not token:
                continue

            all_entries = []
            for tag, path in HAOS_LOG_ENDPOINTS:
                entries = await pull_haos_logs_once(name, url, token, tag, path, last_lines)
                all_entries.extend(entries)

            if all_entries:
                insert_log_batch(all_entries)
                # Publish to SSE for live viewers
                for e in all_entries:
                    await event_bus.publish("log_entry", e)

        await asyncio.sleep(10)  # poll every 10s


# ---------------------------------------------------------------------------
# Periodic prune task
# ---------------------------------------------------------------------------
async def log_prune_task():
    """Background task to prune old logs."""
    while True:
        await asyncio.sleep(LOG_PRUNE_INTERVAL)
        try:
            prune_old_logs()
        except Exception as e:
            logger.error(f"Log prune error: {e}")


# ---------------------------------------------------------------------------
# API Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/admin/logs", tags=["logs"])


class LogBatch(BaseModel):
    entries: list[dict]


@router.post("/ingest")
async def ingest_logs(batch: LogBatch, request: Request):
    """Receive log entries from remote agents."""
    # Auth: either admin session or agent token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if LOG_AGENT_TOKEN and token != LOG_AGENT_TOKEN:
            # Try admin auth as fallback
            try:
                require_admin(request)
            except Exception:
                raise HTTPException(status_code=403, detail="Invalid token")
    elif LOG_AGENT_TOKEN:
        try:
            require_admin(request)
        except Exception:
            raise HTTPException(status_code=403, detail="Authentication required")

    entries = batch.entries
    if not entries:
        return {"accepted": 0}

    # Validate and sanitize
    valid = []
    for e in entries:
        if not all(k in e for k in ("ts", "host", "service", "level", "msg")):
            continue
        # Normalize level
        level = e["level"].upper()
        if level not in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL"):
            level = "INFO"
        valid.append({
            "ts": str(e["ts"])[:64],
            "host": str(e["host"])[:64],
            "service": str(e["service"])[:64],
            "level": level,
            "msg": str(e["msg"])[:4096],
            "src": str(e.get("src", "agent"))[:32],
        })

    insert_log_batch(valid)

    # Publish to SSE for live dashboard
    for e in valid:
        await event_bus.publish("log_entry", e)

    return {"accepted": len(valid)}


@router.get("")
async def get_logs(
    request: Request,
    host: Optional[str] = Query(None, description="Comma-separated host filter"),
    service: Optional[str] = Query(None, description="Comma-separated service filter"),
    level: Optional[str] = Query(None, description="Comma-separated level filter"),
    search: Optional[str] = Query(None, description="Text search in message"),
    before: Optional[str] = Query(None, description="ISO timestamp upper bound"),
    after: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """Query logs with filters. Requires admin auth."""
    require_admin(request)

    hosts = [h.strip() for h in host.split(",") if h.strip()] if host else None
    services = [s.strip() for s in service.split(",") if s.strip()] if service else None
    levels = [l.strip().upper() for l in level.split(",") if l.strip()] if level else None

    rows = query_logs(
        hosts=hosts,
        services=services,
        levels=levels,
        search=search,
        before=before,
        after=after,
        limit=limit,
        offset=offset,
    )
    return {"logs": rows, "count": len(rows)}


@router.get("/sources")
async def get_sources(request: Request):
    """Get list of known hosts and services."""
    require_admin(request)
    return get_log_sources()


@router.get("/stats")
async def get_stats(request: Request):
    """Get log statistics."""
    require_admin(request)
    return get_log_stats()


@router.get("/stream")
async def stream_logs(request: Request):
    """SSE endpoint for real-time log streaming."""
    require_admin(request)

    client = await event_bus.connect(user_id=None)

    async def event_generator():
        try:
            yield f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(client.queue.get(), timeout=30.0)
                    if message.get("type") == "log_entry":
                        yield f"event: log_entry\ndata: {json.dumps(message['data'])}\n\n"
                    # ignore other event types on this stream
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {json.dumps({'ts': time.time()})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await event_bus.disconnect(client)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("")
async def clear_logs(request: Request):
    """Clear all logs."""
    require_admin(request)
    conn = _get_log_conn()
    try:
        conn.execute("DELETE FROM logs")
        conn.commit()
        return {"cleared": True}
    finally:
        conn.close()
