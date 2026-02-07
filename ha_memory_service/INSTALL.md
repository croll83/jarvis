# HA Memory Service (Sidecar) — Guida Installazione

Servizio sidecar che gira accanto a Home Assistant. Ingerisce eventi via WebSocket, genera summary con Qwen, e offre ricerca semantica via ChromaDB.

**Importante:** Viene deployato **1 istanza per location HA**. Se hai 2 location Home Assistant, avrai 2 container di questo servizio.

---

## Prerequisiti

| Requisito | Minimo |
|-----------|--------|
| Python | 3.11+ |
| RAM | 256 MB per istanza |
| Disk | 500 MB per ChromaDB per location |
| Ollama | Raggiungibile via rete (per embedding + summary) |
| Home Assistant | Long-lived access token |

Non richiede GPU — usa Ollama remoto per embedding e summarization.

---

## Deploy con Docker

### 1. Build

```bash
cd jarvis/orchestrator/ha_memory_service
docker build -t jarvis-ha-memory .
```

### 2. Configurazione

Tutte le configurazioni sono via env var (nessun config.py):

#### Env var obbligatorie

| Env Var | Esempio | Descrizione |
|---------|---------|-------------|
| `LOCATION_ID` | `wagmi` | ID univoco della location (deve corrispondere al DB orchestrator) |
| `HA_URL` | `http://192.168.1.100:8123` | URL Home Assistant |
| `HA_TOKEN` | `eyJ0eX...` | Long-lived access token HA |
| `QWEN_URL` | `http://ollama:11434` | URL Ollama (per embedding + summary) |

#### Env var opzionali

| Env Var | Default | Descrizione |
|---------|---------|-------------|
| `DB_PATH` | `/data/ha_memory.db` | Path database SQLite |
| `CHROMA_PATH` | `/data/chroma` | Path storage ChromaDB |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Modello embedding Ollama |
| `SUMMARY_MODEL` | `qwen2.5:3b` | Modello summarization |
| `SUMMARY_TEMPERATURE` | `0.3` | Temperatura summary LLM |
| `SUMMARY_TIMEOUT` | `30` | Timeout chiamate summary (sec) |
| `EMBEDDING_TIMEOUT` | `30` | Timeout chiamate embedding (sec) |
| `SERVICE_PORT` | `8100` | Porta del servizio |

#### Env var intervalli/timing

| Env Var | Default | Descrizione |
|---------|---------|-------------|
| `WS_RETRY_DELAY` | `30` | Delay retry WebSocket dopo errore auth (sec) |
| `WS_RECONNECT_DELAY` | `10` | Delay riconnessione WebSocket (sec) |
| `SCHEDULER_INITIAL_DELAY` | `30` | Delay iniziale scheduler (sec) |
| `SCHEDULER_INTERVAL` | `60` | Intervallo check scheduler (sec) |

#### Env var filtri entita

| Env Var | Default | Descrizione |
|---------|---------|-------------|
| `SKIP_ENTITY_PREFIXES` | `update.` | Prefissi entita da ignorare (separati da virgola) |
| `SKIP_ENTITY_SUFFIXES` | `_battery,_linkquality,_signal` | Suffissi entita da ignorare |

### 3. Avvio (singola location)

```bash
docker run -d \
  --name jarvis_ha_memory_wagmi \
  -e LOCATION_ID=wagmi \
  -e HA_URL=http://192.168.1.100:8123 \
  -e HA_TOKEN=eyJ0eXAiOiJKV1QiLCJhbGciOi... \
  -e QWEN_URL=http://ollama:11434 \
  -v ha_memory_wagmi:/data \
  -p 8100:8100 \
  --restart unless-stopped \
  jarvis-ha-memory
```

### 4. Avvio multi-location

Per ogni location, un container separato con LOCATION_ID diverso e porta diversa:

```bash
# Location 1: wagmi
docker run -d \
  --name jarvis_ha_memory_wagmi \
  -e LOCATION_ID=wagmi \
  -e HA_URL=http://192.168.1.100:8123 \
  -e HA_TOKEN=token_wagmi \
  -e QWEN_URL=http://ollama:11434 \
  -e SERVICE_PORT=8100 \
  -v ha_memory_wagmi:/data \
  -p 8100:8100 \
  --restart unless-stopped \
  jarvis-ha-memory

# Location 2: albani
docker run -d \
  --name jarvis_ha_memory_albani \
  -e LOCATION_ID=albani \
  -e HA_URL=http://100.x.x.x:8123 \
  -e HA_TOKEN=token_albani \
  -e QWEN_URL=http://ollama:11434 \
  -e SERVICE_PORT=8101 \
  -v ha_memory_albani:/data \
  -p 8101:8100 \
  --restart unless-stopped \
  jarvis-ha-memory
```

---

## Integrazione con docker-compose

Aggiungi al `docker-compose.yml` principale o crea un file dedicato:

```yaml
  ha_memory_wagmi:
    build: ./orchestrator/ha_memory_service
    container_name: jarvis_ha_memory_wagmi
    environment:
      - LOCATION_ID=wagmi
      - HA_URL=http://homeassistant:8123
      - HA_TOKEN=${HASS_TOKEN_WAGMI}
      - QWEN_URL=http://ollama:11434
      - EMBEDDING_MODEL=nomic-embed-text
      - SUMMARY_MODEL=qwen2.5:3b
    volumes:
      - ha_memory_wagmi:/data
    ports:
      - "8100:8100"
    depends_on:
      ollama:
        condition: service_healthy
    restart: unless-stopped

  ha_memory_albani:
    build: ./orchestrator/ha_memory_service
    container_name: jarvis_ha_memory_albani
    environment:
      - LOCATION_ID=albani
      - HA_URL=http://100.x.x.x:8123
      - HA_TOKEN=${HASS_TOKEN_ALBANI}
      - QWEN_URL=http://ollama:11434
      - SERVICE_PORT=8100
    volumes:
      - ha_memory_albani:/data
    ports:
      - "8101:8100"
    depends_on:
      ollama:
        condition: service_healthy
    restart: unless-stopped
```

---

## Prompt Templates

I prompt per la summarization sono in `ha_memory_service/prompts/`:

```
prompts/
  location_hourly.txt    # Analisi eventi orari della casa
  location_daily.txt     # Analisi giornaliera con pattern
```

Modificabili per traduzione o personalizzazione senza toccare il codice.

---

## Come Funziona

1. **WebSocket Listener** — Si connette a Home Assistant via WebSocket e riceve tutti gli eventi `state_changed`
2. **Event Filtering** — Ignora entita rumorose (`update.*`, `*_battery`, ecc.)
3. **Raw Storage** — Salva eventi raw in SQLite (TTL 30 min)
4. **Hourly Summary** — Al minuto 5, genera un riassunto degli eventi dell'ultima ora con Qwen
5. **Daily Summary** — Alle 03:00, genera un riassunto giornaliero con pattern e anomalie
6. **Vector Index** — Indicizza summary e fatti in ChromaDB per ricerca semantica
7. **API** — Espone endpoint REST per l'orchestrator (`/memory`, `/search`, `/health`)

---

## API Endpoints

| Metodo | Path | Descrizione |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/memory` | Recupera memoria stratificata (hot/warm/cold/longterm) |
| POST | `/search` | Ricerca semantica eventi |
| GET | `/stats` | Statistiche (conteggi eventi, summary, vettori) |

### Esempio: Recupera memoria

```bash
curl -X POST http://localhost:8100/memory \
  -H "Content-Type: application/json" \
  -d '{"hot_minutes": 30, "warm_hours": 24, "cold_days": 7}'
```

### Esempio: Ricerca semantica

```bash
curl -X POST http://localhost:8100/search \
  -H "Content-Type: application/json" \
  -d '{"query": "quando e stata aperta la porta", "n_results": 10}'
```

---

## Verifica Installazione

```bash
# Health check
curl http://localhost:8100/health

# Verifica connessione WebSocket (nei logs)
docker logs jarvis_ha_memory_wagmi -f --tail 20
```

**Risposta health attesa:**
```json
{
  "status": "healthy",
  "location_id": "wagmi",
  "ha_connected": true,
  "events_today": 1234,
  "summaries_today": 5
}
```

---

## Troubleshooting

### WebSocket non si connette

```bash
# Verifica token HA
curl -H "Authorization: Bearer $HA_TOKEN" http://<HA_URL>/api/

# Verifica raggiungibilita
ping <HA_IP>
```

### Embedding fallisce

```bash
# Verifica che il modello sia scaricato in Ollama
curl http://<QWEN_URL>/api/tags
# Deve contenere "nomic-embed-text"

# Se mancante:
docker exec jarvis_ollama ollama pull nomic-embed-text
```

### ChromaDB pieno

```bash
# Verifica dimensione
du -sh /data/chroma/

# Cleanup manuale (il servizio lo fa automaticamente ogni notte)
# Ma puoi forzarlo riavviando il container
docker restart jarvis_ha_memory_wagmi
```
