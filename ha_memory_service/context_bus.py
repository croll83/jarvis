"""Redis-based context bus for cross-system short-term memory.

Shared between orchestrator, HA memory service, and Hermes agent.
Each system writes events with its own source tag and reads only
events from OTHER sources (no self-duplication).

Events are stored per-user as a capped list with TTL.
Structure: ctx:{user_id}:events → list of JSON events (max 20, TTL 30 min)

Usage:
    bus = ContextBus(source="orchestrator")
    bus.push("user1", text="accendi la luce", room="soggiorno",
             entities=["light.soggiorno"], result="luce accesa")
    events = bus.get_recent("user1", limit=5)  # excludes own source
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EVENT_TTL = 1800  # 30 minutes
_MAX_EVENTS = 20

# Speaker ID → mem0 user_id mapping
_SPEAKER_USER_MAP: Dict[str, str] = {}


def _get_speaker_map() -> Dict[str, str]:
    """Load speaker → user mapping from env or config."""
    global _SPEAKER_USER_MAP
    if not _SPEAKER_USER_MAP:
        raw = os.environ.get("SPEAKER_USER_MAP", "")
        if raw:
            # Format: "1:marco,2:ada,3:shared"
            for pair in raw.split(","):
                if ":" in pair:
                    sid, uid = pair.strip().split(":", 1)
                    _SPEAKER_USER_MAP[sid.strip()] = uid.strip()
    return _SPEAKER_USER_MAP


def speaker_to_user_id(speaker_id: Optional[int], speaker_name: Optional[str] = None) -> str:
    """Map speaker_id to mem0 user_id via SPEAKER_USER_MAP env var. Falls back to 'shared'."""
    smap = _get_speaker_map()
    if speaker_id is not None:
        uid = smap.get(str(speaker_id))
        if uid:
            return uid
    if speaker_name:
        name_lower = speaker_name.lower()
        # Check if any mapped user_id matches the speaker name
        if name_lower in smap.values():
            return name_lower
    return "shared"


class ContextBus:
    """Redis-backed context bus for short-term cross-system memory."""

    def __init__(self, source: str, redis_url: str = None):
        self.source = source
        self._redis_url = redis_url or os.environ.get("REDIS_URL")
        assert self._redis_url, "REDIS_URL environment variable is required"
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import redis
                self._client = redis.from_url(self._redis_url, decode_responses=True)
                self._client.ping()
            except Exception as e:
                logger.warning("Redis connection failed: %s", e)
                self._client = None
        return self._client

    def push(
        self,
        user_id: str,
        *,
        text: str,
        room: str = "",
        event_type: str = "command",
        entities: List[str] = None,
        result: str = "",
    ) -> bool:
        """Push an event to the context bus for a user.

        Args:
            user_id: mem0 user ID (marco, ada, shared)
            text: what happened (command text, state change description)
            room: location/room context
            event_type: command | response | state_change
            entities: HA entity IDs involved
            result: outcome of the action
        """
        client = self._get_client()
        if not client:
            return False

        key = f"ctx:{user_id}:events"
        event = {
            "ts": time.time(),
            "source": self.source,
            "text": text,
            "type": event_type,
        }
        if room:
            event["room"] = room
        if entities:
            event["entities"] = entities
        if result:
            event["result"] = result

        try:
            pipe = client.pipeline()
            pipe.lpush(key, json.dumps(event, ensure_ascii=False))
            pipe.ltrim(key, 0, _MAX_EVENTS - 1)
            pipe.expire(key, _EVENT_TTL)
            pipe.execute()
            return True
        except Exception as e:
            logger.debug("Redis push failed: %s", e)
            self._client = None
            return False

    def get_recent(
        self,
        user_id: str,
        limit: int = 5,
        exclude_source: str = None,
    ) -> List[Dict[str, Any]]:
        """Get recent events for a user, excluding own source.

        Args:
            user_id: mem0 user ID
            limit: max events to return (default 5)
            exclude_source: source to exclude (defaults to self.source)
        """
        client = self._get_client()
        if not client:
            return []

        exclude = exclude_source or self.source
        key = f"ctx:{user_id}:events"

        try:
            # Read more than limit to account for filtered-out events
            raw_events = client.lrange(key, 0, _MAX_EVENTS - 1)
            events = []
            for raw in raw_events:
                try:
                    evt = json.loads(raw)
                    if evt.get("source") != exclude:
                        events.append(evt)
                        if len(events) >= limit:
                            break
                except json.JSONDecodeError:
                    continue
            return events
        except Exception as e:
            logger.debug("Redis get failed: %s", e)
            self._client = None
            return []

    def format_for_context(self, user_id: str, limit: int = 5) -> str:
        """Get recent external events formatted for LLM context injection.

        Returns empty string if no relevant events.
        """
        events = self.get_recent(user_id, limit=limit)
        if not events:
            return ""

        lines = []
        now = time.time()
        for evt in events:
            age_sec = now - evt.get("ts", now)
            if age_sec < 60:
                age_str = f"{int(age_sec)}s fa"
            else:
                age_str = f"{int(age_sec / 60)}min fa"

            parts = [f"({evt.get('source', '?')})"]
            if evt.get("room"):
                parts.append(f"@{evt['room']}")
            parts.append(evt.get("text", ""))
            if evt.get("result"):
                parts.append(f"→ {evt['result']}")

            lines.append(f"- {age_str} {' '.join(parts)}")

        return "\n".join(lines)

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
