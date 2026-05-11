#!/usr/bin/env python3
"""
JARVIS Log Agent — lightweight log collector that tails Docker containers,
systemd units, and plain files, then forwards structured entries to the
JARVIS orchestrator log-collector endpoint.

Zero external dependencies beyond Python 3.10+ stdlib.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
import signal
import ssl
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # fallback to JSON config

LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")
LEVEL_ALIASES = {
    "WARNING": "WARN",
    "CRITICAL": "FATAL",
    "CRIT": "FATAL",
    "ERR": "ERROR",
    "NOTICE": "INFO",
    "EMERG": "FATAL",
    "ALERT": "FATAL",
}

# Regex patterns for level extraction from various log formats
LEVEL_PATTERNS = [
    # Python logging: 2026-04-22 14:30:00,123 - name - LEVEL - msg
    re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL|FATAL)\b"),
    # systemd/journalctl priority: <N> or prio=N
    re.compile(r"<([0-7])>"),
    # nginx: [error], [warn], [notice], [info]
    re.compile(r"\[(emerg|alert|crit|error|warn|notice|info|debug)\]", re.IGNORECASE),
    # generic bracket: [ERROR], [WARN], etc.
    re.compile(r"\[(DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|FATAL|CRIT(?:ICAL)?)\]", re.IGNORECASE),
]

# Syslog numeric priority to level
SYSLOG_PRIO = {
    "0": "FATAL", "1": "FATAL", "2": "FATAL",  # emerg, alert, crit
    "3": "ERROR", "4": "WARN", "5": "INFO",     # err, warning, notice
    "6": "INFO", "7": "DEBUG",                    # info, debug
}

logger = logging.getLogger("jarvis-log-agent")


@dataclass
class LogEntry:
    ts: str
    host: str
    service: str
    level: str
    msg: str
    src: str  # "docker" | "journalctl" | "file"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceConfig:
    """One log source to tail."""
    name: str
    type: str  # "docker" | "journalctl" | "file"
    # docker: container name/id
    # journalctl: unit name(s) or empty for all
    # file: path to file
    target: str
    level_filter: str = "DEBUG"  # minimum level to forward
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    host_id: str
    collector_url: str  # e.g. http://100.88.84.81:5000/api/admin/logs/ingest
    auth_token: str
    sources: list[SourceConfig]
    batch_interval: float = 2.0   # seconds between flushes
    batch_size: int = 100         # max entries per batch
    retry_interval: float = 10.0  # seconds between retries on failure


def load_config(path: str) -> AgentConfig:
    """Load config from YAML or JSON file."""
    raw = Path(path).read_text()
    if path.endswith((".yml", ".yaml")):
        if yaml is None:
            raise RuntimeError("PyYAML not installed — use JSON config or pip install pyyaml")
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)

    sources = []
    for s in data.get("sources", []):
        sources.append(SourceConfig(
            name=s["name"],
            type=s["type"],
            target=s["target"],
            level_filter=s.get("level_filter", "DEBUG").upper(),
            extra_args=s.get("extra_args", []),
        ))

    return AgentConfig(
        host_id=data["host_id"],
        collector_url=data["collector_url"],
        auth_token=data.get("auth_token", os.getenv("LOG_AGENT_TOKEN", "")),
        sources=sources,
        batch_interval=data.get("batch_interval", 2.0),
        batch_size=data.get("batch_size", 100),
        retry_interval=data.get("retry_interval", 10.0),
    )


# ---------------------------------------------------------------------------
# Level parsing
# ---------------------------------------------------------------------------
def parse_level(line: str) -> str:
    """Extract log level from a line. Returns 'INFO' if not detected."""
    for pat in LEVEL_PATTERNS:
        m = pat.search(line)
        if m:
            raw = m.group(1).upper()
            if raw in SYSLOG_PRIO:
                return SYSLOG_PRIO[raw]
            return LEVEL_ALIASES.get(raw, raw)
    return "INFO"


def level_gte(level: str, minimum: str) -> bool:
    """Check if level >= minimum."""
    try:
        return LOG_LEVELS.index(level) >= LOG_LEVELS.index(minimum)
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Tail processes
# ---------------------------------------------------------------------------
class TailProcess:
    """Manages an async subprocess that tails a log source."""

    def __init__(self, source: SourceConfig, host_id: str, queue: asyncio.Queue):
        self.source = source
        self.host_id = host_id
        self.queue = queue
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._running = True

    def _build_cmd(self) -> list[str]:
        s = self.source
        if s.type == "docker":
            cmd = ["docker", "logs", "--follow", "--tail", "0", "--timestamps"]
            cmd.extend(s.extra_args)
            cmd.append(s.target)
            return cmd
        elif s.type in ("journalctl", "journalctl-user"):
            cmd = ["journalctl", "--follow", "--no-pager", "--output", "short-iso", "-n", "0"]
            if s.type == "journalctl-user":
                cmd.append("--user")
            if s.target:
                unit_flag = "--user-unit" if s.type == "journalctl-user" else "-u"
                for unit in s.target.split(","):
                    cmd.extend([unit_flag, unit.strip()])
            cmd.extend(s.extra_args)
            return cmd
        elif s.type == "file":
            cmd = ["tail", "-F", "-n", "0"]
            cmd.extend(s.extra_args)
            cmd.append(s.target)
            return cmd
        else:
            raise ValueError(f"Unknown source type: {s.type}")

    async def start(self):
        while self._running:
            cmd = self._build_cmd()
            logger.info(f"[{self.source.name}] starting: {' '.join(cmd)}")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                await self._read_lines()
            except FileNotFoundError:
                logger.error(f"[{self.source.name}] command not found: {cmd[0]}")
            except Exception as e:
                logger.error(f"[{self.source.name}] error: {e}")

            if self._running:
                logger.warning(f"[{self.source.name}] process exited, restarting in 5s")
                await asyncio.sleep(5)

    async def _read_lines(self):
        assert self.proc and self.proc.stdout
        while self._running:
            line_bytes = await self.proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue

            level = parse_level(line)
            if not level_gte(level, self.source.level_filter):
                continue

            entry = LogEntry(
                ts=now_iso(),
                host=self.host_id,
                service=self.source.name,
                level=level,
                msg=line,
                src=self.source.type,
            )
            try:
                self.queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # drop oldest-style: queue is bounded

    def stop(self):
        self._running = False
        if self.proc:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------------------
# HTTP forwarder (stdlib only, no aiohttp/requests)
# ---------------------------------------------------------------------------
class LogForwarder:
    """Batches log entries and POSTs them to the collector."""

    def __init__(self, config: AgentConfig, queue: asyncio.Queue):
        self.config = config
        self.queue = queue
        self._running = True
        self._consecutive_failures = 0

    async def run(self):
        while self._running:
            batch: list[dict] = []
            deadline = time.monotonic() + self.config.batch_interval

            # Drain queue up to batch_size or timeout
            while len(batch) < self.config.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    entry: LogEntry = await asyncio.wait_for(
                        self.queue.get(), timeout=remaining
                    )
                    batch.append(entry.to_dict())
                except asyncio.TimeoutError:
                    break

            if batch:
                await self._send(batch)

    async def _send(self, batch: list[dict]):
        payload = json.dumps({"entries": batch}).encode("utf-8")

        req = Request(
            self.config.collector_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.auth_token}",
                "X-Host-Id": self.config.host_id,
            },
            method="POST",
        )

        # Run blocking HTTP in thread pool
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._do_request, req)
            if self._consecutive_failures > 0:
                logger.info(f"Collector reachable again after {self._consecutive_failures} failures")
            self._consecutive_failures = 0
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures <= 3 or self._consecutive_failures % 30 == 0:
                logger.error(f"Forward failed ({self._consecutive_failures}x): {e}")
            # back off
            await asyncio.sleep(min(self.config.retry_interval * self._consecutive_failures, 60))

    def _do_request(self, req: Request):
        ctx = ssl.create_default_context()
        try:
            with urlopen(req, timeout=10, context=ctx) as resp:
                resp.read()
        except URLError as e:
            raise ConnectionError(f"{self.config.collector_url}: {e}") from e

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(config_path: str):
    config = load_config(config_path)
    logger.info(f"Log agent starting — host={config.host_id}, sources={len(config.sources)}, "
                f"collector={config.collector_url}")

    queue: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=10000)
    tailers: list[TailProcess] = []
    tasks: list[asyncio.Task] = []

    # Start tailers
    for source in config.sources:
        tailer = TailProcess(source, config.host_id, queue)
        tailers.append(tailer)
        tasks.append(asyncio.create_task(tailer.start()))

    # Start forwarder
    forwarder = LogForwarder(config, queue)
    tasks.append(asyncio.create_task(forwarder.run()))

    # Wait for shutdown signal
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    # Cleanup
    logger.info("Stopping tailers...")
    for t in tailers:
        t.stop()
    forwarder.stop()

    # Give tasks a moment to finish
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Agent stopped.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    if not Path(config_file).exists():
        logger.error(f"Config file not found: {config_file}")
        sys.exit(1)

    asyncio.run(main(config_file))
