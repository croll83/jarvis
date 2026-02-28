/**
 * Intelligent Routing Plugin for OpenClaw v2.1
 * =============================================
 *
 * Intercepts every incoming message via `before_model_resolve` hook,
 * classifies task complexity (SIMPLE / MEDIUM / COMPLEX / REASONING / CRITICAL),
 * and overrides the model for MEDIUM+ tasks.
 *
 * v2.2 additions:
 * - [force-model:provider/model] marker: bypass classification, force exact model
 * - [tier:TIER_NAME] marker: bypass classification, use tier's primary model
 * - Both markers are logged and stored in routing state for model awareness
 *
 * v2.1 fixes:
 * - modelOverride uses bare model name (e.g. "claude-sonnet-4-6"), NOT "anthropic/claude-sonnet-4-6"
 *   OpenClaw's resolveModel(provider, modelId) prepends the provider — sending "anthropic/claude-sonnet-4-6"
 *   caused "Unknown model: anthropic/anthropic/claude-sonnet-4-6" FailoverError
 * - providerOverride set alongside modelOverride
 * - Low-confidence guard + error fallback now also update model (was only updating tier → Haiku leak)
 * - Tier-to-model fallback map so upgraded tiers always get the right model
 * - Fast-path runs on cleaned prompt (Telegram envelope stripped)
 * - Dedup: skip re-routing if same prompt+session was already routed in this cycle
 *
 * v2.0 changes:
 * - Uses native OpenClaw plugin API (removed custom interfaces)
 * - Two hooks: before_model_resolve + before_prompt_build (model awareness)
 * - [routed] marker: skip routing for pre-routed subagent spawns
 * - Low-confidence / error fallback: MEDIUM (Sonnet), not SIMPLE (Haiku)
 * - Routing decisions stored in shared state for model awareness injection
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
import type { OpenClawPluginDefinition } from "openclaw/plugin-sdk";

// ─── Types ──────────────────────────────────────────────────────────────────

interface PluginConfig {
  routerScriptPath?: string;
  routingLogPath?: string;
  classifierTimeoutMs?: number;
  enableFastPath?: boolean;
  dryRun?: boolean;
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

interface RoutingDecision {
  tier: string;
  model: string;
  provider: string;
  confidence: number;
  method: string;
  skipped: boolean;
}

interface RoutingLogEntry {
  timestamp: string;
  source: string;
  agentId: string;
  taskDescription: string;
  tier: string;
  modelSelected: string;
  providerSelected: string;
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

// Marker prefix added by spawn-with-routing.js to skip re-routing
const ROUTED_MARKER = "[routed]";

// Low confidence threshold — below this, fall back to MEDIUM (not SIMPLE)
const LOW_CONFIDENCE_THRESHOLD = 0.30;

/**
 * Tier → primary model mapping.
 * Used when the tier is upgraded (low-confidence guard, error fallback)
 * so the model always matches the final tier.
 *
 * Format: { provider, model } — OpenClaw expects these SEPARATE:
 *   modelOverride = "claude-sonnet-4-6"   (bare model name)
 *   providerOverride = "anthropic"         (provider name)
 *
 * DO NOT use "anthropic/claude-sonnet-4-6" as modelOverride —
 * OpenClaw's resolveModel() prepends the provider again → double prefix error.
 */
const TIER_PRIMARY: Record<string, { provider: string; model: string; fullId: string }> = {
  MEDIUM:    { provider: "anthropic", model: "claude-sonnet-4-6",  fullId: "anthropic/claude-sonnet-4-6" },
  COMPLEX:   { provider: "anthropic", model: "claude-sonnet-4-6",  fullId: "anthropic/claude-sonnet-4-6" },
  REASONING: { provider: "anthropic", model: "claude-opus-4-6",    fullId: "anthropic/claude-opus-4-6" },
  CRITICAL:  { provider: "anthropic", model: "claude-opus-4-6",    fullId: "anthropic/claude-opus-4-6" },
};

/**
 * Split a "provider/model" ID into separate parts.
 * E.g. "anthropic/claude-sonnet-4-6" → { provider: "anthropic", model: "claude-sonnet-4-6" }
 * E.g. "claude-sonnet-4-6" (no slash)  → { provider: "", model: "claude-sonnet-4-6" }
 */
function splitModelId(fullId: string): { provider: string; model: string } {
  const slashIdx = fullId.indexOf("/");
  if (slashIdx < 0) {
    return { provider: "", model: fullId };
  }
  return {
    provider: fullId.slice(0, slashIdx),
    model: fullId.slice(slashIdx + 1),
  };
}

/**
 * Fast-path patterns for SIMPLE classification.
 * These are checked before spawning the classifier subprocess.
 */
const SIMPLE_EXACT = new Set([
  "ciao", "hello", "hi", "hey", "hola", "yo", "ok", "si", "no",
  "grazie", "thanks", "thx", "ty", "prego", "np",
  "buongiorno", "buonasera", "buonanotte",
  "good morning", "good night", "gn", "gm",
  "status", "ping", "test", "?", "!",
  "\uD83D\uDC4D", "\uD83D\uDC4E", "\u2764\uFE0F", "\uD83D\uDE02", "\uD83D\uDE4F", "\u2705", "\uD83D\uDD25",
]);

const SIMPLE_PREFIXES = [
  "che ore sono", "what time", "che giorno",
  "what day", "how are you", "come stai",
  "dimmi l'ora", "che tempo fa",
];

const SIMPLE_PATTERNS = [
  /^.{0,15}$/,                         // Very short messages (<=15 chars)
  /^(si|no|ok|va bene|perfetto|fatto|capito|esatto|giusto|bene|male)[\s!?.]*$/i,
  /^(yes|no|yep|nope|sure|right|correct|wrong|done|got it)[\s!?.]*$/i,
  /^(lol|lmao|haha|ahah)+[\s!?.]*$/i,
  // Domotica: "accendi/spegni/alza/abbassa/apri/chiudi + qualcosa"
  /^(accendi|spegni|alza|abbassa|apri|chiudi|accendimi|spegnimi)\s+.+$/i,
  /^(turn on|turn off|switch on|switch off|open|close|dim)\s+.+$/i,
  // Domotica with "la/le/il/i" articles: "accendi la luce del soggiorno"
  /^(accendi|spegni|alza|abbassa|apri|chiudi)\s+(la|le|il|i|lo|gli|l')\s+.+$/i,
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

/**
 * Strip Telegram/platform envelope from the prompt.
 * Removes fenced JSON metadata blocks, "Conversation info" sections,
 * and bare key:value Telegram metadata lines.
 * Returns the clean user message.
 */
function stripEnvelope(text: string): string {
  let s = text;

  // Remove ```json ... ``` fenced blocks containing Telegram metadata
  s = s.replace(/```json[\s\S]*?```/g, (block) => {
    if (/message_id|sender_name|chat_id|from_user_id|reply_to_message_id|timestamp/i.test(block)) {
      return "";
    }
    return block;
  });

  // Remove "Conversation info (untrusted metadata)" sections
  s = s.replace(/Conversation info \(untrusted[^)]*\)[\s\S]*?(?=\n\n|\n[A-Z][a-z]|$)/gi, "");

  // Remove "Replied message (untrusted, for context)" sections
  s = s.replace(/Replied message \(untrusted[^)]*\)[\s\S]*?(?=\n\n|\n[A-Z][a-z]|$)/gi, "");

  // Remove bare Telegram metadata lines
  s = s.replace(/^\s*(message_id|sender_name|sender_id|chat_id|chat_title|from_user_id|reply_to_message_id|timestamp|platform)\s*:.*$/gim, "");

  // Collapse multiple blank lines
  s = s.replace(/\n{3,}/g, "\n\n");

  return s.trim();
}

function isSimpleFastPath(message: string): boolean {
  const trimmed = message.trim();
  const lower = trimmed.toLowerCase();

  if (SIMPLE_EXACT.has(lower)) return true;

  for (const prefix of SIMPLE_PREFIXES) {
    if (lower.startsWith(prefix)) return true;
  }

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
    const dir = dirname(logPath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    appendFileSync(logPath, JSON.stringify(entry) + "\n");
  } catch (_err) {
    // Logging should never break the pipeline
  }
}

// ─── Plugin Entry Point ─────────────────────────────────────────────────────

const plugin: OpenClawPluginDefinition = {
  id: "intelligent-routing",
  name: "Intelligent Routing",
  version: "2.2.0",
  description: "Classifies task complexity and routes to the appropriate model",

  register(api) {
    const config = resolveConfig(api.pluginConfig as PluginConfig | undefined);

    // Shared routing state between hooks (keyed by sessionKey)
    const routingState = new Map<string, RoutingDecision>();

    // Dedup: track last routed prompt per session to avoid duplicate log entries
    // when OpenClaw retries after failover errors or calls the hook multiple times
    const lastRoutedPrompt = new Map<string, string>();

    api.logger.info(
      `[intelligent-routing] Registering hooks v2.2` +
      ` | script=${config.routerScriptPath}` +
      ` | fastPath=${config.enableFastPath}` +
      ` | dryRun=${config.dryRun}`,
    );

    // ── Hook 1: before_model_resolve ──────────────────────────────────
    // Classifies the task and overrides the model for MEDIUM+ tasks.
    // Skips routing for [routed] subagent prompts.
    api.on("before_model_resolve", (event, ctx) => {
      const startTime = Date.now();
      const rawPrompt = (event.prompt || "").trim();
      const sessionKey = ctx.sessionKey ?? "default";

      // Skip empty messages
      if (!rawPrompt) {
        return {};
      }

      // ── Check [routed] marker ──
      // spawn-with-routing.js adds this to prevent re-routing subagent spawns
      if (rawPrompt.startsWith(ROUTED_MARKER)) {
        api.logger.debug(
          `[intelligent-routing] Skipping pre-routed subagent prompt`,
        );
        routingState.set(sessionKey, {
          tier: "ROUTED",
          model: "(pre-routed)",
          provider: "",
          confidence: 1.0,
          method: "skip-routed-marker",
          skipped: true,
        });
        return {};
      }

      // ── Check [force-model:X] marker ──
      // Bypasses classification entirely, forces the specified model.
      // Accepts "provider/model" or bare "model" formats.
      const forceModelMatch = rawPrompt.match(/^\[force-model:([^\]]+)\]/);
      if (forceModelMatch) {
        const requested = forceModelMatch[1].trim();
        const split = splitModelId(requested);
        const agentId = ctx.agentId || "main";
        const messageSource = ctx.messageProvider || ctx.source || "unknown";

        api.logger.debug(
          `[intelligent-routing] Force-model override: ${requested}`,
        );

        routingState.set(sessionKey, {
          tier: "FORCED",
          model: split.model,
          provider: split.provider,
          confidence: 1.0,
          method: "force-model",
          skipped: false,
        });
        lastRoutedPrompt.set(sessionKey, rawPrompt);

        logDecision(config.routingLogPath, {
          timestamp: new Date().toISOString(),
          source: messageSource,
          agentId,
          taskDescription: rawPrompt.slice(0, 200),
          tier: "FORCED",
          modelSelected: requested,
          providerSelected: split.provider,
          fallbacks: [],
          confidence: 1.0,
          executionTimeMs: Date.now() - startTime,
          success: true,
          classificationMethod: "force-model",
          notes: "",
        });

        if (!split.model) return {};
        return {
          modelOverride: split.model,
          providerOverride: split.provider || undefined,
        };
      }

      // ── Check [tier:X] marker ──
      // Bypasses classification, uses the tier's primary model from TIER_PRIMARY map.
      // Accepts: SIMPLE, MEDIUM, COMPLEX, REASONING, CRITICAL (case-insensitive).
      const tierMatch = rawPrompt.match(/^\[tier:([^\]]+)\]/);
      if (tierMatch) {
        const requestedTier = tierMatch[1].trim().toUpperCase();
        const agentId = ctx.agentId || "main";
        const messageSource = ctx.messageProvider || ctx.source || "unknown";

        api.logger.debug(
          `[intelligent-routing] Tier override: ${requestedTier}`,
        );

        if (requestedTier === "SIMPLE") {
          routingState.set(sessionKey, {
            tier: "SIMPLE",
            model: "(default)",
            provider: "",
            confidence: 1.0,
            method: "tier-override",
            skipped: false,
          });
          lastRoutedPrompt.set(sessionKey, rawPrompt);

          logDecision(config.routingLogPath, {
            timestamp: new Date().toISOString(),
            source: messageSource,
            agentId,
            taskDescription: rawPrompt.slice(0, 200),
            tier: "SIMPLE",
            modelSelected: "(default)",
            providerSelected: "(default)",
            fallbacks: [],
            confidence: 1.0,
            executionTimeMs: Date.now() - startTime,
            success: true,
            classificationMethod: "tier-override",
            notes: "",
          });
          return {};
        }

        const tierModel = TIER_PRIMARY[requestedTier];
        if (tierModel) {
          routingState.set(sessionKey, {
            tier: requestedTier,
            model: tierModel.model,
            provider: tierModel.provider,
            confidence: 1.0,
            method: "tier-override",
            skipped: false,
          });
          lastRoutedPrompt.set(sessionKey, rawPrompt);

          logDecision(config.routingLogPath, {
            timestamp: new Date().toISOString(),
            source: messageSource,
            agentId,
            taskDescription: rawPrompt.slice(0, 200),
            tier: requestedTier,
            modelSelected: tierModel.fullId,
            providerSelected: tierModel.provider,
            fallbacks: [],
            confidence: 1.0,
            executionTimeMs: Date.now() - startTime,
            success: true,
            classificationMethod: "tier-override",
            notes: "",
          });

          return {
            modelOverride: tierModel.model,
            providerOverride: tierModel.provider,
          };
        }

        // Unknown tier — fall through to normal classification
        api.logger.warn(
          `[intelligent-routing] Unknown tier override "${requestedTier}", falling through to classifier`,
        );
      }

      // ── Dedup: if same prompt was already routed in this session, return cached result ──
      const prevPrompt = lastRoutedPrompt.get(sessionKey);
      const prevDecision = routingState.get(sessionKey);
      if (prevPrompt === rawPrompt && prevDecision && !prevDecision.skipped) {
        api.logger.debug(
          `[intelligent-routing] Dedup: same prompt already routed → ${prevDecision.tier} (${prevDecision.model})`,
        );
        if (prevDecision.tier === "SIMPLE") {
          return {};
        }
        if (prevDecision.model) {
          return {
            modelOverride: prevDecision.model,
            providerOverride: prevDecision.provider || undefined,
          };
        }
        return {};
      }

      // ── Strip Telegram/platform envelope for classification ──
      const cleanPrompt = stripEnvelope(rawPrompt);

      let tier = "SIMPLE";
      let modelPart = "";    // bare model name (e.g. "claude-sonnet-4-6")
      let providerPart = ""; // provider (e.g. "anthropic")
      let fullModelId = "";  // full ID for logging (e.g. "anthropic/claude-sonnet-4-6")
      let fallbacks: string[] = [];
      let confidence = 1.0;
      let classificationMethod = "fast-path";
      let classifierFailed = false;

      // ── Stage 1: Fast-path keyword check (on cleaned prompt) ──
      const isSimple = config.enableFastPath && isSimpleFastPath(cleanPrompt);

      if (isSimple) {
        tier = "SIMPLE";
        confidence = 1.0;
        classificationMethod = "fast-path";
      } else {
        // ── Stage 2: Full classifier ──
        // Pass cleanPrompt (envelope stripped) — the raw prompt contains Telegram
        // metadata with JSON/backticks that inflate code_presence + output_format scores
        classificationMethod = "full-classifier";
        const result = callClassifier(
          config.routerScriptPath,
          cleanPrompt,
          config.classifierTimeoutMs,
        );

        if (result) {
          tier = result.tier;
          fullModelId = result.model;
          const split = splitModelId(result.model);
          modelPart = split.model;
          providerPart = split.provider;
          fallbacks = result.fallbacks || [];
          confidence = result.confidence;
          classifierFailed = result.classifierFailed === true;
        } else {
          // Classifier failed — fall back to MEDIUM (Sonnet), NOT SIMPLE (Haiku)
          tier = "MEDIUM";
          confidence = 0;
          classificationMethod = "fallback-on-error";
          classifierFailed = true;
          // Set model to MEDIUM primary
          const mp = TIER_PRIMARY.MEDIUM;
          modelPart = mp.model;
          providerPart = mp.provider;
          fullModelId = mp.fullId;
          api.logger.warn(
            `[intelligent-routing] Classifier failed/timeout, defaulting to MEDIUM (${mp.fullId})`,
          );
        }

        // ── Low-confidence guard ──
        // If classifier is uncertain about a MEDIUM/COMPLEX task, upgrade to MEDIUM (Sonnet).
        // SIMPLE is exempt — low score always produces low confidence via sigmoid,
        // but that means "certainly simple", not "uncertain classification".
        // CRITICAL and REASONING are exempt — false negatives there are costly.
        if (
          confidence > 0 &&
          confidence < LOW_CONFIDENCE_THRESHOLD &&
          tier !== "SIMPLE" &&
          tier !== "CRITICAL" &&
          tier !== "REASONING"
        ) {
          const oldTier = tier;
          tier = "MEDIUM";
          // Also upgrade the model to match the new tier
          const mp = TIER_PRIMARY.MEDIUM;
          modelPart = mp.model;
          providerPart = mp.provider;
          fullModelId = mp.fullId;
          api.logger.debug(
            `[intelligent-routing] Low confidence (${confidence.toFixed(2)}) for tier ${oldTier}, upgrading to MEDIUM (${mp.fullId})`,
          );
        }
      }

      const executionTimeMs = Date.now() - startTime;
      const agentId = ctx.agentId || "main";

      // ── Save routing decision for model awareness (Hook 2) + dedup ──
      routingState.set(sessionKey, {
        tier,
        model: tier === "SIMPLE" ? "(default)" : modelPart,
        provider: tier === "SIMPLE" ? "" : providerPart,
        confidence,
        method: classificationMethod,
        skipped: false,
      });
      lastRoutedPrompt.set(sessionKey, rawPrompt);

      // ── Determine message source (telegram, whatsapp, webchat, etc.) ──
      const messageSource = ctx.messageProvider || ctx.source || "unknown";

      // ── Log the decision ──
      logDecision(config.routingLogPath, {
        timestamp: new Date().toISOString(),
        source: messageSource,
        agentId,
        taskDescription: cleanPrompt.slice(0, 200),
        tier,
        modelSelected: tier === "SIMPLE" ? "(default)" : fullModelId,
        providerSelected: tier === "SIMPLE" ? "(default)" : providerPart,
        fallbacks,
        confidence,
        executionTimeMs,
        success: !classifierFailed,
        classificationMethod,
        notes: config.dryRun ? "dry-run" : "",
      });

      api.logger.debug(
        `[intelligent-routing] ${tier} (${classificationMethod}, ${confidence.toFixed(2)}) -> ` +
        `${tier === "SIMPLE" ? "default" : `${providerPart}/${modelPart}`} [${executionTimeMs}ms]`,
      );

      // ── Build result ──
      // SIMPLE: no override — gateway default model handles it
      // MEDIUM+: override to the classified model (bare name + provider separate)
      if (tier === "SIMPLE" || config.dryRun) {
        return {};
      }

      if (modelPart) {
        return {
          modelOverride: modelPart,
          providerOverride: providerPart || undefined,
        };
      }

      return {};
    });

    // ── Hook 2: before_prompt_build ──────────────────────────────────
    // Injects model awareness into the prompt so the agent knows
    // which model it's actually running on after the classifier override.
    api.on("before_prompt_build", (event, ctx) => {
      const sessionKey = ctx.sessionKey ?? "default";
      const routing = routingState.get(sessionKey);

      if (!routing || routing.skipped) {
        return {};
      }

      // Don't inject for SIMPLE tier (no override, agent uses default)
      if (routing.tier === "SIMPLE") {
        return {};
      }

      const fullModel = routing.provider
        ? `${routing.provider}/${routing.model}`
        : routing.model;

      const prependContext = [
        `[Routing Info]`,
        `Tier: ${routing.tier}`,
        `Model: ${fullModel}`,
        `Confidence: ${(routing.confidence * 100).toFixed(0)}%`,
        `Method: ${routing.method}`,
      ].join(" | ");

      return { prependContext };
    });

    api.logger.info(`[intelligent-routing] Hooks registered successfully (before_model_resolve + before_prompt_build).`);
  },
};

export default plugin;
