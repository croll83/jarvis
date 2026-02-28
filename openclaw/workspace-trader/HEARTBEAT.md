# Heartbeat

Il trader agent non usa heartbeat tradizionale — usa cron job per le attivita periodiche.

Cron gestiti:
1. **Market Scan** (ogni 5 min) — scan mercato + esecuzione trade
2. **Sentiment Scan** (ogni 4h) — scraping Twitter + analisi
3. **Whale Refresh** (ogni 5 giorni) — aggiornamento lista whale
4. **Daily Report** (10:00 + 22:00 CET) — report portfolio su Telegram
5. **Postmortem** (21:00 CET) — analisi trade chiusi

Non c'e nulla da fare nel heartbeat. Tutto e gestito dai cron.
