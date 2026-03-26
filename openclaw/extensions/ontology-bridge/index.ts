/**
 * Ontology Bridge Plugin for OpenClaw v2.0
 * =========================================
 *
 * Unified memory bridge combining three sources:
 *
 * 1. KEYWORD MATCH (ontology entities):
 *    - Cached index of all ontology entities
 *    - Matches entity names in prompt → fetches properties + relations
 *
 * 2. SEMANTIC SEARCH (shared ChromaDB):
 *    - Queries user_messages and user_facts collections
 *    - Embedding via fastembed (nomic-embed-text, Ollama-compatible API)
 *    - Returns top-k semantically relevant messages/facts
 *
 * 3. RECENT MESSAGES (orchestrator API):
 *    - Fetches last N messages from orchestrator SQLite
 *    - Provides immediate conversational context
 *
 * All three sources share a 600-token budget.
 *
 * 4. FACT EXTRACTION (agent_end):
 *    - Pattern-matches "remember", decisions, preferences
 *    - Writes back to ontology via REST API
 *
 * 5. TOOL TRACKING (after_tool_call):
 *    - Monitors ontology skill usage to avoid duplicate writes
 */

import { appendFileSync, existsSync, mkdirSync } from "fs";
import { dirname } from "path";
import type { OpenClawPluginDefinition } from "openclaw/plugin-sdk";

// ─── Types ──────────────────────────────────────────────────────────────────

interface PluginConfig {
  ontologyUrl?: string;
  chromaUrl?: string;
  orchestratorUrl?: string;
  embeddingUrl?: string;
  defaultSpeakerId?: string;
  cacheRefreshIntervalMs?: number;
  maxContextTokens?: number;
  recentMessagesCount?: number;
  extractionEnabled?: boolean;
  skipAgents?: string[];
  logPath?: string;
}

interface CachedEntity {
  id: string;
  type: string;
  searchKey: string;
  displayName: string;
  keywords: string[];
}

interface OntologyEntity {
  id: string;
  type: string;
  properties: Record<string, unknown>;
  created: string;
  updated: string;
}

interface MatchedContext {
  entity: OntologyEntity;
  relations: Array<{
    rel_type: string;
    direction: "outgoing" | "incoming";
    target: { id: string; type: string; name: string };
  }>;
}

// ─── Entity Cache ───────────────────────────────────────────────────────────

class EntityCache {
  private entities: CachedEntity[] = [];
  private lastRefresh = 0;
  private refreshIntervalMs: number;
  private ontologyUrl: string;
  private speakerId: string;
  private apiToken: string;
  private logger: { info: (m: string) => void; warn: (m: string) => void; debug?: (m: string) => void };
  private refreshing = false;

  constructor(opts: {
    ontologyUrl: string;
    speakerId: string;
    apiToken: string;
    refreshIntervalMs: number;
    logger: EntityCache["logger"];
  }) {
    this.ontologyUrl = opts.ontologyUrl;
    this.speakerId = opts.speakerId;
    this.apiToken = opts.apiToken;
    this.refreshIntervalMs = opts.refreshIntervalMs;
    this.logger = opts.logger;
  }

  async ensureFresh(): Promise<void> {
    const now = Date.now();
    if (now - this.lastRefresh < this.refreshIntervalMs) return;
    if (this.refreshing) return;
    this.refreshing = true;
    try {
      await this.refresh();
    } finally {
      this.refreshing = false;
    }
  }

  private async refresh(): Promise<void> {
    const SEARCHABLE_TYPES = [
      "Person", "Organization", "Project", "Task", "Goal", "Strategy",
      "Account", "Event", "Location", "Document", "Topic", "Preference",
      "Device", "Skill", "Note",
    ];

    const allEntities: CachedEntity[] = [];

    for (const type of SEARCHABLE_TYPES) {
      try {
        const resp = await fetch(
          `${this.ontologyUrl}/entities?type=${type}&limit=200`,
          {
            headers: {
              "X-Speaker-Id": this.speakerId,
              ...(this.apiToken ? { Authorization: `Bearer ${this.apiToken}` } : {}),
            },
          },
        );
        if (!resp.ok) continue;
        const data = (await resp.json()) as OntologyEntity[];
        for (const e of data) {
          const props = e.properties || {};
          const name =
            (props.name as string) ||
            (props.title as string) ||
            (props.subject as string) ||
            "";
          if (!name) continue;

          const keywords: string[] = [];
          if (props.tags && Array.isArray(props.tags)) {
            keywords.push(...(props.tags as string[]).map((t) => t.toLowerCase()));
          }
          if (props.description && typeof props.description === "string") {
            keywords.push(props.description.slice(0, 60).toLowerCase());
          }
          if (props.speaker_id && typeof props.speaker_id === "string") {
            keywords.push(props.speaker_id.toLowerCase());
          }

          allEntities.push({
            id: e.id,
            type: e.type,
            searchKey: name.toLowerCase(),
            displayName: name,
            keywords,
          });
        }
      } catch {
        // Silently skip failed types
      }
    }

    this.entities = allEntities;
    this.lastRefresh = Date.now();
    this.logger.info(`[ontology-bridge] Cache refreshed: ${allEntities.length} entities indexed`);
  }

  findMatches(text: string, maxResults = 8): CachedEntity[] {
    const lower = text.toLowerCase();
    const scored: Array<{ entity: CachedEntity; score: number }> = [];

    for (const e of this.entities) {
      let score = 0;

      if (e.searchKey.length >= 3 && lower.includes(e.searchKey)) {
        score += 10;
        score += Math.min(e.searchKey.length / 10, 2);
      }

      for (const kw of e.keywords) {
        if (kw.length >= 3 && lower.includes(kw)) {
          score += 3;
          break;
        }
      }

      if (score > 0) {
        if (e.type === "Person") score += 2;
        if (e.type === "Project" || e.type === "Task") score += 1;
      }

      if (score > 0) {
        scored.push({ entity: e, score });
      }
    }

    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, maxResults).map((s) => s.entity);
  }

  get size(): number {
    return this.entities.length;
  }
}

// ─── Ontology API Client ────────────────────────────────────────────────────

class OntologyClient {
  private baseUrl: string;
  private speakerId: string;
  private apiToken: string;

  constructor(baseUrl: string, speakerId: string, apiToken: string) {
    this.baseUrl = baseUrl;
    this.speakerId = speakerId;
    this.apiToken = apiToken;
  }

  private headers(speakerId?: string): Record<string, string> {
    const h: Record<string, string> = {
      "X-Speaker-Id": speakerId || this.speakerId,
      "Content-Type": "application/json",
    };
    if (this.apiToken) h.Authorization = `Bearer ${this.apiToken}`;
    return h;
  }

  async getEntity(id: string): Promise<OntologyEntity | null> {
    try {
      const resp = await fetch(`${this.baseUrl}/entities/${id}`, {
        headers: this.headers(),
      });
      if (!resp.ok) return null;
      return (await resp.json()) as OntologyEntity;
    } catch {
      return null;
    }
  }

  async getRelated(
    id: string,
    dir: "outgoing" | "incoming" | "both" = "both",
  ): Promise<Array<{ rel_type: string; direction: string; entity: OntologyEntity }>> {
    try {
      const resp = await fetch(
        `${this.baseUrl}/entities/${id}/related?dir=${dir}`,
        { headers: this.headers() },
      );
      if (!resp.ok) return [];
      return (await resp.json()) as Array<{
        rel_type: string;
        direction: string;
        entity: OntologyEntity;
      }>;
    } catch {
      return [];
    }
  }

  async createEntity(
    type: string,
    properties: Record<string, unknown>,
    speakerId?: string,
  ): Promise<OntologyEntity | null> {
    try {
      const resp = await fetch(`${this.baseUrl}/entities`, {
        method: "POST",
        headers: this.headers(speakerId),
        body: JSON.stringify({ type, properties }),
      });
      if (!resp.ok) return null;
      return (await resp.json()) as OntologyEntity;
    } catch {
      return null;
    }
  }

  async healthCheck(): Promise<boolean> {
    try {
      const resp = await fetch(`${this.baseUrl}/health`, {
        signal: AbortSignal.timeout(3000),
      });
      return resp.ok;
    } catch {
      return false;
    }
  }
}

// ─── ChromaDB Client (REST API) ─────────────────────────────────────────────

class ChromaClient {
  private baseUrl: string;
  private embeddingUrl: string;
  private embeddingModel: string;

  constructor(baseUrl: string, embeddingUrl: string, embeddingModel = "nomic-embed-text") {
    this.baseUrl = baseUrl;
    this.embeddingUrl = embeddingUrl;
    this.embeddingModel = embeddingModel;
  }

  /** Get embedding from fastembed (Ollama-compatible API). */
  private async embed(text: string): Promise<number[] | null> {
    try {
      const resp = await fetch(`${this.embeddingUrl}/api/embeddings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: this.embeddingModel, prompt: text }),
        signal: AbortSignal.timeout(5000),
      });
      if (!resp.ok) return null;
      const data = (await resp.json()) as { embedding: number[] };
      return data.embedding;
    } catch {
      return null;
    }
  }

  /** Query a ChromaDB collection via REST API. */
  async query(
    collectionName: string,
    queryText: string,
    nResults = 5,
    whereFilter?: Record<string, unknown>,
  ): Promise<Array<{ content: string; metadata: Record<string, unknown>; distance: number }>> {
    try {
      // Get collection ID
      const colResp = await fetch(
        `${this.baseUrl}/api/v2/tenants/default_tenant/databases/default_database/collections/${collectionName}`,
        { signal: AbortSignal.timeout(3000) },
      );
      if (!colResp.ok) return [];
      const col = (await colResp.json()) as { id: string };

      // Get embedding for query
      const embedding = await this.embed(queryText);
      if (!embedding) return [];

      // Query collection
      const queryBody: Record<string, unknown> = {
        query_embeddings: [embedding],
        n_results: nResults,
        include: ["documents", "metadatas", "distances"],
      };
      if (whereFilter) queryBody.where = whereFilter;

      const resp = await fetch(
        `${this.baseUrl}/api/v2/tenants/default_tenant/databases/default_database/collections/${col.id}/query`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(queryBody),
          signal: AbortSignal.timeout(5000),
        },
      );
      if (!resp.ok) return [];

      const data = (await resp.json()) as {
        documents: string[][];
        metadatas: Array<Array<Record<string, unknown>>>;
        distances: number[][];
      };

      if (!data.documents?.[0]) return [];

      const results: Array<{ content: string; metadata: Record<string, unknown>; distance: number }> = [];
      for (let i = 0; i < data.documents[0].length; i++) {
        results.push({
          content: data.documents[0][i],
          metadata: data.metadatas?.[0]?.[i] || {},
          distance: data.distances?.[0]?.[i] || 1,
        });
      }
      return results;
    } catch {
      return [];
    }
  }

  async healthCheck(): Promise<boolean> {
    try {
      const resp = await fetch(`${this.baseUrl}/api/v2/heartbeat`, {
        signal: AbortSignal.timeout(2000),
      });
      return resp.ok;
    } catch {
      return false;
    }
  }
}

// ─── Orchestrator Client ────────────────────────────────────────────────────

class OrchestratorClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  /** Fetch recent chat messages from orchestrator API. */
  async getRecentMessages(count = 6): Promise<Array<{ role: string; content: string; speaker: string }>> {
    try {
      const resp = await fetch(
        `${this.baseUrl}/api/recent_turns?max_turns=${Math.ceil(count / 2)}&max_seconds=900`,
        { signal: AbortSignal.timeout(3000) },
      );
      if (!resp.ok) return [];
      return (await resp.json()) as Array<{ role: string; content: string; speaker: string }>;
    } catch {
      return [];
    }
  }
}

// ─── Context Formatters ─────────────────────────────────────────────────────

function formatEntityContext(matched: MatchedContext[]): string {
  if (matched.length === 0) return "";

  const lines: string[] = ["[Ontology Context]"];

  for (const m of matched) {
    const props = m.entity.properties || {};
    const name = (props.name as string) || (props.title as string) || m.entity.id;
    const type = m.entity.type;

    const keyProps = Object.entries(props)
      .filter(([k, v]) => {
        if (k === "owner" || k === "visibility" || k === "notes") return false;
        if (k === "name" || k === "title") return false;
        if (typeof v === "object") return false;
        return true;
      })
      .slice(0, 6)
      .map(([k, v]) => `${k}=${v}`)
      .join(", ");

    lines.push(`- ${type}:${name} (${m.entity.id})${keyProps ? ` [${keyProps}]` : ""}`);

    if (m.relations.length > 0) {
      const relSummary = m.relations
        .slice(0, 5)
        .map((r) => {
          const arrow = r.direction === "outgoing" ? "->" : "<-";
          return `${r.rel_type}${arrow}${r.target.type}:${r.target.name}`;
        })
        .join("; ");
      lines.push(`  relations: ${relSummary}`);
    }
  }

  return lines.join("\n");
}

function formatSemanticResults(
  results: Array<{ content: string; metadata: Record<string, unknown>; distance: number }>,
  label: string,
): string {
  if (results.length === 0) return "";
  const lines: string[] = [`[${label}]`];
  for (const r of results) {
    const similarity = (1 - r.distance).toFixed(2);
    const speaker = (r.metadata.speaker_name as string) || "User";
    const content = (r.metadata.content as string) || r.content;
    const truncated = content.length > 150 ? content.slice(0, 147) + "..." : content;
    lines.push(`- (${similarity}) ${speaker}: ${truncated}`);
  }
  return lines.join("\n");
}

function formatRecentMessages(
  messages: Array<{ role: string; content: string; speaker: string }>,
): string {
  if (messages.length === 0) return "";
  const lines: string[] = ["[Recent Conversation]"];
  for (const msg of messages) {
    const speaker = msg.role === "user" ? (msg.speaker || "User") : "JARVIS";
    const truncated = msg.content.length > 200 ? msg.content.slice(0, 197) + "..." : msg.content;
    lines.push(`- ${speaker}: ${truncated}`);
  }
  return lines.join("\n");
}

function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

// ─── Fact Extraction Patterns ───────────────────────────────────────────────

interface ExtractedFact {
  type: "status_update" | "new_entity" | "preference" | "decision" | "note";
  entityType?: string;
  description: string;
  properties?: Record<string, unknown>;
}

function extractFacts(assistantTexts: string[], prompt: string): ExtractedFact[] {
  const facts: ExtractedFact[] = [];
  const fullText = assistantTexts.join("\n");

  // Task status changes
  const taskDone = fullText.match(
    /(?:completed|finished|done|closed|cancelled|resolved)\s+(?:the\s+)?(?:task|ticket|issue)\s+["']?([^"'\n.]+)/gi,
  );
  if (taskDone) {
    for (const match of taskDone) {
      const taskName = match
        .replace(/^(?:completed|finished|done|closed|cancelled|resolved)\s+(?:the\s+)?(?:task|ticket|issue)\s+["']?/i, "")
        .trim();
      facts.push({
        type: "status_update",
        entityType: "Task",
        description: `Task "${taskName}" marked as done`,
        properties: { status_enum: "done" },
      });
    }
  }

  // "Remember" requests
  const rememberMatch = prompt.match(
    /(?:ricorda(?:ti)?|remember|salva|annota|save|note)\s+(?:che|that|questo|this)?\s*[:\-]?\s*(.{10,200})/i,
  );
  if (rememberMatch) {
    facts.push({
      type: "note",
      entityType: "Note",
      description: rememberMatch[1].trim(),
      properties: { content: rememberMatch[1].trim() },
    });
  }

  // Preferences
  const prefMatch = prompt.match(
    /(?:preferisco|i prefer|voglio sempre|always use|d'ora in poi|from now on)\s+(.{5,150})/i,
  );
  if (prefMatch) {
    facts.push({
      type: "preference",
      entityType: "Preference",
      description: prefMatch[1].trim(),
      properties: { subject: "user_preference", value: prefMatch[1].trim() },
    });
  }

  // Decisions
  const decisionPatterns = [
    /(?:ho deciso|decided|decision is|abbiamo scelto|we chose)\s*(?:di|to|:)?\s*(.{10,200})/i,
    /(?:la decisione è|the plan is|andremo con|we'll go with)\s*(?:di|to|:)?\s*(.{10,200})/i,
  ];
  for (const pat of decisionPatterns) {
    const dMatch = prompt.match(pat) || fullText.match(pat);
    if (dMatch) {
      facts.push({
        type: "decision",
        entityType: "Note",
        description: dMatch[1].trim(),
        properties: { content: `Decision: ${dMatch[1].trim()}`, tags: ["decision"] },
      });
      break;
    }
  }

  return facts;
}

// ─── Logging ────────────────────────────────────────────────────────────────

function logBridge(logPath: string | undefined, entry: Record<string, unknown>): void {
  if (!logPath) return;
  try {
    const dir = dirname(logPath);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    appendFileSync(logPath, JSON.stringify(entry) + "\n");
  } catch {
    // Logging should never crash the plugin
  }
}

// ─── Plugin Entry Point ─────────────────────────────────────────────────────

const plugin: OpenClawPluginDefinition = {
  id: "ontology-bridge",
  name: "Ontology Bridge",
  version: "2.0.0",
  description:
    "Unified memory bridge: ontology entities + ChromaDB semantic search + orchestrator recent messages.",

  register(api) {
    const cfg = (api.pluginConfig || {}) as PluginConfig;
    const ontologyUrl = cfg.ontologyUrl || process.env.ONTOLOGY_URL || "http://127.0.0.1:8100";
    const chromaUrl = cfg.chromaUrl || process.env.CHROMA_URL || "http://127.0.0.1:8000";
    const orchestratorUrl = cfg.orchestratorUrl || process.env.ORCHESTRATOR_URL || "http://127.0.0.1:5000";
    const embeddingUrl = cfg.embeddingUrl || process.env.EMBEDDING_URL || "http://127.0.0.1:11435";
    const defaultSpeakerId = cfg.defaultSpeakerId || "jarvis-agent";
    const cacheRefreshMs = cfg.cacheRefreshIntervalMs || 300_000;
    const maxContextTokens = cfg.maxContextTokens || 600;
    const recentMessagesCount = cfg.recentMessagesCount || 6;
    const extractionEnabled = cfg.extractionEnabled !== false;
    const skipAgents = new Set(cfg.skipAgents || ["personal-relay"]);
    const logPath =
      cfg.logPath || "/home/jarvis/.openclaw/workspace/state/ontology-bridge.jsonl";
    const apiToken = process.env.ONTOLOGY_API_TOKEN || "";

    // Token budget allocation (within maxContextTokens = 600):
    // - Ontology entities: up to ~250 tokens (42%)
    // - Semantic search: up to ~200 tokens (33%)
    // - Recent messages: up to ~150 tokens (25%)
    const TOKEN_BUDGET = {
      ontology: Math.floor(maxContextTokens * 0.42),
      semantic: Math.floor(maxContextTokens * 0.33),
      recent: Math.floor(maxContextTokens * 0.25),
    };

    // Initialize clients
    const cache = new EntityCache({
      ontologyUrl,
      speakerId: defaultSpeakerId,
      apiToken,
      refreshIntervalMs: cacheRefreshMs,
      logger: api.logger,
    });
    const ontologyClient = new OntologyClient(ontologyUrl, defaultSpeakerId, apiToken);
    const chromaClient = new ChromaClient(chromaUrl, embeddingUrl);
    const orchestratorClient = new OrchestratorClient(orchestratorUrl);

    // Track ontology tool usage per session
    const sessionOntologyUsage = new Map<string, number>();

    // Service availability flags
    let chromaAvailable = false;

    // ── Startup health checks ──
    ontologyClient.healthCheck().then((ok) => {
      if (ok) {
        api.logger.info(`[ontology-bridge] Ontology server reachable at ${ontologyUrl}`);
        cache.ensureFresh().catch(() => {});
      } else {
        api.logger.warn(`[ontology-bridge] Ontology server unreachable at ${ontologyUrl}`);
      }
    });

    // Delay first check — cold cross-network connection may timeout
    setTimeout(() => {
      chromaClient.healthCheck().then((ok) => {
        chromaAvailable = ok;
        api.logger.info(`[ontology-bridge] ChromaDB ${ok ? "reachable" : "unreachable"} at ${chromaUrl}`);
        if (!ok) {
          setTimeout(() => {
            chromaClient.healthCheck().then((r) => {
              chromaAvailable = r;
              api.logger.info(`[ontology-bridge] ChromaDB ${r ? "reachable" : "unreachable"} at ${chromaUrl} (retry)`);
            });
          }, 3000).unref();
        }
      });
    }, 2000).unref();

    // Periodic health re-check for ChromaDB (every 5 min)
    setInterval(() => {
      chromaClient.healthCheck().then((ok) => { chromaAvailable = ok; });
    }, 300_000).unref();

    // ── Hook 1: before_prompt_build (unified context injection) ──────
    api.on("before_prompt_build", async (event, ctx) => {
      const agentId = ctx.agentId || "main";
      if (skipAgents.has(agentId)) return {};

      const prompt = (event.prompt || "").trim();
      if (!prompt || prompt.length < 5) return {};
      if (prompt.startsWith("Read HEARTBEAT.md") && prompt.length < 200) return {};

      const startTime = Date.now();
      const contextParts: string[] = [];
      let totalTokens = 0;

      // ── Source 1: Ontology keyword match ──
      try {
        await cache.ensureFresh();

        if (cache.size > 0) {
          const matches = cache.findMatches(prompt);
          if (matches.length > 0) {
            const contextItems: MatchedContext[] = [];
            let ontologyTokens = 0;

            const fetchPromises = matches.map(async (match) => {
              const [entity, related] = await Promise.all([
                ontologyClient.getEntity(match.id),
                ontologyClient.getRelated(match.id, "both"),
              ]);
              return { match, entity, related };
            });

            const results = await Promise.race([
              Promise.all(fetchPromises),
              new Promise<null>((resolve) => setTimeout(() => resolve(null), 2000)),
            ]);

            if (results) {
              for (const r of results) {
                if (!r.entity) continue;

                const relations = r.related.map((rel) => ({
                  rel_type: rel.rel_type,
                  direction: (rel.direction === "outgoing" ? "outgoing" : "incoming") as "outgoing" | "incoming",
                  target: {
                    id: rel.entity.id,
                    type: rel.entity.type,
                    name:
                      (rel.entity.properties?.name as string) ||
                      (rel.entity.properties?.title as string) ||
                      rel.entity.id,
                  },
                }));

                const item: MatchedContext = { entity: r.entity, relations };
                const itemText = formatEntityContext([item]);
                const itemTokens = estimateTokens(itemText);
                if (ontologyTokens + itemTokens > TOKEN_BUDGET.ontology) break;

                contextItems.push(item);
                ontologyTokens += itemTokens;
              }

              if (contextItems.length > 0) {
                const text = formatEntityContext(contextItems);
                contextParts.push(text);
                totalTokens += estimateTokens(text);
              }
            }
          }
        }
      } catch (err) {
        api.logger.warn(`[ontology-bridge] Ontology match error: ${err}`);
      }

      // ── Source 2: ChromaDB semantic search ──
      if (chromaAvailable && totalTokens < maxContextTokens) {
        try {
          const messages = await chromaClient.query("user_messages", prompt, 3);
          const relevant = messages.filter((r) => (1 - r.distance) > 0.3);

          if (relevant.length > 0) {
            let semanticText = formatSemanticResults(relevant, "Relevant Memory");
            while (estimateTokens(semanticText) > TOKEN_BUDGET.semantic && relevant.length > 1) {
              relevant.pop();
              semanticText = formatSemanticResults(relevant, "Relevant Memory");
            }
            if (estimateTokens(semanticText) <= TOKEN_BUDGET.semantic) {
              contextParts.push(semanticText);
              totalTokens += estimateTokens(semanticText);
            }
          }
        } catch (err) {
          api.logger.warn(`[ontology-bridge] ChromaDB search error: ${err}`);
        }
      }

      // ── Source 3: Orchestrator recent messages ──
      if (totalTokens < maxContextTokens) {
        try {
          const recentMsgs = await orchestratorClient.getRecentMessages(recentMessagesCount);
          if (recentMsgs.length > 0) {
            let recentText = formatRecentMessages(recentMsgs);
            while (estimateTokens(recentText) > TOKEN_BUDGET.recent && recentMsgs.length > 2) {
              recentMsgs.shift();
              recentText = formatRecentMessages(recentMsgs);
            }
            if (estimateTokens(recentText) <= TOKEN_BUDGET.recent) {
              contextParts.push(recentText);
              totalTokens += estimateTokens(recentText);
            }
          }
        } catch (err) {
          api.logger.warn(`[ontology-bridge] Orchestrator fetch error: ${err}`);
        }
      }

      if (contextParts.length === 0) return {};

      const contextText = contextParts.join("\n\n");
      const elapsed = Date.now() - startTime;

      logBridge(logPath, {
        timestamp: new Date().toISOString(),
        type: "context_inject",
        agentId,
        sources: contextParts.length,
        tokenEstimate: estimateTokens(contextText),
        elapsedMs: elapsed,
      });

      api.logger.debug?.(
        `[ontology-bridge] Injected ${contextParts.length} sources (~${estimateTokens(contextText)} tokens) in ${elapsed}ms`,
      );

      return { prependContext: contextText };
    });

    // ── Hook 2: after_tool_call (track ontology usage) ──────────────
    api.on("after_tool_call", (event, ctx) => {
      const sessionKey = ctx.sessionKey ?? "default";
      if (
        event.toolName === "Bash" &&
        typeof event.params?.command === "string" &&
        event.params.command.includes(ontologyUrl)
      ) {
        sessionOntologyUsage.set(
          sessionKey,
          (sessionOntologyUsage.get(sessionKey) || 0) + 1,
        );
      }
    });

    // ── Hook 3: agent_end (fact extraction) ─────────────────────────
    api.on("agent_end", async (event, ctx) => {
      if (!extractionEnabled) return;
      const agentId = ctx.agentId || "main";
      if (skipAgents.has(agentId)) return;
      const sessionKey = ctx.sessionKey ?? "default";

      const ontologyUsageCount = sessionOntologyUsage.get(sessionKey) || 0;
      if (ontologyUsageCount > 2) {
        sessionOntologyUsage.delete(sessionKey);
        return;
      }
      sessionOntologyUsage.delete(sessionKey);

      const messages = event.messages || [];
      const assistantTexts: string[] = [];
      let userPrompt = "";

      for (const msg of messages) {
        const m = msg as { role?: string; content?: unknown };
        if (m.role === "user" && typeof m.content === "string") {
          userPrompt = m.content;
        }
        if (m.role === "assistant") {
          if (typeof m.content === "string") {
            assistantTexts.push(m.content);
          } else if (Array.isArray(m.content)) {
            for (const block of m.content) {
              if (typeof block === "object" && block !== null && (block as any).type === "text") {
                assistantTexts.push((block as any).text || "");
              }
            }
          }
        }
      }

      if (assistantTexts.length === 0 || !userPrompt) return;

      try {
        const facts = extractFacts(assistantTexts, userPrompt);
        if (facts.length === 0) return;

        for (const fact of facts) {
          if (fact.entityType === "Note" && fact.properties) {
            const created = await ontologyClient.createEntity("Note", {
              ...fact.properties,
              tags: [...((fact.properties.tags as string[]) || []), "auto-extracted"],
            });
            if (created) {
              api.logger.info(
                `[ontology-bridge] Auto-extracted ${fact.type}: ${fact.description.slice(0, 80)}`,
              );
              logBridge(logPath, {
                timestamp: new Date().toISOString(),
                type: "fact_extracted",
                factType: fact.type,
                entityId: created.id,
                description: fact.description.slice(0, 200),
                agentId,
              });
            }
          } else if (fact.entityType === "Preference" && fact.properties) {
            const created = await ontologyClient.createEntity("Preference", {
              ...fact.properties,
              tags: ["auto-extracted"],
            });
            if (created) {
              api.logger.info(
                `[ontology-bridge] Auto-extracted preference: ${fact.description.slice(0, 80)}`,
              );
            }
          }
        }
      } catch (err) {
        api.logger.warn(`[ontology-bridge] Fact extraction error: ${err}`);
      }
    });

    // ── Log startup ──
    const skipList = skipAgents.size > 0 ? ` | skip=[${[...skipAgents].join(",")}]` : "";
    api.logger.info(
      `[ontology-bridge] v2.0 registered | ontology=${ontologyUrl} | chroma=${chromaUrl} | ` +
        `orchestrator=${orchestratorUrl} | maxTokens=${maxContextTokens} | extraction=${extractionEnabled}${skipList}`,
    );
  },
};

export default plugin;
