# Italo Token Refresh — Cron Setup

## Cos'è

Cron di sistema (non OpenClaw) che refresha il JWT Italo ogni 40 minuti per mantenere la sessione attiva. In caso di errore, manda una notifica Telegram via `openclaw message send`.

## Prerequisiti

- Node.js installato (`/usr/bin/node`)
- OpenClaw gateway attivo su `127.0.0.1:18789` (per notifiche Telegram)
- Token Italo validi in `~/.openclaw/workspace/skills/train-assistant/.env`

## File coinvolti

| File | Scopo |
|------|-------|
| `scripts/refresh-italo-cron.sh` | Script bash eseguito dal cron |
| `scripts/search-italo.mjs` | Script Node.js con `--refresh-only` |
| `~/.openclaw/workspace/skills/train-assistant/.env` | Token JWT (session + refresh) |
| `/tmp/italo-refresh.log` | Log delle esecuzioni |

## Installazione

### 1. Verifica che lo script è eseguibile

```bash
chmod +x /opt/jarvis/openclaw/skills/train-assistant/scripts/refresh-italo-cron.sh
```

### 2. Aggiungi il cron (utente jarvis)

```bash
(crontab -l 2>/dev/null; echo "*/40 * * * * /opt/jarvis/openclaw/skills/train-assistant/scripts/refresh-italo-cron.sh") | crontab -
```

### 3. Verifica

```bash
crontab -l
# Output atteso:
# */40 * * * * /opt/jarvis/openclaw/skills/train-assistant/scripts/refresh-italo-cron.sh
```

## Rimozione

```bash
crontab -l | grep -v "refresh-italo-cron" | crontab -
```

## Monitoraggio

```bash
# Ultime 10 righe del log
tail -10 /tmp/italo-refresh.log

# Solo errori
grep FAIL /tmp/italo-refresh.log

# Ultimo refresh
tail -1 /tmp/italo-refresh.log
```

### Formato log

```
[2026-03-10 02:40:01] OK
[2026-03-10 03:20:01] OK
[2026-03-10 04:00:01] FAIL: Italo refresh 401: {"statusCode":401}
```

## Notifiche Telegram

In caso di errore, il cron manda un messaggio Telegram via OpenClaw:

```bash
openclaw message send --channel telegram -t 172751380 -m "⚠️ Italo token refresh fallito: <errore>"
```

- Chat ID: `172751380` (configurato in `refresh-italo-cron.sh`)
- Flag `--silent` per non disturbare di notte

## Quando il refresh fallisce

Il refresh token ha validità ~24h. Se scade:

1. Apri `https://biglietti.italotreno.com` nel browser
2. DevTools → Application → Cookies → copia il valore di `BIGSessionToken`
3. Aggiorna `~/.openclaw/workspace/skills/train-assistant/.env`:
   ```
   ITALO_SESSION_TOKEN=<token copiato>
   ITALO_REFRESH_TOKEN=<refresh token dal browser>
   ```
4. Il cron riprenderà a funzionare al prossimo ciclo

## Tempistiche

| Token | Durata | Refresh |
|-------|--------|---------|
| Session JWT | ~60 min | Automatico via cron ogni 40 min |
| Refresh token | ~24h | Richiede nuova estrazione da browser |
