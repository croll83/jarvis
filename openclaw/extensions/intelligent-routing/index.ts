/**
 * Intelligent Routing Plugin for OpenClaw
 * ========================================
 *
 * Intercepts every incoming message via the `before_model_resolve` hook,
 * classifies task complexity (SIMPLE / MEDIUM / COMPLEX / REASONING / CRITICAL),
 * and overrides the model for MEDIUM+ tasks. SIMPLE tasks stay on the gateway
 * default model (Haiku) — no override is applied.
 *
 * Classification uses a two-stage approach:
 *   1. Fast-path: keyword/pattern matching for obvious SIMPLE tasks (< 1ms)
 *   2. Full-path: calls intelligent-router-hook.js subprocess for nuanced classification
 *
 * All routing decisions are logged to state/routing-log.jsonl.
 */

import { execSync } from "child_process";
import { appendFileSync, existsSync, mkdirSync } from "fs";
import { dirname, resolve } from "path";

// ─── Types ──────────────────────────────────────────────────────────────────

interface PluginConfig {
  routerScriptPath?: string;
  routingLogPath?: string;
  classifierTimeoutMs?: number;
  enableFastPath?: boolean;
  dryRun?: boolean;
}

interface OpenClawPluginApi {
  id: string;
  name: string;
  config: Record<string, unknown>;
  pluginConfig?: PluginConfig;
  logger: {
    debug: (msg: string) => void;
    info: (msg: string) => void;
    warn: (msg: string) => void;
    error: (msg: string) => void;
  };
  registerHook: (
    hookName: string,
    callback: (ctx: HookContext) => HookResult | Promise<HookResult>,
  ) => void;
}

interface HookContext {
  /** The user's message text */
  message: string;
  /** Channel the message came from (telegram, discord, etc.) */
  channel?: string;
  /** Session/requester identifier */
  sessionId?: string;
  /** Agent id if routed to a specific agent */
  agentId?: string;
  /** Current default model configured on the gateway */
  defaultModel?: string;
}

interface HookResult {
  /** Override the model selection. undefined = keep default. */
  modelOverride?: string;
  /** Override thinking level (on, high, null) */
  thinkingOverride?: string | null;
  /** Additional metadata to attach to the request */
  metadata?: Record<string, unknown>;
}

interface RouterOutput {
  tier: string;
  model: string;
  modelAlias?: string;
  fallbacks: string[];
  thinking: string | null;
  confidence: number;
  handleDirectly?: boolean;
  classifierFailed?: boolean;
}

interface RoutingLogEntry {
  timestamp: string;
  source: string;
  agentId: string;
  taskDescription: string;
  tier: string;
  modelSelected: string;
  fallbacks: string[];
  confidence: number;
  executionTimeMs: number;
  success: boolean;
  classificationMethod: string;
  notes: string;
}

// ─── Constants ──────────────────────────────────────────────────────────────

const DEFAULT_ROUTER_SCRIPT =
  "/home/jarvis/.openclaw/workspace/skills/intelligent-router/intelligent-router-hook.js";
const DEFAULT_LOG_PATH =
  "/home/jarvis/.openclaw/workspace/state/routing-log.jsonl";
const DEFAULT_TIMEOUT_MS = 8000;

/**
 * Fast-path patterns for SIMPLE classification.
 * These are checked before spawning the classifier subprocess.
 */
const SIMPLE_EXACT = new Set([
  "ciao", "hello", "hi", "hey", "hola", "yo", "ok", "sì", "si", "no",
  "grazie", "thanks", "thx", "ty", "prego", "np",
  "buongiorno", "buonasera", "buonanotte",
  "good morning", "good night", "gn", "gm",
  "status", "ping", "test", "?", "!",
  "👍", "👎", "❤️", "😂", "🙏", "✅", "🔥",
]);

const SIMPLE_PREFIXES = [
  "che ore sono", "what time", "che giorno",
  "what day", "how are you", "come stai",
  "dimmi l'ora", "che tempo fa",
];

const SIMPLE_PATTERNS = [
  /^.{0,15}$/,                         // Very short messages (≤15 chars)
  /^(si|no|ok|va bene|perfetto|fatto|capito|esatto|giusto|bene|male)[\s!?.]*$/i,
  /^(yes|no|yep|nope|sure|right|correct|wrong|done|got it)[\s!?.]*$/i,
  /^(lol|lmao|haha|ahah|😂|🤣|💀)+[\s!?.]*$/i,
];

// ─── Helpers ────────────────────────────────────────────────────────────────

function resolveConfig(pluginConfig?: PluginConfig) {
  return {
    routerScriptPath: pluginConfig?.routerScriptPath || DEFAULT_ROUTER_SCRIPT,
    routingLogPath: pluginConfig?.routingLogPath || DEFAULT_LOG_PATH,
    classifierTimeoutMs: pluginConfig?.classifierTimeoutMs || DEFAULT_TIMEOUT_MS,
    enableFastPath: pluginConfig?.enableFastPath !== false,
    dryRun: pluginConfig?.dryRun === true,
  };
}

function isSimpleFastPath(message: string): boolean {
  const trimmed = message.trim();
  const lower = trimmed.toLowerCase();

  // Exact match
  if (SIMPLE_EXACT.has(lower)) return true;

  // Prefix match
  for (const prefix of SIMPLE_PREFIXES) {
    if (lower.startsWith(prefix)) return true;
  }

  // Pattern match
  for (const pattern of SIMPLE_PATTERNS) {
    if (pattern.test(trimmed)) return true;
  }

  return false;
}

function callClassifier(
  scriptPath: string,
  message: string,
  timeoutMs: number,
): RouterOutput | null {
  try {
    // Sanitize message for shell: escape special chars
    const sanitized = message
      .replace(/\\/g, "\\\\")
      .replace(/"/g, '\\"')
      .replace(/\$/g, "\\$")
      .replace(/`/g, "\\`");

    const output = execSync(
      `node "${scriptPath}" "${sanitized}"`,
      {
        encoding: "utf-8",
        timeout: timeoutMs,
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env },
      },
    );

    return JSON.parse(output.trim()) as RouterOutput;
  } catch (_err) {
    return null;
  }
}

function logDecision(
  logPath: string,
  entry: RoutingLogEntry,
): void {
  try {
    // Ensure parent directory exists
    const dir = dirname(logPath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    appendFileSync(logPath, JSON.stringify(entry) + "\n");
  } catch (_err) {
    // Logging should never break the pipeline — fail silently
  }
}

// ─── Plugin Entry Point ─────────────────────────────────────────────────────

export default function register(api: OpenClawPluginApi) {
  const config = resolveConfig(api.pluginConfig as PluginConfig | undefined);

  api.logger.info(
    `[intelligent-routing] Registering before_model_resolve hook` +
    ` | script=${config.routerScriptPath}` +
    ` | fastPath=${config.enableFastPath}` +
    ` | dryRun=${config.dryRun}`,
  );

  api.registerHook("before_model_resolve", (ctx: HookContext): HookResult => {
    const startTime = Date.now();
    const message = (ctx.message || "").trim();

    // Skip empty messages
    if (!message) {
      return {};
    }

    let tier = "SIMPLE";
    let model = "";
    let fallbacks: string[] = [];
    let confidence = 1.0;
    let thinking: string | null = null;
    let classificationMethod = "fast-path";
    let classifierFailed = false;

    // ── Stage 1: Fast-path keyword check ──
    const isSimple = config.enableFastPath && isSimpleFastPath(message);

    if (isSimple) {
      // Fast-path: SIMPLE, no override needed
      tier = "SIMPLE";
      confidence = 1.0;
      classificationMethod = "fast-path";
    } else {
      // ── Stage 2: Full classifier ──
      classificationMethod = "full-classifier";
      const result = callClassifier(
        config.routerScriptPath,
        message,
        config.classifierTimeoutMs,
      );

      if (result) {
        tier = result.tier;
        model = result.model;
        fallbacks = result.fallbacks || [];
        confidence = result.confidence;
        thinking = result.thinking;
        classifierFailed = result.classifierFailed === true;
      } else {
        // Classifier failed — fall back to SIMPLE (safe default, use Haiku)
        tier = "SIMPLE";
        confidence = 0;
        classificationMethod = "fallback-on-error";
        classifierFailed = true;
        api.logger.warn(
          `[intelligent-routing] Classifier failed/timeout, defaulting to SIMPLE`,
        );
      }
    }

    const executionTimeMs = Date.now() - startTime;
    const agentId = ctx.agentId || "main";

    // ── Log the decision ──
    logDecision(config.routingLogPath, {
      timestamp: new Date().toISOString(),
      source: "plugin:intelligent-routing",
      agentId,
      taskDescription: message.slice(0, 200),
      tier,
      modelSelected: tier === "SIMPLE" ? "(default)" : model,
      fallbacks,
      confidence,
      executionTimeMs,
      success: !classifierFailed,
      classificationMethod,
      notes: config.dryRun ? "dry-run" : "",
    });

    api.logger.debug(
      `[intelligent-routing] ${tier} (${classificationMethod}, ${confidence.toFixed(2)}) → ` +
      `${tier === "SIMPLE" ? "default" : model} [${executionTimeMs}ms]`,
    );

    // ── Build result ──
    // SIMPLE: no override — gateway default model (Haiku) handles it
    // MEDIUM+: override to the classified model
    if (tier === "SIMPLE" || config.dryRun) {
      return {};
    }

    const result: HookResult = {};

    if (model) {
      result.modelOverride = model;
    }

    if (thinking) {
      result.thinkingOverride = thinking;
    }

    result.metadata = {
      routingTier: tier,
      routingConfidence: confidence,
      routingMethod: classificationMethod,
    };

    return result;
  });

  api.logger.info(`[intelligent-routing] Hook registered successfully.`);
}
