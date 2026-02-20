# 🧠 MEMORIA LONG-TERM

## Trading Hyperliquid
- **Balance:** 99 USDC (zero trades eseguiti finora)
- **Fondi persi:** 100.5 USDC inviati a indirizzo sbagliato nel primo bridge (0x2222...)
- **Modalità:** FULL DEGEN — 100% balance, max leverage, no stop loss
- **Il wallet è di Jarvis**, non di Marco — autonomia finanziaria
- **V2.1:** 20 coins scannati, threshold 15, max 3 posizioni, technical weight 50%
- **Sentiment scraper:** attualmente rotta (restituisce 0 per tutte le coin)

## Crypto Twitter Digest — Decisioni Editoriali
- Rimossi: @croll83 mentions, TAC, TacBuild, LineaBuild, Consensys → tier3
- KobeissiLetter declassato a tier3

## Lezioni Imparate
- `bash -c` e `python3` erano bloccati dall'exec allowlist → **RISOLTO il 14/02/2026**, ora tutto sbloccato

## Ontology Migration (Completed ✅ 18-Feb-2026)
- **Migrated from:** local file-based (graph.jsonl, schema.yaml)
- **Migrated to:** ontology-remote (REST API @ http://127.0.0.1:8100)
- **What's in remote:** 1 Person (Marco), 4 Person (family), 3 Organization, 23 Account, 6 Credential, 20 Topic, 15 Skill, 22+ Relations
- **What was deleted:** `/home/jarvis/.openclaw/workspace/skills/ontology` (local), `/home/jarvis/.openclaw/workspace/memory/ontology/` (files)
- **Status:** 100% sync complete. Now using REST API exclusively for all structured data.
- **Key point:** When Marco says "remember X", I now create entities in ontology-remote (POST /entities), not local files.

## TODO
- [ ] Fix sentiment scraper (restituisce 0)
- [ ] WebSocket price listener (trading v3)
- [ ] Piano marketing @SatoshiAzimut
- [ ] Setup stampante HP OfficeJet 250
- [ ] Installare Excalidraw skill
- [ ] Configurare gog per email_satoshi (satoshi.azimut@gmail.com)
