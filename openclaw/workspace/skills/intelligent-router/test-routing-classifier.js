#!/usr/bin/env node
/**
 * Test cases for the routing classifier fix.
 * Tests: envelope stripping, fast-path Italian, confidence threshold.
 */

const { route, stripTelegramEnvelope, isFastPathSimple } = require('./intelligent-router-hook');

const tests = [
  // 1. Telegram envelope stripping
  {
    name: "Telegram envelope: simple greeting with metadata",
    input: '```json\n{"message_id": 12345, "sender_name": "Marco", "chat_id": -100123}\n```\nCiao come stai?',
    expect: { tier: 'SIMPLE', fastPath: true, envelopeStripped: true }
  },
  // 2. Telegram envelope with complex task underneath
  {
    name: "Telegram envelope: complex task with metadata",
    input: '```json\n{"message_id": 999, "sender_name": "Marco", "chat_id": -100}\n```\nAnalizza il codice sorgente del progetto e trova tutti i bug di sicurezza, poi scrivi un report dettagliato con le fix proposte',
    expect: { tier: 'NOT_SIMPLE', envelopeStripped: true }
  },
  // 3. Fast-path: Italian greeting
  {
    name: "Fast-path: 'ciao'",
    input: 'Ciao!',
    expect: { tier: 'SIMPLE', fastPath: true }
  },
  // 4. Fast-path: short confirmation
  {
    name: "Fast-path: 'ok perfetto'",
    input: 'ok perfetto',
    expect: { tier: 'SIMPLE', fastPath: true }
  },
  // 5. Fast-path: emoji only
  {
    name: "Fast-path: emoji message",
    input: '👍🔥',
    expect: { tier: 'SIMPLE', fastPath: true }
  },
  // 6. Fast-path: short Italian question
  {
    name: "Fast-path: 'che ora è?'",
    input: 'che ora è?',
    expect: { tier: 'SIMPLE', fastPath: true }
  },
  // 7. Fast-path: short status question
  {
    name: "Fast-path: 'come stai?'",
    input: 'come stai?',
    expect: { tier: 'SIMPLE', fastPath: true }
  },
  // 8. NOT fast-path: genuine complex task (web, no envelope)
  {
    name: "Web: complex coding task (no envelope, not fast-path)",
    input: 'Refactor the entire authentication module to use JWT tokens with refresh rotation, implement rate limiting per user, and add comprehensive unit tests with >90% coverage',
    expect: { tier: 'NOT_SIMPLE', fastPath: false }
  },
  // 9. Envelope stripping unit test
  {
    name: "stripTelegramEnvelope: removes metadata block",
    unit: () => {
      const input = '```json\n{"message_id": 1, "sender_name": "X", "chat_id": 2}\n```\nHello world';
      const out = stripTelegramEnvelope(input);
      return !out.includes('message_id') && out.includes('Hello world');
    }
  },
  // 10. isFastPathSimple unit test: longer Italian is NOT fast-path
  {
    name: "isFastPathSimple: longer sentence is false",
    unit: () => {
      return !isFastPathSimple('Voglio che tu analizzi il progetto completo e mi dia un report dettagliato delle performance con grafici e suggerimenti di ottimizzazione');
    }
  },
];

// Run
console.log('=== Routing Classifier Test Results ===\n');
let pass = 0, fail = 0;

for (const t of tests) {
  if (t.unit) {
    const ok = t.unit();
    console.log(`${ok ? '✅' : '❌'} ${t.name}`);
    ok ? pass++ : fail++;
    continue;
  }

  const result = route(t.input);
  let ok = true;
  const notes = [];

  if (t.expect.tier === 'NOT_SIMPLE') {
    if (result.tier === 'SIMPLE') { ok = false; notes.push(`tier=${result.tier}, expected NOT SIMPLE`); }
  } else if (t.expect.tier && result.tier !== t.expect.tier) {
    ok = false; notes.push(`tier=${result.tier}, expected ${t.expect.tier}`);
  }
  if (t.expect.fastPath === true && !result.fastPath) { ok = false; notes.push('expected fastPath'); }
  if (t.expect.fastPath === false && result.fastPath) { ok = false; notes.push('unexpected fastPath'); }
  if (t.expect.envelopeStripped === true && !result.envelopeStripped) { ok = false; notes.push('expected envelopeStripped'); }

  const detail = `tier=${result.tier} conf=${result.confidence} fast=${!!result.fastPath} stripped=${!!result.envelopeStripped}`;
  console.log(`${ok ? '✅' : '❌'} ${t.name}`);
  console.log(`   ${detail}${notes.length ? ' | ' + notes.join(', ') : ''}`);
  ok ? pass++ : fail++;
}

console.log(`\n${pass}/${pass + fail} passed${fail ? ` (${fail} FAILED)` : ''}`);
process.exit(fail > 0 ? 1 : 0);
