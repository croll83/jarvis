#!/usr/bin/env node
/**
 * Intelligent Router Hook v1.1
 *
 * Input: task description (string)
 * Output (JSON): { tier, model, fallbacks, thinking, confidence }
 *
 * Calls the Python classifier, parses output, selects model from config.
 * Usable by Haiku via `exec`.
 *
 * Usage:
 *   node intelligent-router-hook.js "task description here"
 *   node intelligent-router-hook.js --tier MEDIUM   # force tier override
 *
 * v1.1 changes:
 *   - stripTelegramEnvelope(): remove Telegram metadata before classifying
 *   - isFastPathSimple(): Italian/short-message fast-path to SIMPLE
 *   - confidence < 0.30 defaults to SIMPLE (classifier is uncertain, don't pay for MEDIUM)
 *   - classifier failure defaults to SIMPLE (not MEDIUM)
 */

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const ROUTER_DIR = __dirname;
const ROUTER_PY = path.join(ROUTER_DIR, 'scripts', 'router.py');
const CONFIG_PATH = path.join(ROUTER_DIR, 'config.json');

// Confidence threshold below which we default to SIMPLE
const LOW_CONFIDENCE_THRESHOLD = 0.30;

// Thinking settings per tier
const THINKING_MAP = {
  SIMPLE: null,
  MEDIUM: null,
  COMPLEX: 'on',
  REASONING: 'high',
  CRITICAL: 'high',
};

// Model alias map for spawn commands
const ALIAS_MAP = {
  'anthropic/claude-haiku-4-5': 'haiku',
  'anthropic/claude-sonnet-4-6': 'sonnet',
  'anthropic/claude-opus-4-6': 'opus',
  'google/gemini-2.5-flash': 'gemini-flash',
  'google/gemini-3-flash-preview': 'gemini-3-flash',
  'google/gemini-3-pro-preview': 'gemini-3-pro',
  'xai/grok-4-1-fast-reasoning': 'grok',
  'qwen/qwen-2.5-7b-instruct': 'qwen',
};

function loadConfig() {
  return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
}

/**
 * Strip Telegram/platform envelope from the prompt.
 *
 * Removes:
 *   - ```json ... ``` blocks containing message_id / sender_name / chat_id
 *   - "Conversation info (untrusted metadata)" sections
 *   - "Replied message (untrusted, for context)" sections
 *   - Bare JSON lines with typical Telegram fields
 *
 * Returns the cleaned text (the real user message).
 */
function stripTelegramEnvelope(text) {
  let s = text;

  // Remove ```json ... ``` fenced blocks that look like Telegram metadata
  // (contain message_id, sender_name, chat_id, etc.)
  s = s.replace(/```json[\s\S]*?```/g, (block) => {
    if (/message_id|sender_name|chat_id|from_user_id|reply_to_message_id/i.test(block)) {
      return '';
    }
    return block; // keep non-metadata JSON blocks
  });

  // Remove "Conversation info (untrusted metadata)" sections until next blank line
  s = s.replace(/Conversation info \(untrusted[^)]*\)[\s\S]*?(?=\n\n|\n[A-Z][a-z]|$)/gi, '');

  // Remove "Replied message (untrusted, for context)" sections until next blank line
  s = s.replace(/Replied message \(untrusted[^)]*\)[\s\S]*?(?=\n\n|\n[A-Z][a-z]|$)/gi, '');

  // Remove lines that look like key: value Telegram metadata
  s = s.replace(/^\s*(message_id|sender_name|sender_id|chat_id|chat_title|from_user_id|reply_to_message_id|timestamp|platform)\s*:.*$/gim, '');

  // Collapse multiple blank lines
  s = s.replace(/\n{3,}/g, '\n\n');

  return s.trim();
}

// FAST-PATH DISABLED — all tasks go through classifier
// To re-enable, uncomment isFastPathSimple() below and its call in route()
/*
function isFastPathSimple(text) {
  const t = text.trim().toLowerCase();

  if (!t) return true;

  const realChars = t.replace(/[^a-z0-9àèéìòùáéíóú]/gi, '');
  if (realChars.length < 8) return true;

  if (/^[\p{Emoji_Presentation}\p{Extended_Pictographic}\s]+$/u.test(t)) return true;

  const greetings = ['ciao', 'hey', 'ey', 'a bello', 'salve', 'buongiorno', 'buonasera', 'buonanotte', 'ciau'];
  for (const g of greetings) {
    if (t === g || t.startsWith(g + ' ') || t.startsWith(g + '!') || t.startsWith(g + ',')) return true;
  }

  const simpleTokens = ['ok', 'okay', 'sì', 'si', 'no', 'nope', 'certo', 'esatto', 'capito',
    'grazie', 'prego', 'perfetto', 'ottimo', 'bene', 'benissimo', 'ottima', 'bravo',
    'yes', 'yep', 'nah', 'sure', 'thanks', 'ty', 'thx', 'np'];
  const words = t.replace(/[?!.,]/g, '').split(/\s+/).filter(Boolean);
  if (words.length <= 3 && words.some(w => simpleTokens.includes(w))) return true;

  const cleanWords = t
    .replace(/[\p{Emoji_Presentation}\p{Extended_Pictographic}]/gu, '')
    .replace(/[^\w\sàèéìòùáéíóú]/gi, '')
    .trim()
    .split(/\s+/)
    .filter(w => w.length > 0);
  if (cleanWords.length <= 5) return true;

  if (cleanWords.length <= 8) {
    const italianSimplePatterns = [
      /^che (ora|ore|modello|versione|linguaggio|lingua|temperatura|giorno|data)/,
      /^(hai|stai|sei) \w+(\s\w+){0,3}\??$/,
      /^(funziona|funzionano|va bene|va tutto)/,
      /^(quanti|quante|quanto|quanta) /,
      /^cos[aè] (stai|hai|sei|è|sta|sono)/,
      /^(quale|qual è|qual e) (il|la|lo|l'|i|le) /,
      /^(come stai|come va|come mai|come funziona)/,
      /^(quando|dove|chi) (è|sei|hai|siete|era|sarà)/,
      /^(stai usando|stai facendo|stai guardando|stai lavorando)/,
      /^(mi puoi dire|puoi dirmi|sai dirmi|sai cos[aè])/,
      /^(dimmi|dimm[oi]) (che|cosa|come|quando|dove|chi|qual)/,
      /^(che tipo di|che versione|che modello)/,
      /^(what (time|model|version|day|date) )/,
      /^(are you|is it|do you|can you|did you|have you) /,
      /^(how (are|is|do|did|many|much))/,
    ];
    if (italianSimplePatterns.some(p => p.test(t))) return true;
  }

  return false;
}
*/

function classifyWithPython(task) {
  try {
    const escaped = task.replace(/"/g, '\\"');
    const output = execSync(
      `python3 "${ROUTER_PY}" classify "${escaped}"`,
      { encoding: 'utf-8', timeout: 10000, stdio: ['pipe', 'pipe', 'pipe'] }
    );

    // Parse the text output
    const tierMatch = output.match(/^Classification:\s*(\w+)/m);
    const confMatch = output.match(/^Confidence:\s*([\d.]+)%/m);

    if (!tierMatch) return null;

    return {
      tier: tierMatch[1],
      confidence: confMatch ? parseFloat(confMatch[1]) / 100 : 0,
    };
  } catch (e) {
    return null;
  }
}

function route(task, overrideTier) {
  const config = loadConfig();
  let tier, confidence;
  let fastPath = false;
  let envelopeStripped = false;

  if (overrideTier) {
    tier = overrideTier.toUpperCase();
    confidence = 1.0;
  } else {
    // ── Step 1: strip Telegram envelope ────────────────────────────────
    const cleanTask = stripTelegramEnvelope(task);
    envelopeStripped = cleanTask !== task;

    // ── Step 2: fast-path DISABLED — all tasks go through classifier ──
    // if (isFastPathSimple(cleanTask)) {
    //   tier = 'SIMPLE';
    //   confidence = 0.92;
    //   fastPath = true;
    // } else {
    {
      // ── Step 3: call Python classifier ─────────────────────────────
      const result = classifyWithPython(cleanTask);

      if (!result) {
        // Classifier failed → safe default is SIMPLE (was MEDIUM — expensive and wrong)
        tier = 'SIMPLE';
        confidence = 0;
      } else {
        tier = result.tier;
        confidence = result.confidence;

        // ── Step 4: low-confidence guard ───────────────────────────────
        // If the classifier is uncertain (< 30% confidence), default to SIMPLE.
        // CRITICAL and REASONING are exempt — false negatives there are costly.
        if (confidence < LOW_CONFIDENCE_THRESHOLD
            && tier !== 'CRITICAL'
            && tier !== 'REASONING') {
          tier = 'SIMPLE';
        }
      }
    }
  }

  const rules = config.routing_rules[tier];
  if (!rules) {
    // Unknown tier, fall back to MEDIUM
    const medRules = config.routing_rules.MEDIUM;
    return {
      tier: 'MEDIUM',
      model: medRules.primary,
      modelAlias: ALIAS_MAP[medRules.primary] || medRules.primary,
      fallbacks: medRules.fallback_chain,
      thinking: null,
      confidence,
      classifierFailed: !overrideTier && confidence === 0,
    };
  }

  const isSimple = tier === 'SIMPLE';

  return {
    tier,
    model: rules.primary,
    modelAlias: ALIAS_MAP[rules.primary] || rules.primary,
    fallbacks: rules.fallback_chain,
    thinking: THINKING_MAP[tier] || null,
    confidence,
    handleDirectly: isSimple,
    fastPath: fastPath || undefined,
    envelopeStripped: envelopeStripped || undefined,
    classifierFailed: !overrideTier && confidence === 0,
  };
}

// CLI
if (require.main === module) {
  const args = process.argv.slice(2);
  let task = '';
  let overrideTier = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--tier' && args[i + 1]) {
      overrideTier = args[++i];
    } else {
      task += (task ? ' ' : '') + args[i];
    }
  }

  if (!task) {
    console.error('Usage: node intelligent-router-hook.js "task description"');
    process.exit(1);
  }

  const result = route(task, overrideTier);
  console.log(JSON.stringify(result, null, 2));
}

module.exports = { route, stripTelegramEnvelope /* , isFastPathSimple */ };
