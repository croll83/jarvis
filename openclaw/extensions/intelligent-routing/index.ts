/**
 * Intelligent Routing Plugin for OpenClaw v4.0
 * =============================================
 *
 * Classifies task complexity and routes to the appropriate model.
 *
 * v4.0 changes:
 * - INLINE CLASSIFIER: Full 15-dimension weighted scorer in TypeScript.
 *   Eliminates the subprocess chain (index.ts -> hook.js -> router.py).
 *   Classification latency: <1ms (was ~50ms with subprocess).
 * - BOUNDARY-DISTANCE CONFIDENCE: Sigmoid calibrated on distance from the
 *   nearest tier boundary, not on the raw score. Inspired by ClawRouter.
 *   Result: confidence is a real measure of certainty, not complexity.
 * - NEGATIVE SIMPLE SCORES: Simple indicators push the score DOWN (-1.0),
 *   actively pulling trivial tasks toward SIMPLE tier.
 * - LONG CONTEXT OVERRIDE: Prompts exceeding MAX_TOKENS_FORCE_COMPLEX
 *   bypass classification and go straight to COMPLEX.
 * - Bilingual keywords (Italian + English) ported from router.py v3.2.
 * - Complexity boosters and CRITICAL keyword overrides preserved.
 *
 * v3.0: Removed thinkingOverride (OpenClaw handles thinking natively).
 * v2.2: [force-model:X], [tier:X] markers.
 * v2.1: Split modelOverride/providerOverride, low-confidence guard, dedup.
 * v2.0: Native OpenClaw plugin API, [routed] marker, model awareness.
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync } from "fs";
import { dirname, join } from "path";
import type { OpenClawPluginDefinition } from "openclaw/plugin-sdk";

// ─── Types ──────────────────────────────────────────────────────────────────

interface PluginConfig {
  routingLogPath?: string;
  enableFastPath?: boolean;
  dryRun?: boolean;
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
  thinking: string;
  fallbacks: string[];
  confidence: number;
  executionTimeMs: number;
  success: boolean;
  classificationMethod: string;
  notes: string;
}

interface ClassificationResult {
  tier: string;
  confidence: number;
  score: number;
  method: string;
  signals: string[];
}

// ─── Inline Classifier: Keywords (Bilingual IT/EN) ──────────────────────────

const REASONING_KEYWORDS = [
  // English
  "prove", "theorem", "proof", "derive", "derivation", "formal",
  "verify", "verification", "logic", "logical", "induction", "deduction",
  "lemma", "corollary", "axiom", "postulate", "qed", "step by step",
  "show that", "demonstrate that", "mathematically", "rigorously",
  "reason about", "reasoning", "think through", "think step",
  // Italiano
  "dimostra", "dimostrazione", "teorema", "prova", "derivare", "derivazione",
  "formale", "verifica", "verificare", "logica", "logico", "induzione",
  "deduzione", "assioma", "postulato", "passo dopo passo", "passo passo",
  "dimostra che", "mostra che", "matematicamente", "rigorosamente",
  "ragiona", "ragionamento", "ragiona su", "ragionaci", "pensaci",
  "analisi logica", "ragiona sulla", "ragiona sul",
];

const CODE_KEYWORDS = [
  // English
  "lint", "refactor", "bug fix", "code review", "software", "application",
  "component", "module", "package", "library", "frontend", "backend",
  "fullstack", "codebase", "repository", "repo", "commit", "merge",
  "pull request", "branch", "git", "ci/cd", "pipeline", "unit test",
  "test suite", "coverage", "linter", "formatter", "transpile",
  // Italiano
  "codice", "correggere", "correzione", "errore", "errori", "bug",
  "refactoring", "revisione codice", "applicazione", "componente",
  "modulo", "pacchetto", "libreria", "sviluppo", "sviluppare",
  "programma", "programmazione", "script", "funzione", "classe",
  "metodo", "variabile", "database", "query", "interfaccia",
  "implementa", "implementare", "implementazione",
];

const CODE_PATTERNS = [
  /`[^`]+`/,              // inline code
  /```[\s\S]*?```/,       // code blocks
  /\bdef\b/, /\bclass\b/, /\bimport\b/, /\bfrom\b/,
  /\breturn\b/, /\bif\b.*:\s*$/, /\.py\b/, /\.js\b/, /\.java\b/,
  /\.cpp\b/, /\.rs\b/, /\.go\b/, /\.ts\b/, /\.tsx\b/, /\.jsx\b/,
  /\bAPI\b/, /\bJSON\b/, /\bSQL\b/, /\bHTML\b/, /\bCSS\b/,
  /\b(python|javascript|java|rust|golang|c\+\+|typescript|ruby|php|bash|shell)\s+\w+/i,
  /\bwrite\s+.*?(function|code|script|class|method|program)/i,
  /\bscrivi\s+.*?(funzione|codice|script|classe|metodo|programma)/i,
  /\bcode\s+(for|to|that)/i,
  /\bcodice\s+(per|che|di)/i,
  /\bprogram(ming)?\b/i,
  /\b(coding|development|implementation)\b/i,
  /\b(sviluppo|implementazione)\b/i,
  /[{}\[\]();]/,          // code punctuation clusters
  /\b\w+\(\)/,            // function calls like foo()
  /->\s*\w+/,             // arrow notation
  /\b(npm|pip|cargo|yarn|brew|apt)\b/i,
];

const AGENTIC_KEYWORDS = [
  // English
  "run", "test", "fix", "deploy", "edit", "build", "create", "implement",
  "execute", "refactor", "migrate", "integrate", "setup", "configure",
  "install", "compile", "debug", "troubleshoot", "automate", "scaffold",
  "generate", "provision", "orchestrate", "spawn", "launch",
  // Italiano
  "esegui", "testa", "testare", "correggi", "correggere", "deploya",
  "modifica", "modificare", "costruisci", "costruire", "crea", "creare",
  "implementa", "implementare", "configura", "configurare", "installa",
  "installare", "compila", "compilare", "debugga", "debuggare",
  "automatizza", "automatizzare", "genera", "generare", "lancia",
  "lanciare", "avvia", "avviare", "fai partire", "metti su",
  "sistema", "aggiusta", "aggiustare", "ripara", "riparare",
  "scrivi", "scrivere", "riscrivi", "riscrivere", "rifai", "rifare",
];

const MULTI_STEP_PATTERNS = [
  // English
  /\bfirst\b.*\bthen\b/i, /\bstep\s+\d+/i, /\d+\.\s+\w+/,
  /\bnext\b/i, /\bafter\s+that\b/i, /\bfinally\b/i, /\bsubsequently\b/i,
  /\band then\b/i, /\bfollowed by\b/i, /,\s*then\b/i, /\bthen\s+\w+\s+it\b/i,
  /\bmulti[- ]?step\b/i, /\bmultiple\s+steps?\b/i,
  // Italiano
  /\bprima\b.*\bpoi\b/i, /\bpasso\s+\d+/i, /\bfase\s+\d+/i,
  /\bsuccessivamente\b/i, /\bdopo\s+di\s+che\b/i, /\binfine\b/i,
  /\be\s+poi\b/i, /\bquindi\b/i, /\bdopodiché\b/i, /\bin\s+seguito\b/i,
  /\bper\s+prima\s+cosa\b/i, /\bcome\s+primo\b/i, /\bcome\s+secondo\b/i,
  /\bprima\s+di\s+tutto\b/i, /\balla\s+fine\b/i,
  /\b\d+\)\s+\w+/,
];

const SIMPLE_INDICATORS = [
  // English
  "check", "get", "fetch", "show", "display", "status",
  "what is", "how much", "tell me", "find", "search",
  "hello", "hi", "thanks", "thank you", "ok", "yes", "no",
  // Italiano
  "controlla", "prendi", "mostra", "mostrami", "stato",
  "cos'è", "cosa è", "quanto", "dimmi", "trova", "cerca",
  "ciao", "grazie", "ok", "sì", "va bene", "perfetto",
  "che ore sono", "com'è", "come va",
  // Domotica / Home automation
  "accendi", "spegni", "alza", "abbassa", "apri", "chiudi",
  "luce", "luci", "lampada", "tapparella", "tapparelle",
  "serranda", "serrande", "persiana", "persiane",
  "condizionatore", "climatizzatore", "riscaldamento",
  "termostato", "temperatura", "ventilatore",
  "turn on", "turn off", "switch on", "switch off",
  "open", "close", "dim", "brighten",
  "light", "lights", "lamp", "blind", "blinds",
  "shutter", "shutters", "thermostat", "heater", "fan",
  "soggiorno", "camera", "cucina", "bagno", "corridoio",
  "garage", "giardino", "terrazzo", "cantina",
];

const TECHNICAL_TERMS = [
  // Universal / English
  "algorithm", "architecture", "optimization", "performance", "scalability",
  "database", "security", "authentication", "encryption", "protocol",
  "framework", "library", "dependency", "middleware", "endpoint",
  "microservice", "container", "docker", "kubernetes", "pipeline",
  "concurrency", "parallelism", "distributed", "latency", "throughput",
  "caching", "load balancer", "reverse proxy", "webhook", "websocket",
  "machine learning", "neural network", "deep learning", "transformer",
  "blockchain", "smart contract", "consensus", "defi", "token",
  // Italiano
  "algoritmo", "architettura", "ottimizzazione", "prestazioni",
  "scalabilità", "sicurezza", "autenticazione", "crittografia",
  "protocollo", "dipendenza", "dipendenze", "infrastruttura",
  "concorrenza", "parallelismo", "distribuito", "latenza",
  "apprendimento automatico", "rete neurale", "contratto intelligente",
];

const CREATIVE_MARKERS = [
  // English
  "creative", "imaginative", "story", "poem", "narrative", "write a",
  "compose", "brainstorm", "innovative", "original", "artistic",
  "fiction", "character", "plot", "dialogue", "screenplay",
  // Italiano
  "creativo", "creativa", "immaginativo", "storia", "poesia", "racconto",
  "scrivi un", "scrivi una", "componi", "brainstorming", "innovativo",
  "originale", "artistico", "narrativa", "personaggio", "trama",
  "dialogo", "sceneggiatura", "inventare", "inventa",
];

const IMPERATIVE_VERBS = [
  // English
  "analyze", "evaluate", "compare", "assess", "investigate", "examine",
  "review", "validate", "verify", "optimize", "improve", "enhance",
  "design", "plan", "architect", "strategize", "diagnose",
  // Italiano
  "analizza", "analizzare", "analisi", "valuta", "valutare", "valutazione",
  "confronta", "confrontare", "confronto", "esamina", "esaminare",
  "rivedi", "rivedere", "revisione", "ottimizza", "ottimizzare",
  "migliora", "migliorare", "miglioramento", "progetta", "progettare",
  "pianifica", "pianificare", "strategia", "diagnostica", "diagnosticare",
  "approfondisci", "approfondire", "studia", "studiare", "indaga",
  "indagare", "verifica", "verificare",
  "riassumi", "riassumere", "elenca", "elencare",
  "summarize", "list", "describe", "explain",
  "spiega", "spiegare", "descrivi", "descrivere",
  "riscrivi", "riscrivere", "supporta", "supportare",
];

const CONSTRAINT_KEYWORDS = [
  // English
  "must", "should", "require", "need", "constraint", "limit", "restriction",
  "only", "exactly", "precisely", "specifically", "without", "except",
  "mandatory", "forbidden", "prohibited",
  // Italiano
  "deve", "dovrebbe", "richiede", "necessario", "vincolo", "limite",
  "restrizione", "solo", "esattamente", "precisamente", "specificamente",
  "senza", "eccetto", "tranne", "obbligatorio", "vietato", "proibito",
  "non deve", "assicurati", "assicurarsi", "fondamentale", "critico",
  "importante che", "necessariamente",
];

const CRITICAL_KEYWORDS = [
  // English
  "security audit", "security analysis", "vulnerability", "penetration test",
  "pentest", "exploit", "attack vector", "threat model", "zero day",
  "production deploy", "production release", "incident response",
  "data breach", "compliance", "gdpr", "sox", "hipaa",
  // Italiano
  "audit sicurezza", "analisi sicurezza", "analizza la sicurezza",
  "analizzare la sicurezza", "vulnerabilità", "test penetrazione",
  "vettore attacco", "modello minacce", "risposta incidente",
  "violazione dati", "conformità", "sicurezza del sistema",
  "sicurezza di", "rischio sicurezza", "falla di sicurezza",
];

const COMPLEXITY_BOOSTERS = [
  // English
  "step by step", "in detail", "detailed analysis", "deep dive",
  "comprehensive", "thorough", "end to end", "from scratch",
  "production ready", "production-ready", "best practices",
  "trade-offs", "trade offs", "pros and cons", "edge cases",
  "error handling", "security audit", "code review",
  "system design", "architecture design", "database design",
  "full stack", "full-stack", "ci/cd", "devops",
  "performance tuning", "memory leak", "race condition",
  "deadlock", "scalability", "high availability",
  "disaster recovery", "load testing", "stress testing",
  "distributed system", "distributed caching", "load balancing",
  // Italiano
  "passo dopo passo", "passo passo", "nel dettaglio", "in dettaglio",
  "analisi dettagliata", "analisi approfondita", "approfondimento",
  "completo", "completa", "esaustivo", "esaustiva",
  "da zero", "da capo", "da scratch", "produzione",
  "best practice", "migliori pratiche", "pro e contro",
  "casi limite", "casi edge", "gestione errori",
  "audit sicurezza", "analisi sicurezza", "revisione codice",
  "design sistema", "architettura sistema", "design database",
  "debug complesso", "debug avanzato", "problema complesso",
  "ottimizzazione performance", "memory leak", "race condition",
  "alta disponibilità", "disaster recovery", "test di carico",
  "ragiona sulla strategia", "analisi strategica",
  "multi-step", "multi step", "più passaggi", "vari passaggi",
  "classificatore", "classificazione",
  "classifica male", "funziona male", "non funziona",
  "debug il", "debugga il", "debug del", "debug della",
  "supportare italiano", "supportare inglese", "bilingue",
  "sistema distribuito", "caching distribuito", "bilanciamento carico",
  "progettazione sistema",
];

const OUTPUT_FORMAT_PATTERNS = [
  /\bjson\b/i, /\btable\b/i, /\btabella\b/i, /\blist\b/i, /\blista\b/i,
  /\belenco\b/i, /\bmarkdown\b/i, /\bformat\b/i, /\bformato\b/i,
  /\bcsv\b/i, /\byaml\b/i, /\bxml\b/i, /\bschema\b/i,
];

const REFERENCE_PATTERNS = [
  /\bthe\s+\w+\s+(?:above|below|mentioned|previous)\b/i,
  /\bthis\s+\w+\b/i,
  /\bquello\s+(?:sopra|sotto|precedente|menzionato)\b/i,
  /\bquest[oa]\s+\w+\b/i,
  /\bil\s+\w+\s+(?:sopra|sotto|precedente)\b/i,
];

const NEGATION_PATTERNS = [
  /\bnot\b/i, /\bno\b/i, /\bnever\b/i, /\bwithout\b/i, /\bexcept\b/i,
  /\bnon\b/i, /\bmai\b/i, /\bsenza\b/i, /\beccetto\b/i, /\btranne\b/i,
  /\bnessun[oa]?\b/i, /\bniente\b/i,
];

// ─── Inline Classifier: Scoring Config ──────────────────────────────────────

const SCORING_WEIGHTS: Record<string, number> = {
  reasoning_markers: 0.18,
  code_presence: 0.15,
  multi_step_patterns: 0.12,
  agentic_task: 0.10,
  technical_terms: 0.10,
  token_count: 0.08,
  creative_markers: 0.05,
  question_complexity: 0.05,
  constraint_count: 0.04,
  imperative_verbs: 0.03,
  output_format: 0.03,
  simple_indicators: 0.02,
  domain_specificity: 0.02,
  reference_complexity: 0.02,
  negation_complexity: 0.01,
};

// Tier boundaries on the weighted score axis.
// Matches Python v3.2 thresholds.
const TIER_BOUNDARIES = {
  simpleMedium: 0.05,
  mediumComplex: 0.30,
  complexCritical: 0.50,
};

// Sigmoid steepness for boundary-distance confidence calibration.
// 12 maps: distance 0.01 -> ~53%, 0.05 -> ~65%, 0.10 -> ~77%, 0.15 -> ~86%
const CONFIDENCE_STEEPNESS = 12;

// Prompts exceeding this estimated token count bypass classification -> COMPLEX.
const MAX_TOKENS_FORCE_COMPLEX = 100_000;

// ─── Inline Classifier: Scoring Functions ───────────────────────────────────

function countKeywordMatches(text: string, keywords: string[]): number {
  const lower = text.toLowerCase();
  let count = 0;
  for (const kw of keywords) {
    if (lower.includes(kw.toLowerCase())) {
      count++;
    }
  }
  return count;
}

function countPatternMatches(text: string, patterns: RegExp[]): number {
  let count = 0;
  for (const p of patterns) {
    const matches = text.match(new RegExp(p.source, p.flags + (p.flags.includes("g") ? "" : "g")));
    if (matches) count += matches.length;
  }
  return count;
}

function scoreDimensions(text: string): Record<string, number> {
  const scores: Record<string, number> = {};

  // 1. Reasoning markers (0.18)
  const reasoningCount = countKeywordMatches(text, REASONING_KEYWORDS);
  scores.reasoning_markers = Math.min(reasoningCount / 2.0, 1.0);

  // 2. Code presence (0.15)
  const codeCount = countPatternMatches(text, CODE_PATTERNS) + countKeywordMatches(text, CODE_KEYWORDS);
  scores.code_presence = Math.min(codeCount / 3.0, 1.0);

  // 3. Multi-step patterns (0.12)
  const multiStepCount = countPatternMatches(text, MULTI_STEP_PATTERNS);
  scores.multi_step_patterns = Math.min(multiStepCount / 2.0, 1.0);

  // 4. Agentic task (0.10)
  const agenticCount = countKeywordMatches(text, AGENTIC_KEYWORDS);
  scores.agentic_task = Math.min(agenticCount / 1.5, 1.0);

  // 5. Technical terms (0.10)
  const techCount = countKeywordMatches(text, TECHNICAL_TERMS);
  scores.technical_terms = Math.min(techCount / 2.0, 1.0);

  // 6. Token count (0.08)
  const wordCount = text.split(/\s+/).filter(Boolean).length;
  const tokenEstimate = wordCount * 1.3;
  scores.token_count = Math.min(tokenEstimate / 200.0, 1.0);

  // 7. Creative markers (0.05)
  const creativeCount = countKeywordMatches(text, CREATIVE_MARKERS);
  scores.creative_markers = Math.min(creativeCount / 2.0, 1.0);

  // 8. Question complexity (0.05)
  const questionMarks = (text.match(/\?/g) || []).length;
  const questionWords = (text.match(
    /\b(who|what|when|where|why|how|chi|cosa|quando|dove|perché|come|quale|quali)\b/gi,
  ) || []).length;
  scores.question_complexity = Math.min((questionMarks + questionWords) / 3.0, 1.0);

  // 9. Constraint count (0.04)
  const constraintCount = countKeywordMatches(text, CONSTRAINT_KEYWORDS);
  scores.constraint_count = Math.min(constraintCount / 3.0, 1.0);

  // 10. Imperative verbs (0.03)
  const imperativeCount = countKeywordMatches(text, IMPERATIVE_VERBS);
  scores.imperative_verbs = Math.min(imperativeCount / 1.5, 1.0);

  // 11. Output format (0.03)
  const formatCount = countPatternMatches(text, OUTPUT_FORMAT_PATTERNS);
  scores.output_format = Math.min(formatCount / 2.0, 1.0);

  // 12. Simple indicators (0.02) — NEGATIVE SCORE (ClawRouter approach)
  // Actively pushes trivial tasks toward lower scores.
  const simpleCount = countKeywordMatches(text, SIMPLE_INDICATORS);
  scores.simple_indicators = simpleCount >= 2 ? -1.0 : simpleCount >= 1 ? -1.0 : 0;

  // 13. Domain specificity (0.02)
  const upperAbbrevs = (text.match(/\b[A-Z]{2,}\b/g) || []).length;
  const dotNotation = (text.match(/\b\w+\.\w+\b/g) || []).length;
  scores.domain_specificity = Math.min((upperAbbrevs + dotNotation) / 3.0, 1.0);

  // 14. Reference complexity (0.02)
  const refCount = countPatternMatches(text, REFERENCE_PATTERNS);
  scores.reference_complexity = Math.min(refCount / 2.0, 1.0);

  // 15. Negation complexity (0.01)
  const negCount = countPatternMatches(text, NEGATION_PATTERNS);
  scores.negation_complexity = Math.min(negCount / 3.0, 1.0);

  return scores;
}

function calculateWeightedScore(dimensions: Record<string, number>): number {
  let sum = 0;
  for (const [dim, score] of Object.entries(dimensions)) {
    sum += score * (SCORING_WEIGHTS[dim] ?? 0);
  }
  return sum;
}

/**
 * Sigmoid confidence calibrated on distance from the nearest tier boundary.
 * Close to boundary = uncertain (~50%). Far from boundary = confident (~90%+).
 */
function boundaryDistanceConfidence(score: number, tier: string): number {
  const { simpleMedium, mediumComplex, complexCritical } = TIER_BOUNDARIES;
  let distance: number;

  switch (tier) {
    case "SIMPLE":
      distance = simpleMedium - score;
      break;
    case "MEDIUM":
      distance = Math.min(score - simpleMedium, mediumComplex - score);
      break;
    case "COMPLEX":
      distance = Math.min(score - mediumComplex, complexCritical - score);
      break;
    case "CRITICAL":
      distance = score - complexCritical;
      break;
    case "REASONING":
      // REASONING is override-based, assign high confidence
      distance = 0.15;
      break;
    default:
      distance = 0;
  }

  return 1 / (1 + Math.exp(-CONFIDENCE_STEEPNESS * distance));
}

/**
 * Main classification function. Replaces the Python subprocess chain.
 */
function classifyTask(text: string): ClassificationResult {
  const signals: string[] = [];

  // ── Long context override ──
  const estimatedTokens = Math.ceil(text.length / 4);
  if (estimatedTokens > MAX_TOKENS_FORCE_COMPLEX) {
    return {
      tier: "COMPLEX",
      confidence: 0.95,
      score: 1.0,
      method: "long-context-override",
      signals: [`${estimatedTokens} estimated tokens > ${MAX_TOKENS_FORCE_COMPLEX}`],
    };
  }

  // ── Score all 15 dimensions ──
  const dimensions = scoreDimensions(text);
  let score = calculateWeightedScore(dimensions);

  // ── Complexity boosters ──
  const boosterCount = countKeywordMatches(text, COMPLEXITY_BOOSTERS);
  const boosterBonus = Math.min(boosterCount * 0.08, 0.20);
  score += boosterBonus;
  if (boosterBonus > 0) {
    signals.push(`boosters +${boosterBonus.toFixed(2)}`);
  }

  // ── CRITICAL keyword override ──
  const criticalCount = countKeywordMatches(text, CRITICAL_KEYWORDS);
  if (criticalCount > 0) {
    const confidence = boundaryDistanceConfidence(score, "CRITICAL");
    signals.push("critical-keywords");
    return {
      tier: "CRITICAL",
      confidence: Math.max(confidence, 0.85),
      score,
      method: "inline-classifier",
      signals,
    };
  }

  // ── REASONING override: reasoning_markers >= 0.5 ──
  if (dimensions.reasoning_markers >= 0.5 && (score >= 0.10 || dimensions.reasoning_markers >= 0.75)) {
    const confidence = boundaryDistanceConfidence(score, "REASONING");
    signals.push("reasoning-override");
    return {
      tier: "REASONING",
      confidence: Math.max(confidence, 0.85),
      score,
      method: "inline-classifier",
      signals,
    };
  }

  // ── Agentic task bump ──
  const isAgentic = dimensions.agentic_task > 0.3 || dimensions.multi_step_patterns > 0.3;
  if (isAgentic) {
    const hasCode = dimensions.code_presence > 0;
    const hasMultiStep = dimensions.multi_step_patterns > 0.3;
    if (hasMultiStep && hasCode && score < 0.40) {
      score = 0.40;
      signals.push("agentic-bump-complex");
    } else if (score < 0.30) {
      score = 0.30;
      signals.push("agentic-bump-medium");
    }
  }

  // ── Map score to tier using boundaries ──
  let tier: string;
  if (score < TIER_BOUNDARIES.simpleMedium) {
    tier = "SIMPLE";
  } else if (score < TIER_BOUNDARIES.mediumComplex) {
    tier = "MEDIUM";
  } else if (score < TIER_BOUNDARIES.complexCritical) {
    tier = "COMPLEX";
  } else {
    tier = "CRITICAL";
  }

  // ── Confidence via boundary distance ──
  const confidence = boundaryDistanceConfidence(score, tier);

  // Build signals from top dimensions
  const topDims = Object.entries(dimensions)
    .map(([name, val]) => ({ name, contribution: val * (SCORING_WEIGHTS[name] ?? 0) }))
    .filter((d) => d.contribution > 0.005)
    .sort((a, b) => b.contribution - a.contribution)
    .slice(0, 3);
  for (const d of topDims) {
    signals.push(`${d.name}=${d.contribution.toFixed(3)}`);
  }

  return { tier, confidence, score, method: "inline-classifier", signals };
}

// ─── Plugin Constants ───────────────────────────────────────────────────────

const DEFAULT_LOG_PATH =
  "/home/jarvis/.openclaw/workspace/state/routing-log.jsonl";

const ROUTED_MARKER = "[routed]";

// Low confidence threshold — below this, fall back to MEDIUM (not SIMPLE)
const LOW_CONFIDENCE_THRESHOLD = 0.55;

interface TierModelConfig {
  provider: string;
  model: string;
  fullId: string;
  fallbacks: string[];
}

/**
 * Tier -> primary model mapping + fallback chains.
 * Matches config.json routing_rules.
 */
const TIER_CONFIG: Record<string, TierModelConfig> = {
  SIMPLE: {
    provider: "anthropic",
    model: "claude-haiku-4-5",
    fullId: "anthropic/claude-haiku-4-5",
    fallbacks: ["google/gemini-2.5-flash", "qwen/qwen-2.5-7b-instruct"],
  },
  MEDIUM: {
    provider: "anthropic",
    model: "claude-sonnet-4-6",
    fullId: "anthropic/claude-sonnet-4-6",
    fallbacks: ["google/gemini-3-flash-preview", "xai/grok-4-1-fast-non-reasoning"],
  },
  COMPLEX: {
    provider: "anthropic",
    model: "claude-sonnet-4-6",
    fullId: "anthropic/claude-sonnet-4-6",
    fallbacks: ["google/gemini-3-pro-preview", "anthropic/claude-opus-4-6"],
  },
  REASONING: {
    provider: "anthropic",
    model: "claude-opus-4-6",
    fullId: "anthropic/claude-opus-4-6",
    fallbacks: ["anthropic/claude-sonnet-4-6", "google/gemini-3-pro-preview"],
  },
  CRITICAL: {
    provider: "anthropic",
    model: "claude-opus-4-6",
    fullId: "anthropic/claude-opus-4-6",
    fallbacks: ["google/gemini-3-pro-preview"],
  },
};

// ─── Plugin Helpers ─────────────────────────────────────────────────────────

function splitModelId(fullId: string): { provider: string; model: string } {
  const idx = fullId.indexOf("/");
  if (idx < 0) return { provider: "", model: fullId };
  return { provider: fullId.slice(0, idx), model: fullId.slice(idx + 1) };
}

/**
 * Strip Telegram/platform envelope from the prompt.
 */
function stripEnvelope(text: string): string {
  let s = text;
  s = s.replace(/```json[\s\S]*?```/g, (block) =>
    /message_id|sender_name|chat_id|from_user_id|reply_to_message_id|timestamp/i.test(block) ? "" : block,
  );
  s = s.replace(/Conversation info \(untrusted[^)]*\)[\s\S]*?(?=\n\n|\n[A-Z][a-z]|$)/gi, "");
  s = s.replace(/Replied message \(untrusted[^)]*\)[\s\S]*?(?=\n\n|\n[A-Z][a-z]|$)/gi, "");
  s = s.replace(/^\s*(message_id|sender_name|sender_id|chat_id|chat_title|from_user_id|reply_to_message_id|timestamp|platform)\s*:.*$/gim, "");
  s = s.replace(/\n{3,}/g, "\n\n");
  return s.trim();
}

/**
 * Fast-path patterns for obvious SIMPLE classification (<0.1ms).
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

const SIMPLE_FAST_PATTERNS = [
  /^.{0,15}$/,
  /^(si|no|ok|va bene|perfetto|fatto|capito|esatto|giusto|bene|male)[\s!?.]*$/i,
  /^(yes|no|yep|nope|sure|right|correct|wrong|done|got it)[\s!?.]*$/i,
  /^(lol|lmao|haha|ahah)+[\s!?.]*$/i,
  /^(accendi|spegni|alza|abbassa|apri|chiudi|accendimi|spegnimi)\s+.+$/i,
  /^(turn on|turn off|switch on|switch off|open|close|dim)\s+.+$/i,
  /^(accendi|spegni|alza|abbassa|apri|chiudi)\s+(la|le|il|i|lo|gli|l')\s+.+$/i,
  /HEARTBEAT/i,
];

function isSimpleFastPath(message: string): boolean {
  const trimmed = message.trim();
  const lower = trimmed.toLowerCase();
  if (SIMPLE_EXACT.has(lower)) return true;
  for (const prefix of SIMPLE_PREFIXES) {
    if (lower.startsWith(prefix)) return true;
  }
  for (const pattern of SIMPLE_FAST_PATTERNS) {
    if (pattern.test(trimmed)) return true;
  }
  return false;
}

const CRON_JOBS_PATH = join(process.env.HOME || "/home/jarvis", ".openclaw/cron/jobs.json");

function lookupCronModel(cronId: string): string | null {
  try {
    const data = JSON.parse(readFileSync(CRON_JOBS_PATH, "utf-8"));
    const jobs = data.jobs || data;
    if (!Array.isArray(jobs)) return null;
    const job = jobs.find((j: any) => j.id === cronId);
    return job?.payload?.model || null;
  } catch {
    return null;
  }
}

function logDecision(logPath: string, entry: RoutingLogEntry): void {
  try {
    const dir = dirname(logPath);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    appendFileSync(logPath, JSON.stringify(entry) + "\n");
  } catch {
    // Logging should never break the pipeline
  }
}

// ─── Plugin Entry Point ─────────────────────────────────────────────────────

const plugin: OpenClawPluginDefinition = {
  id: "intelligent-routing",
  name: "Intelligent Routing",
  version: "4.0.0",
  description: "Classifies task complexity and routes to the appropriate model (inline classifier, no subprocess)",

  register(api) {
    const pluginConfig = api.pluginConfig as PluginConfig | undefined;
    const routingLogPath = pluginConfig?.routingLogPath || DEFAULT_LOG_PATH;
    const enableFastPath = pluginConfig?.enableFastPath !== false;
    const dryRun = pluginConfig?.dryRun === true;

    const routingState = new Map<string, RoutingDecision>();
    const lastRoutedPrompt = new Map<string, string>();

    api.logger.info(
      `[intelligent-routing] v4.0 registered | inline-classifier | fastPath=${enableFastPath} | dryRun=${dryRun}`,
    );

    // ── Hook 1: before_model_resolve ──────────────────────────────────
    api.on("before_model_resolve", (event, ctx) => {
      const startTime = Date.now();
      const rawPrompt = (event.prompt || "").trim();
      const sessionKey = ctx.sessionKey ?? "default";

      if (!rawPrompt) return {};

      const promptForMarkers = rawPrompt.replace(/^\[cron:[^\]]*\]\s*/, "");

      // ── [routed] marker ──
      if (promptForMarkers.startsWith(ROUTED_MARKER)) {
        api.logger.debug(`[intelligent-routing] Skipping pre-routed subagent prompt`);
        routingState.set(sessionKey, {
          tier: "ROUTED", model: "(pre-routed)", provider: "",
          confidence: 1.0, method: "skip-routed-marker", skipped: true,
        });

        const cronIdMatch = rawPrompt.match(/^\[cron:([0-9a-f-]+)\s/);
        const cronModel = cronIdMatch ? lookupCronModel(cronIdMatch[1]) : null;
        const resolvedModel = cronModel || "(pre-routed)";
        const resolvedSplit = cronModel ? splitModelId(cronModel) : { provider: "", model: "" };

        logDecision(routingLogPath, {
          timestamp: new Date().toISOString(),
          source: ctx.messageProvider || (ctx as any).source || "unknown",
          agentId: ctx.agentId || "main",
          taskDescription: rawPrompt.slice(0, 200),
          tier: "ROUTED", modelSelected: resolvedModel,
          providerSelected: resolvedSplit.provider || "(cron-assigned)",
          thinking: "native", fallbacks: [], confidence: 1.0,
          executionTimeMs: Date.now() - startTime,
          success: true, classificationMethod: "skip-routed-marker", notes: "",
        });
        return {};
      }

      // ── [force-model:X] marker ──
      const forceModelMatch = promptForMarkers.match(/^\[force-model:([^\]]+)\]/);
      if (forceModelMatch) {
        const requested = forceModelMatch[1].trim();
        const split = splitModelId(requested);
        api.logger.debug(`[intelligent-routing] Force-model override: ${requested}`);

        routingState.set(sessionKey, {
          tier: "FORCED", model: split.model, provider: split.provider,
          confidence: 1.0, method: "force-model", skipped: false,
        });
        lastRoutedPrompt.set(sessionKey, rawPrompt);

        logDecision(routingLogPath, {
          timestamp: new Date().toISOString(),
          source: ctx.messageProvider || ctx.source || "unknown",
          agentId: ctx.agentId || "main",
          taskDescription: rawPrompt.slice(0, 200),
          tier: "FORCED", modelSelected: requested, providerSelected: split.provider,
          thinking: "(default)", fallbacks: [], confidence: 1.0,
          executionTimeMs: Date.now() - startTime,
          success: true, classificationMethod: "force-model", notes: "",
        });

        if (!split.model) return {};
        return { modelOverride: split.model, providerOverride: split.provider || undefined };
      }

      // ── [tier:X] marker ──
      const tierMatch = promptForMarkers.match(/^\[tier:([^\]]+)\]/);
      if (tierMatch) {
        const requestedTier = tierMatch[1].trim().toUpperCase();
        const tierModel = TIER_CONFIG[requestedTier];
        api.logger.debug(`[intelligent-routing] Tier override: ${requestedTier}`);

        if (tierModel) {
          routingState.set(sessionKey, {
            tier: requestedTier, model: tierModel.model, provider: tierModel.provider,
            confidence: 1.0, method: "tier-override", skipped: false,
          });
          lastRoutedPrompt.set(sessionKey, rawPrompt);

          logDecision(routingLogPath, {
            timestamp: new Date().toISOString(),
            source: ctx.messageProvider || ctx.source || "unknown",
            agentId: ctx.agentId || "main",
            taskDescription: rawPrompt.slice(0, 200),
            tier: requestedTier, modelSelected: tierModel.fullId,
            providerSelected: tierModel.provider,
            thinking: "native", fallbacks: tierModel.fallbacks, confidence: 1.0,
            executionTimeMs: Date.now() - startTime,
            success: true, classificationMethod: "tier-override", notes: "",
          });

          return { modelOverride: tierModel.model, providerOverride: tierModel.provider };
        }

        api.logger.warn(`[intelligent-routing] Unknown tier "${requestedTier}", falling through`);
      }

      // ── Dedup ──
      const prevPrompt = lastRoutedPrompt.get(sessionKey);
      const prevDecision = routingState.get(sessionKey);
      if (prevPrompt === rawPrompt && prevDecision && !prevDecision.skipped) {
        api.logger.debug(
          `[intelligent-routing] Dedup: ${prevDecision.tier} (${prevDecision.model})`,
        );
        if (prevDecision.model) {
          return {
            modelOverride: prevDecision.model,
            providerOverride: prevDecision.provider || undefined,
          };
        }
        return {};
      }

      // ── Strip envelope for classification ──
      const cleanPrompt = stripEnvelope(rawPrompt);

      // ── Classification ──
      let tier: string;
      let confidence: number;
      let classificationMethod: string;
      let classificationSignals: string[] = [];

      // Stage 1: Fast-path
      const isSimple = enableFastPath && isSimpleFastPath(cleanPrompt);

      if (isSimple) {
        tier = "SIMPLE";
        confidence = 1.0;
        classificationMethod = "fast-path";
      } else {
        // Stage 2: Inline 15-dimension classifier (was subprocess, now <1ms)
        const result = classifyTask(cleanPrompt);
        tier = result.tier;
        confidence = result.confidence;
        classificationMethod = result.method;
        classificationSignals = result.signals;

        // Low-confidence guard: uncertain non-SIMPLE → MEDIUM
        // With boundary-distance confidence, this triggers when the score
        // is close to a tier boundary (genuinely ambiguous classification).
        if (
          confidence < LOW_CONFIDENCE_THRESHOLD &&
          tier !== "SIMPLE" &&
          tier !== "CRITICAL" &&
          tier !== "REASONING"
        ) {
          const oldTier = tier;
          tier = "MEDIUM";
          api.logger.debug(
            `[intelligent-routing] Low confidence (${confidence.toFixed(2)}) for ${oldTier}, upgrading to MEDIUM`,
          );
        }
      }

      // ── Resolve model from tier ──
      const tierModel = TIER_CONFIG[tier] || TIER_CONFIG.MEDIUM;
      const modelPart = tierModel.model;
      const providerPart = tierModel.provider;
      const fullModelId = tierModel.fullId;
      const fallbacks = tierModel.fallbacks;

      const executionTimeMs = Date.now() - startTime;

      // ── Save state for dedup + model awareness ──
      routingState.set(sessionKey, {
        tier, model: modelPart, provider: providerPart,
        confidence, method: classificationMethod, skipped: false,
      });
      lastRoutedPrompt.set(sessionKey, rawPrompt);

      // ── Log ──
      const notes = [
        ...(dryRun ? ["dry-run"] : []),
        ...classificationSignals,
      ].join("; ");

      logDecision(routingLogPath, {
        timestamp: new Date().toISOString(),
        source: ctx.messageProvider || ctx.source || "unknown",
        agentId: ctx.agentId || "main",
        taskDescription: cleanPrompt.slice(0, 200),
        tier, modelSelected: fullModelId, providerSelected: providerPart,
        thinking: "native", fallbacks, confidence,
        executionTimeMs, success: true,
        classificationMethod, notes,
      });

      api.logger.info(
        `[intelligent-routing] ${tier} (${classificationMethod}, ${confidence.toFixed(2)}) -> ` +
        `${providerPart}/${modelPart} [${executionTimeMs}ms]`,
      );

      if (dryRun) return {};
      return { modelOverride: modelPart, providerOverride: providerPart || undefined };
    });

    // ── Hook 2: before_prompt_build (model awareness) ─────────────────
    api.on("before_prompt_build", (event, ctx) => {
      const sessionKey = ctx.sessionKey ?? "default";
      const routing = routingState.get(sessionKey);

      if (!routing || routing.skipped || routing.tier === "SIMPLE") {
        return {};
      }

      const fullModel = routing.provider ? `${routing.provider}/${routing.model}` : routing.model;
      const prependContext = [
        `[Routing Info]`,
        `Tier: ${routing.tier}`,
        `Model: ${fullModel}`,
        `Thinking: native`,
        `Confidence: ${(routing.confidence * 100).toFixed(0)}%`,
        `Method: ${routing.method}`,
      ].join(" | ");

      return { prependContext };
    });

    api.logger.info(`[intelligent-routing] Hooks registered (before_model_resolve + before_prompt_build).`);
  },
};

export default plugin;
