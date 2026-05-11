"""Mem0 self-hosted memory plugin — MemoryProvider interface.

Connects to a self-hosted Mem0 REST API (FastAPI server) backed by
ChromaDB (vector) and Kuzu (graph) for persistent memory.

v5: Adds ReasoningBank (procedural memory) — strategy induction from
    session transcripts + semantic retrieval at prefetch time.
    Based on: https://github.com/google-research/reasoning-bank (ICLR 2026)

    Memory layers managed by this plugin:
    - Layer 2 (Redis): short-term cross-system context bus
    - Layer 3 (Mem0): long-term declarative memory (facts, relations)
    - Layer 4 (ReasoningBank): long-term procedural memory (strategies)

Config via environment variables:
  MEM0_BASE_URL    — Mem0 server URL (default: http://localhost:8200)
  MEM0_USER_ID     — User identifier (default: hermes-user)
  REDIS_URL        — Redis context bus URL (default: redis://localhost:6379/0)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────────

_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120
_TIMEOUT = 30.0
_ADD_TIMEOUT = 120.0
_RB_INDUCTION_TIMEOUT = 180.0  # strategy induction can take longer
_REDIS_CTX_LIMIT = 5
_REDIS_CTX_TTL = 1800
_REDIS_MAX_EVENTS = 20

# ReasoningBank settings
_RB_COLLECTION = "rb_strategies"
_RB_MIN_SESSION_TURNS = 2       # skip induction for trivial sessions
_RB_MAX_STRATEGIES_PER_SESSION = 3
_RB_MAX_PREFETCH_RESULTS = 2
_RB_DEDUP_THRESHOLD = 0.85      # cosine similarity threshold for dedup
_RB_RELEVANCE_THRESHOLD = 0.75  # minimum score to inject in prefetch

# ─── ReasoningBank Induction Prompt ────────────────────────────────────────

REASONING_BANK_INDUCTION_PROMPT = """\
Sei un analizzatore di traiettorie di un agente AI. Il tuo compito è estrarre \
strategie generalizzabili da un transcript di sessione tra un utente e un assistente AI.

Analizza il transcript e identifica pattern che sarebbero utili in sessioni FUTURE \
con qualsiasi utente, non solo in questo caso specifico.

Per ogni strategia:
1. **text**: Descrivi COSA fare in modo generico (non il caso specifico di questa sessione)
2. **context**: Descrivi QUANDO applicarla (quale tipo di task, tool, o situazione la triggerebbe)
3. **signal**: Indica se deriva da un'azione riuscita ("success") o da un errore poi corretto ("failure_recovery")

Regole:
- Massimo {max_strategies} strategie per sessione
- Solo strategie VERAMENTE generalizzabili (applicabili oltre questa sessione)
- NON estrarre fatti dichiarativi (nomi, preferenze, account) — quelli vanno in un'altra memoria
- NON estrarre informazioni specifiche di infrastruttura (IP, porte, URL)
- Le strategie devono essere in italiano, concise (max 2 frasi ciascuna)
- Se non trovi strategie generalizzabili, restituisci una lista vuota

Esempi di buone strategie:
- "Quando si cerca sul web, verificare sempre la data dei risultati prima di presentarli come attuali" (context: "web_search tool usage")
- "Per il controllo domotico, risolvere sempre il nome dell'entità con entity_resolve prima di inviare il comando" (context: "home_control via orchestrator")
- "Se la connessione CDP al browser fallisce, verificare che il processo browser sia attivo prima di riprovare" (context: "browser automation / CDP")

Restituisci SOLO JSON valido nel formato:
{{"strategies": [{{"text": "...", "context": "...", "signal": "success|failure_recovery"}}]}}

Se non ci sono strategie generalizzabili:
{{"strategies": []}}
"""


class Mem0SelfHostedProvider(MemoryProvider):
    """Mem0 self-hosted memory with native pipeline (fact extraction + graph)
    and ReasoningBank (procedural memory from trajectory induction)."""

    def __init__(self):
        self._base_url = ""
        self._user_id = "hermes-user"
        self._client = None
        self._redis = None
        self._redis_available = False
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread = None
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        # ReasoningBank: ChromaDB direct client for rb_strategies collection
        self._chroma_client = None
        self._rb_collection = None
        self._rb_embed_url = ""
        self._rb_embed_model = ""
        self._rb_llm_url = ""
        self._rb_llm_model = ""
        # ReasoningBank: optional Anthropic-compatible induction backend.
        # When enabled, _rb_induce calls anthropic.messages.create against an
        # Anthropic-compatible proxy (default: in-house router on hermes:18801)
        # instead of the OpenAI-compatible local LLM. Better instruction
        # following → less banal/redundant strategies.
        self._rb_use_anthropic = False
        self._rb_anthropic_base_url = ""
        self._rb_anthropic_api_key = ""
        self._rb_anthropic_model = ""
        self._rb_anthropic_client = None

    @property
    def name(self) -> str:
        return "mem0-selfhosted"

    def is_available(self) -> bool:
        return bool(os.environ.get("MEM0_BASE_URL", ""))

    def save_config(self, values, hermes_home):
        pass

    def get_config_schema(self):
        return [
            {"key": "base_url", "description": "Mem0 server URL", "required": True,
             "env_var": "MEM0_BASE_URL", "default": "http://localhost:8200"},
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "redis_url", "description": "Redis context bus URL",
             "env_var": "REDIS_URL", "default": "redis://localhost:6379/0"},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._base_url = os.environ.get("MEM0_BASE_URL", "http://localhost:8200").rstrip("/")
        self._user_id = os.environ.get("MEM0_USER_ID", "hermes-user")

        # Use profile name as user_id if available
        identity = kwargs.get("agent_identity", "")
        if identity:
            short = identity.replace("hermes-", "") if identity.startswith("hermes-") else identity
            self._user_id = short

        self._client = httpx.Client(base_url=self._base_url, timeout=_TIMEOUT)

        # Initialize Redis context bus (best-effort)
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        try:
            import redis as redis_lib
            self._redis = redis_lib.from_url(redis_url, decode_responses=True)
            self._redis.ping()
            self._redis_available = True
            logger.info("Redis context bus connected: %s", redis_url)
        except Exception as e:
            self._redis_available = False
            logger.info("Redis context bus not available (ok, optional): %s", e)

        # Initialize ReasoningBank (ChromaDB direct + LLM config)
        self._rb_init()

    def _rb_init(self) -> None:
        """Initialize ReasoningBank: ChromaDB collection + LLM/embed endpoints."""
        # Read ChromaDB connection from mem0 server env (same instance)
        chroma_host = os.environ.get("CHROMA_HOST", "")
        chroma_port = os.environ.get("CHROMA_PORT", "8000")

        if not chroma_host:
            # Derive from MEM0_BASE_URL — ChromaDB is on same host
            from urllib.parse import urlparse
            parsed = urlparse(self._base_url)
            chroma_host = parsed.hostname or "localhost"

        self._rb_embed_url = os.environ.get(
            "EMBED_URL",
            os.environ.get("RB_EMBED_URL", f"http://{chroma_host}:11435/v1")
        )
        self._rb_embed_model = os.environ.get(
            "EMBED_MODEL",
            os.environ.get("RB_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
        )
        self._rb_llm_url = os.environ.get(
            "RB_LLM_URL",
            os.environ.get("LLM_URL", f"http://{chroma_host}:30000/v1")
        )
        self._rb_llm_model = os.environ.get(
            "RB_LLM_MODEL",
            os.environ.get("LLM_MODEL", "dark-opus")
        )

        # Optional: Anthropic-compatible induction backend (preferred — see
        # __init__ comment). Default ON, pointing at the in-house proxy.
        self._rb_use_anthropic = os.environ.get("RB_USE_ANTHROPIC", "1") == "1"
        self._rb_anthropic_base_url = os.environ.get(
            "RB_ANTHROPIC_BASE_URL", "http://100.116.99.9:18801"
        )
        self._rb_anthropic_api_key = os.environ.get("RB_ANTHROPIC_API_KEY", "dummy")
        self._rb_anthropic_model = os.environ.get(
            "RB_ANTHROPIC_MODEL", "claude-sonnet-4-5"
        )
        if self._rb_use_anthropic:
            try:
                import anthropic
                self._rb_anthropic_client = anthropic.Anthropic(
                    api_key=self._rb_anthropic_api_key,
                    base_url=self._rb_anthropic_base_url,
                )
            except Exception as e:
                logger.warning(
                    "RB Anthropic client init failed, falling back to local LLM: %s", e
                )
                self._rb_use_anthropic = False

        try:
            import chromadb
            self._chroma_client = chromadb.HttpClient(
                host=chroma_host, port=int(chroma_port)
            )
            self._rb_collection = self._chroma_client.get_or_create_collection(
                name=_RB_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
            count = self._rb_collection.count()
            llm_descr = (
                f"anthropic://{self._rb_anthropic_base_url}/{self._rb_anthropic_model}"
                if self._rb_use_anthropic
                else f"{self._rb_llm_url}/{self._rb_llm_model}"
            )
            logger.info(
                "ReasoningBank initialized: collection=%s, strategies=%d, "
                "llm=%s, embed=%s/%s",
                _RB_COLLECTION, count,
                llm_descr,
                self._rb_embed_url, self._rb_embed_model,
            )
        except Exception as e:
            self._rb_collection = None
            logger.warning("ReasoningBank init failed (non-fatal, will skip RB): %s", e)

    # ─── ReasoningBank: Embed ──────────────────────────────────────────

    def _rb_embed(self, text: str) -> Optional[List[float]]:
        """Get embedding vector from the embedding server."""
        try:
            resp = httpx.post(
                f"{self._rb_embed_url}/embeddings",
                json={"model": self._rb_embed_model, "input": text},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
        except Exception as e:
            logger.warning("RB embedding failed: %s", e)
            return None

    # ─── ReasoningBank: Search ─────────────────────────────────────────

    def _rb_search(self, query: str, limit: int = _RB_MAX_PREFETCH_RESULTS) -> List[Dict[str, Any]]:
        """Search rb_strategies collection for relevant strategies."""
        if not self._rb_collection:
            return []
        try:
            embedding = self._rb_embed(query)
            if not embedding:
                return []

            results = self._rb_collection.query(
                query_embeddings=[embedding],
                n_results=limit,
                include=["documents", "metadatas", "distances"],
            )

            strategies = []
            if results and results.get("documents"):
                for i, doc in enumerate(results["documents"][0]):
                    # ChromaDB cosine distance: 0 = identical, 2 = opposite
                    # Convert to similarity: 1 - (distance / 2)
                    distance = results["distances"][0][i] if results.get("distances") else 1.0
                    similarity = 1.0 - (distance / 2.0)

                    if similarity < _RB_RELEVANCE_THRESHOLD:
                        continue

                    meta = results["metadatas"][0][i] if results.get("metadatas") else {}
                    strategies.append({
                        "text": doc,
                        "context": meta.get("context", ""),
                        "signal": meta.get("source_signal", ""),
                        "similarity": round(similarity, 3),
                        "id": results["ids"][0][i],
                    })

            return strategies
        except Exception as e:
            logger.debug("RB search failed: %s", e)
            return []

    # ─── ReasoningBank: Store ──────────────────────────────────────────

    def _rb_store(self, strategies: List[Dict[str, Any]], session_id: str = "") -> int:
        """Store induced strategies in ChromaDB, with deduplication."""
        if not self._rb_collection or not strategies:
            return 0

        stored = 0
        now = datetime.now(timezone.utc).isoformat()

        for strategy in strategies[:_RB_MAX_STRATEGIES_PER_SESSION]:
            text = strategy.get("text", "").strip()
            if not text:
                continue

            embedding = self._rb_embed(text)
            if not embedding:
                continue

            # Dedup check: search for similar existing strategies
            try:
                existing = self._rb_collection.query(
                    query_embeddings=[embedding],
                    n_results=1,
                    include=["documents", "metadatas", "distances"],
                )
                if (existing and existing.get("distances")
                        and existing["distances"][0]
                        and len(existing["distances"][0]) > 0):
                    distance = existing["distances"][0][0]
                    similarity = 1.0 - (distance / 2.0)
                    if similarity >= _RB_DEDUP_THRESHOLD:
                        # Update existing: increment use_count
                        existing_id = existing["ids"][0][0]
                        existing_meta = existing["metadatas"][0][0] if existing.get("metadatas") else {}
                        use_count = int(existing_meta.get("use_count", 0)) + 1
                        existing_meta["use_count"] = use_count
                        existing_meta["last_updated"] = now
                        self._rb_collection.update(
                            ids=[existing_id],
                            metadatas=[existing_meta],
                        )
                        logger.info(
                            "RB dedup: updated existing strategy %s (use_count=%d, sim=%.2f)",
                            existing_id[:8], use_count, similarity,
                        )
                        stored += 1
                        continue
            except Exception as e:
                logger.debug("RB dedup check failed, storing as new: %s", e)

            # Store as new strategy
            strategy_id = f"rb_{uuid.uuid4().hex[:16]}"
            metadata = {
                "user_id": self._user_id,
                "type": "strategy",
                "context": strategy.get("context", ""),
                "source_signal": strategy.get("signal", ""),
                "induced_from_session": session_id,
                "created_at": now,
                "use_count": 0,
                "last_used": "",
            }

            try:
                self._rb_collection.add(
                    ids=[strategy_id],
                    documents=[text],
                    embeddings=[embedding],
                    metadatas=[metadata],
                )
                stored += 1
                logger.info("RB stored new strategy: %s — %s", strategy_id[:8], text[:80])
            except Exception as e:
                logger.warning("RB store failed for strategy: %s", e)

        return stored

    # ─── ReasoningBank: Induce ─────────────────────────────────────────

    def _rb_induce(self, messages: List[Dict[str, Any]], session_id: str = "") -> None:
        """Induce strategies from a full session transcript using LLM.
        Called at session end, runs in background thread."""
        if not self._rb_collection:
            return

        # Filter to user+assistant, build transcript
        transcript_parts = []
        turn_count = 0
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content and isinstance(content, str):
                label = "USER" if role == "user" else "ASSISTANT"
                transcript_parts.append(f"[{label}]: {content}")
                if role == "user":
                    turn_count += 1

        if turn_count < _RB_MIN_SESSION_TURNS:
            logger.info("RB induction skipped: only %d user turns (min %d)", turn_count, _RB_MIN_SESSION_TURNS)
            return

        transcript = "\n\n".join(transcript_parts)

        # Truncate if too long (keep last 8000 chars to stay within context)
        if len(transcript) > 8000:
            transcript = "...(sessione troncata)...\n\n" + transcript[-8000:]

        system_prompt = REASONING_BANK_INDUCTION_PROMPT.format(
            max_strategies=_RB_MAX_STRATEGIES_PER_SESSION,
        )

        try:
            user_content = f"Transcript della sessione:\n\n{transcript}"
            if self._rb_use_anthropic and self._rb_anthropic_client is not None:
                # Anthropic-compatible path: messages.create with system prompt
                # as separate parameter. Plain text output (no tool_use) — the
                # induction prompt asks for a JSON-only response which we parse
                # below with the same fence-stripping logic as the OpenAI path.
                ant_resp = self._rb_anthropic_client.messages.create(
                    model=self._rb_anthropic_model,
                    max_tokens=1000,
                    temperature=0.3,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                    timeout=_RB_INDUCTION_TIMEOUT,
                )
                raw_content = "".join(
                    b.text for b in ant_resp.content
                    if getattr(b, "type", None) == "text"
                )
            else:
                resp = httpx.post(
                    f"{self._rb_llm_url}/chat/completions",
                    json={
                        "model": self._rb_llm_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 1000,
                    },
                    headers={"Authorization": "Bearer not-needed"},
                    timeout=_RB_INDUCTION_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                raw_content = data["choices"][0]["message"]["content"]

            # Parse JSON from response (handle markdown code blocks)
            json_str = raw_content.strip()
            if json_str.startswith("```"):
                # Strip markdown fences
                lines = json_str.split("\n")
                json_lines = [l for l in lines if not l.strip().startswith("```")]
                json_str = "\n".join(json_lines)

            result = json.loads(json_str)
            strategies = result.get("strategies", [])

            if not strategies:
                logger.info("RB induction: no strategies found in session %s", session_id[:8] if session_id else "?")
                return

            stored = self._rb_store(strategies, session_id=session_id)
            logger.info(
                "RB induction complete: %d strategies induced, %d stored for user %s",
                len(strategies), stored, self._user_id,
            )

        except json.JSONDecodeError as e:
            logger.warning("RB induction: failed to parse LLM response as JSON: %s", e)
        except Exception as e:
            logger.warning("RB induction failed for session %s: %s", session_id[:8] if session_id else "?", e)

    def system_prompt_block(self) -> str:
        return (
            "# Mem0 Memory\n"
            f"Active (self-hosted). User: {self._user_id}.\n"
            "La memoria a lungo termine è gestita automaticamente a fine sessione.\n"
            "Tools: mem0_search (cerca nella memoria), mem0_search_cross (cerca tra tutti gli utenti), "
            "mem0_save (salva fatti nella memoria a lungo termine).\n"
            "Se l'utente dice 'ricorda che...' o chiede di salvare qualcosa, "
            "usa mem0_save per memorizzare direttamente. Il pipeline estrae fatti e relazioni automaticamente."
        )

    # ─── Circuit breaker ───────────────────────────────────────────────

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning("Mem0 circuit breaker tripped after %d failures", self._consecutive_failures)

    # ─── Redis context bus ─────────────────────────────────────────────

    def _redis_get_external_context(self, user_id: str) -> str:
        """Read recent events from other systems (orchestrator, HA).
        Excludes events from 'hermes' source to avoid duplication."""
        if not self._redis_available or not self._redis:
            return ""
        try:
            key = f"ctx:{user_id}:events"
            raw_events = self._redis.lrange(key, 0, _REDIS_MAX_EVENTS - 1)
            if not raw_events:
                return ""

            lines = []
            now = time.time()
            for raw in raw_events:
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if evt.get("source") == "hermes":
                    continue

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
                if len(lines) >= _REDIS_CTX_LIMIT:
                    break

            return "\n".join(lines)
        except Exception as e:
            logger.debug("Redis context read failed: %s", e)
            return ""

    def _redis_push_event(self, user_id: str, text: str) -> None:
        """Push a Hermes event to Redis so orchestrator/HA can see it."""
        if not self._redis_available or not self._redis:
            return
        try:
            key = f"ctx:{user_id}:events"
            event = json.dumps({
                "ts": time.time(),
                "source": "hermes",
                "text": text[:200],
                "type": "response",
            }, ensure_ascii=False)
            pipe = self._redis.pipeline()
            pipe.lpush(key, event)
            pipe.ltrim(key, 0, _REDIS_MAX_EVENTS - 1)
            pipe.expire(key, _REDIS_CTX_TTL)
            pipe.execute()
        except Exception as e:
            logger.debug("Redis push failed: %s", e)

    # ─── Shared memory helpers ─────────────────────────────────────────

    def _search_with_shared(self, query: str, limit: int = 5) -> dict:
        """Search own memories + shared memories via native memory.search()."""
        resp = self._client.post("/search", json={
            "query": query, "user_id": self._user_id, "limit": limit,
        })
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        relations = data.get("relations", [])

        if self._user_id != "shared":
            try:
                resp2 = self._client.post("/search", json={
                    "query": query, "user_id": "shared", "limit": limit,
                })
                resp2.raise_for_status()
                data2 = resp2.json()
                shared_results = data2.get("results", [])
                shared_relations = data2.get("relations", [])
                for r in shared_results:
                    if isinstance(r, dict):
                        r["_source"] = "shared"
                results = results + shared_results
                if shared_relations:
                    relations = (relations or []) + shared_relations
            except Exception as e:
                logger.debug("Mem0 shared search failed: %s", e)

        return {"results": results, "relations": relations}

    # ─── Prefetch / sync ───────────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## Mem0 Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open():
            return

        def _run():
            try:
                # 1. Mem0 search (long-term declarative memory)
                data = self._search_with_shared(query, limit=5)
                results = data.get("results", [])
                relations = data.get("relations", [])
                lines = []

                for r in results:
                    mem = r.get("memory", r.get("data", "")) if isinstance(r, dict) else str(r)
                    if mem:
                        src = r.get("_source", "")
                        prefix = "[shared] " if src == "shared" else ""
                        lines.append(f"{prefix}{mem}")

                if relations:
                    for rel in relations[:5]:
                        if isinstance(rel, dict):
                            lines.append(f"[graph] {rel.get('source', '')} → {rel.get('relationship', '')} → {rel.get('target', rel.get('destination', ''))}")
                        elif isinstance(rel, str):
                            lines.append(f"[graph] {rel}")

                # 2. Redis context bus (short-term cross-system)
                redis_ctx = self._redis_get_external_context(self._user_id)

                # 3. ReasoningBank search (long-term procedural memory)
                rb_strategies = self._rb_search(query)

                # Combine all layers
                combined = "\n".join(f"- {l}" for l in lines[:10])

                if redis_ctx:
                    combined = f"{combined}\n\n### Contesto recente (voice/domotica)\n{redis_ctx}" if combined else f"### Contesto recente (voice/domotica)\n{redis_ctx}"

                if rb_strategies:
                    rb_lines = []
                    for s in rb_strategies:
                        ctx_hint = f" [{s['context']}]" if s.get("context") else ""
                        rb_lines.append(f"- {s['text']}{ctx_hint}")
                    rb_block = "\n".join(rb_lines)
                    combined = f"{combined}\n\n### Strategie apprese\n{rb_block}" if combined else f"### Strategie apprese\n{rb_block}"

                with self._prefetch_lock:
                    self._prefetch_result = combined
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Mem0 prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="mem0-prefetch")
        self._prefetch_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Push a brief summary of Hermes response to Redis for orchestrator/HA visibility."""
        if assistant_content and len(assistant_content) > 10:
            summary = assistant_content[:200]
            self._redis_push_event(self._user_id, summary)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Send full conversation to mem0 pipeline for fact extraction + graph,
        and to ReasoningBank for strategy induction.
        Both run async in background threads."""
        if not messages or self._is_breaker_open():
            return

        # Filter to user + assistant messages only, skip system
        conversation = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content and isinstance(content, str):
                conversation.append({"role": role, "content": content})

        if not conversation:
            return

        session_id = f"sess_{uuid.uuid4().hex[:12]}"

        # Thread 1: Mem0 fact extraction (declarative)
        def _flush_mem0():
            try:
                client = httpx.Client(base_url=self._base_url, timeout=_ADD_TIMEOUT)
                resp = client.post("/add", json={
                    "messages": conversation,
                    "user_id": self._user_id,
                })
                resp.raise_for_status()
                result = resp.json()
                added = len(result.get("results", []))
                logger.info("Mem0 session flush: %d facts extracted for user %s", added, self._user_id)
                self._record_success()
                client.close()
            except Exception as e:
                self._record_failure()
                logger.warning("Mem0 session flush failed for %s: %s", self._user_id, e)

        # Thread 2: ReasoningBank strategy induction (procedural)
        def _flush_rb():
            self._rb_induce(conversation, session_id=session_id)

        t1 = threading.Thread(target=_flush_mem0, daemon=True, name="mem0-session-flush")
        t1.start()

        t2 = threading.Thread(target=_flush_rb, daemon=True, name="rb-session-induction")
        t2.start()

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """When Hermes writes to builtin MEMORY.md, also send to mem0 pipeline."""
        if action not in ("add", "replace") or not content:
            return
        if self._is_breaker_open():
            return

        def _write():
            try:
                client = httpx.Client(base_url=self._base_url, timeout=_ADD_TIMEOUT)
                resp = client.post("/add", json={
                    "text": content,
                    "user_id": self._user_id,
                })
                resp.raise_for_status()
                self._record_success()
                logger.info("Mem0 memory_write bridged for user %s", self._user_id)
                client.close()
            except Exception as e:
                self._record_failure()
                logger.debug("Mem0 memory_write bridge failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="mem0-memwrite")
        t.start()

    # ─── Tool schemas ──────────────────────────────────────────────────

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "mem0_search",
                "description": (
                    "Cerca nella memoria a lungo termine. Restituisce fatti e relazioni "
                    "rilevanti dal grafo di conoscenza. Usa quando serve contesto storico."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Cosa cercare."},
                        "limit": {"type": "integer", "description": "Max risultati (default: 10)."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mem0_search_cross",
                "description": "Cerca nella memoria di TUTTI gli utenti. Usa per conoscenza condivisa.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Cosa cercare."},
                        "limit": {"type": "integer", "description": "Max risultati (default: 10)."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mem0_save",
                "description": (
                    "Salva informazioni nella memoria a lungo termine Mem0. "
                    "Usa quando l'utente dice 'ricorda che...', 'salva in memoria', "
                    "o vuole memorizzare fatti importanti. Il testo viene processato "
                    "dal pipeline mem0 (estrazione fatti + grafo)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Il testo da memorizzare. Scrivi fatti chiari e completi.",
                        },
                    },
                    "required": ["text"],
                },
            },
        ]

    # ─── Tool handlers ─────────────────────────────────────────────────

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps({"error": "Mem0 temporarily unavailable."})

        try:
            if tool_name == "mem0_search":
                query = args.get("query", "")
                if not query:
                    return json.dumps({"error": "Missing: query"})
                limit = min(int(args.get("limit", 10)), 50)
                data = self._search_with_shared(query, limit=limit)
                self._record_success()

                results = data.get("results", [])
                relations = data.get("relations", [])

                if not results and not relations:
                    return json.dumps({"result": "Nessun ricordo rilevante trovato."})

                items = []
                for r in results:
                    mem = r.get("memory", r.get("data", "")) if isinstance(r, dict) else str(r)
                    score = r.get("score", 0) if isinstance(r, dict) else 0
                    src = r.get("_source", "") if isinstance(r, dict) else ""
                    if mem:
                        items.append({"memory": mem, "score": score,
                                      "source": src or self._user_id})

                rel_items = []
                if relations:
                    for rel in relations:
                        if isinstance(rel, dict):
                            rel_items.append(
                                f"{rel.get('source', '')} → {rel.get('relationship', '')} → {rel.get('target', rel.get('destination', ''))}"
                            )
                        elif isinstance(rel, str):
                            rel_items.append(rel)

                return json.dumps({
                    "results": items,
                    "relations": rel_items,
                    "count": len(items),
                })

            elif tool_name == "mem0_search_cross":
                query = args.get("query", "")
                if not query:
                    return json.dumps({"error": "Missing: query"})
                limit = min(int(args.get("limit", 10)), 50)
                resp = self._client.post("/search_cross", json={
                    "query": query, "limit": limit,
                })
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                self._record_success()
                if not results:
                    return json.dumps({"result": "Nessun ricordo trovato."})
                items = []
                for r in results:
                    mem = r.get("memory", r.get("data", "")) if isinstance(r, dict) else str(r)
                    if mem:
                        items.append({"memory": mem})
                return json.dumps({"results": items, "count": len(items)})

            elif tool_name == "mem0_save":
                text = args.get("text", "")
                if not text:
                    return json.dumps({"error": "Missing: text"})
                save_client = httpx.Client(base_url=self._base_url, timeout=_ADD_TIMEOUT)
                resp = save_client.post("/add", json={
                    "text": text,
                    "user_id": self._user_id,
                })
                resp.raise_for_status()
                result = resp.json()
                added = len(result.get("results", []))
                save_client.close()
                self._record_success()
                return json.dumps({
                    "status": "ok",
                    "facts_extracted": added,
                    "message": f"Salvato: {added} fatti estratti e memorizzati.",
                })

        except Exception as e:
            self._record_failure()
            return json.dumps({"error": f"Mem0 API error: {e}"})

        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    def shutdown(self) -> None:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)
        if self._client:
            self._client.close()
            self._client = None
        if self._redis:
            try:
                self._redis.close()
            except Exception:
                pass
            self._redis = None


def register(ctx) -> None:
    """Register Mem0 self-hosted as a memory provider plugin."""
    ctx.register_memory_provider(Mem0SelfHostedProvider())
