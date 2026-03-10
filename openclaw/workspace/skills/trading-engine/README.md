# Trading Engine v3.0

Multi-strategy Hyperliquid perpetual futures trading engine per Jarvis.

## Architettura

```
Trader Agent (OpenClaw, heartbeat/cron-driven)
├── Tools (CLI scripts via exec)
│   ├── hl-account    → balance, posizioni, fills
│   ├── hl-trade      → market/limit buy/sell, close, leverage
│   ├── hl-market     → candele, funding, prezzi, overview
│   ├── signal-analyze → indicatori tecnici (RSI, EMA, MACD, BB, ATR, VWAP)
│   ├── whale-monitor  → leaderboard HL, tracking posizioni whale
│   ├── risk-check     → validazione pre-trade cross-strategy
│   ├── strategy-ops   → CRUD strategie in ontology
│   ├── daily-report   → report giornaliero portfolio
│   └── postmortem     → analisi trade chiusi
│
├── Browser Tool (sentiment, via workstation Chrome)
│   └── Scraping Twitter → analisi Grok → sentiment per coin
│
└── Ontology (fonte di verità)
    ├── Strategy entities → config, stato, performance
    ├── Account entities  → whale list
    └── Transaction entities → log trade
```

## Strategie

| Strategia | Pesi (tech/sent/whale) | Timeframe | Target | Leva |
|-----------|----------------------|-----------|--------|------|
| Scalping | 70/25/5 | 5m | 1-2% | 5-10x |
| Sentiment | 70 sent/20 whale/10 tech | 15m-1h | 5-10% | 3-5x |
| Copytrading | 70 whale/25 tech/10 sent | 1h-4h | 10% | mirror |

## Setup

```bash
cd scripts && npm install
```

### Variabili d'ambiente richieste

- `JARVIS_WALLET` — chiave privata (da TPM)
- `HYPERLIQUID_ADDRESS` — indirizzo wallet
- `ONTOLOGY_URL` — endpoint ontology REST API
- `ONTOLOGY_API_TOKEN` — token autenticazione ontology

### Bootstrap strategie

```bash
node strategy-ops.mjs init
```

## Differenze da v2

- **Multi-strategia** — N strategie in parallelo (v2: singola)
- **Ontology-driven** — strategie e performance tracciati in ontology (v2: file JSON)
- **Agentico** — l'agent ragiona e decide, gli script sono tool (v2: script autonomi)
- **Wallet in RAM** — chiave da env var TPM (v2: file crittografato su disco)
- **No drawdown protection** — rimosso per scelta
- **Browser sentiment** — Chrome reale via workstation (v2: headless broken)
- **Whale tracking** — top 10 HL leaderboard (v2: 3 wallet hardcoded)
