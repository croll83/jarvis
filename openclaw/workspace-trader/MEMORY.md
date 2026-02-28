# Jarvis Trader — Long-term Memory

## Trading Config
- Exchange: Hyperliquid (perpetual futures)
- Wallet: env var JARVIS_WALLET (TPM-backed, never on disk)
- Mode: LIVE (no dry-run per scalping e copytrading)

## Strategie attive
- **Scalping**: ATTIVA — 70% tech, 25% sent, 5% whale — budget 40%
- **Sentiment**: PAUSED — da attivare dopo validazione pipeline sentiment
- **Copytrading**: ATTIVA — 70% whale, 25% tech, 10% sent — budget 30%

## Whale tracking
- Top 10 HL leaderboard, portfolio $5K-$100K, PnL positivo
- Aggiornamento ogni 5 giorni
- Min position size per segnale: $10K

## Lezioni apprese
- SOL liquidation ($99 → $45): mai overleverage su alt in trend rialzista estremo
- Sentiment scraper CDP headless: broken, usa browser reale della workstation
- Grok per sentiment: funziona bene come analizzatore, dare contesto + tweet raw
- Lexicon-based scoring: inutile, eliminato in v3

## Known issues
- Sentiment strategy paused finche il pipeline non e validato (min 2 settimane di dati)
- Le API HL leaderboard possono cambiare formato senza preavviso
- Funding rate extremes possono essere opportunita (arb) o trappole

## Account Twitter per sentiment
- Tier 1: lookonchain, EmberCN, CryptoQuant_AI
- Tier 2: Pentosh1, HsakaTrades, WatcherGuru, Tree_of_Alpha
- Tier 3: ali_charts, CryptoBullet1, SmartContracter
