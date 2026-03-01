# Jarvis Trader — Tools Reference

## Script Location

Tutti gli script sono sotto la skill trading-engine:
```
cd ~/.openclaw/workspace/skills/trading-engine/scripts
```

## Tool CLI Reference

### hl-account.mjs — Account info
```bash
node hl-account.mjs balance                    # equity, available, margin
node hl-account.mjs positions                  # open positions con PnL
node hl-account.mjs fills --limit 20           # ultimi fill/trade
node hl-account.mjs orders                     # ordini aperti
```

### hl-trade.mjs — Esecuzione trade
```bash
node hl-trade.mjs market-buy --coin BTC --size 0.01 --strategy Scalping [--slippage 0.5]
node hl-trade.mjs market-sell --coin BTC --size 0.01 --strategy Scalping
node hl-trade.mjs limit-buy --coin BTC --size 0.01 --price 80000 --strategy Sentiment
node hl-trade.mjs limit-sell --coin BTC --size 0.01 --price 90000 --strategy Scalping
node hl-trade.mjs close --coin BTC --strategy Scalping
node hl-trade.mjs set-leverage --coin BTC --leverage 10 [--mode isolated]
node hl-trade.mjs cancel-all [--coin BTC]
```
**IMPORTANTE**: Passa SEMPRE `--strategy <nome>` per collegare la Transaction alla Strategy nell'ontology.
Lo script crea automaticamente le relazioni: `originated_from` (Strategy), `affects_account` (Account), `executed_by` (Agent Trading).

### hl-market.mjs — Dati di mercato
```bash
node hl-market.mjs price --coin BTC             # prezzo singolo
node hl-market.mjs price --all                   # tutti i prezzi
node hl-market.mjs candles --coin BTC --interval 5m --limit 100
node hl-market.mjs funding --coin BTC            # funding rate + annualizzato
node hl-market.mjs overview                      # top funding, top OI
node hl-market.mjs meta                          # lista perp disponibili
```

### signal-analyze.mjs — Analisi tecnica
```bash
node signal-analyze.mjs --coin BTC --timeframe 5m [--json]
# Output: RSI, EMA, MACD, BB, ATR, VWAP + score (-100 to +100)
```

### whale-monitor.mjs — Tracking whale
```bash
node whale-monitor.mjs refresh                  # aggiorna top 10 whale in ontology
node whale-monitor.mjs scan                     # controlla posizioni whale attuali
node whale-monitor.mjs list                     # mostra whale tracciate
```

### risk-check.mjs — Validazione pre-trade
```bash
node risk-check.mjs --coin BTC --direction long --size 0.01 --leverage 10 --strategy scalping
# Exit 0 = approved, Exit 1 = blocked
```

### strategy-ops.mjs — Gestione strategie
```bash
node strategy-ops.mjs list                      # lista strategie
node strategy-ops.mjs get --id <id>             # dettaglio strategia
node strategy-ops.mjs update --id <id> --status active|paused
node strategy-ops.mjs perf --id <id> --pnl 50 --trades 10 --wins 7
node strategy-ops.mjs init                      # bootstrap strategie default
```

### daily-report.mjs — Report giornaliero
```bash
node daily-report.mjs [--json]
```

### postmortem.mjs — Analisi trade chiusi
```bash
node postmortem.mjs [--days 1] [--json]
```

## Ontology

Endpoint: definito in env `ONTOLOGY_URL`
Speaker: `jarvis-agent`

### Entita e relazioni
- **Strategy** — configurazione e performance strategie
- **Account** (service=hyperliquid) — whale tracciate
- **Transaction** — log dei trade eseguiti
  - `originated_from` → Strategy (quale strategia ha generato il trade)
  - `affects_account` → Account (su quale wallet)
  - `executed_by` → Person "Agent Trading" (chi ha eseguito)

## Sentiment via Browser

Per il sentiment NON usare script — usa il browser tool direttamente:
1. `browser open` profilo Twitter
2. `browser snapshot` per leggere i tweet
3. Analizza con il tuo reasoning
4. Account list in SOUL.md

## Telegram

Per inviare messaggi a Marco: usa il tool `message` con `channel=telegram`, `to=${TELEGRAM_ADMIN_ID}`.
