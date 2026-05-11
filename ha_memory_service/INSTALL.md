# HA Memory Service — Guida Installazione

Servizio memory per JARVIS. Ingerisce eventi HA via WebSocket, genera summary con LLM, pubblica contesto real-time su Redis e estrae pattern long-term su mem0.

**1 istanza per location HA.**

---

## Metodo 1: Add-on HAOS (consigliato)

Il modo piu semplice. L'addon gira dentro HAOS con accesso diretto a HA, Tailscale, e Supervisor API.

### Prerequisiti

- HAOS con almeno **8 GB RAM** (il servizio usa ~300 MB)
- Addon Tailscale installato (se serve raggiungere Ollama remoto)

### Installazione

1. Copia la cartella `ha_memory_service/` nel path local addons di HAOS:

```
/addons/jarvis_ha_memory/
├── main.py
├── prompts/
├── Dockerfile
├── build.yaml
├── config.yaml
├── run.sh
├── requirements.txt
├── translations/
│   └── en.yaml
└── DOCS.md
```

Per copiare i file su HAOS puoi usare:
- **Samba share addon** — monta `/addons/` via rete e copia
- **SSH addon** — `scp -r ha_memory_service/ root@<HAOS_IP>:/addons/jarvis_ha_memory/`
- **VS Code addon** — naviga in `/addons/` e copia

2. In HA vai su **Settings > Add-ons > Add-on Store**
3. Clicca i **3 puntini** in alto a destra > **Check for updates**
4. L'addon **JARVIS HA Memory** appare nella sezione "Local add-ons"
5. Clicca > **Install**

### Configurazione

Dopo l'installazione, vai nella tab **Configuration** dell'addon:

**Cloud (temporaneo):**
```yaml
location_id: wagmi
ai_backend: api
openrouter_api_key: sk-or-v1-xxx
gemini_api_key: AIzaSyxxx
```

**Locale (definitivo):**
```yaml
location_id: wagmi
ai_backend: local
ollama_url: http://100.x.x.x:11434
```

6. Clicca **Save** > **Start**
7. Controlla i log nella tab **Log**

### Vantaggi addon

- **Zero config HA token** — il Supervisor lo gestisce automaticamente
- **Tailscale incluso** — se l'addon Tailscale e installato, la rete 100.x.x.x e gia visibile
- **Watchdog** — HA monitora la salute del servizio e lo riavvia se crasha
- **Backup** — incluso nei backup HA automatici
- **Update** — aggiornabile dalla UI
- **Log** — visibili dalla UI HA

### Migrazione cloud -> locale

1. Vai in **Configuration** dell'addon
2. Cambia `ai_backend` da `api` a `local`
3. Inserisci `ollama_url` con l'IP Tailscale di Ollama
4. Rimuovi le API keys (opzionale)
5. **Save** > **Restart**

I summary storici in SQLite persistono.

---

## Metodo 2: Docker standalone (LXC su Proxmox)

Per deploy separato dall'istanza HA (es. monitoring di HA remoti, o ambienti senza HAOS).

### Prerequisiti

| Requisito | Minimo |
|-----------|--------|
| Proxmox | 7.x+ |
| LXC template | Debian 12 o Ubuntu 22.04 |
| RAM (LXC) | 512 MB |
| Disk (LXC) | 4 GB |
| Docker | Installato dentro LXC |
| Tailscale | Installato dentro LXC |

### Creazione LXC

```bash
pct create 201 local:vztmpl/debian-12-standard_12.2-1_amd64.tar.zst \
  --hostname ha-memory \
  --memory 512 \
  --swap 256 \
  --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --start 1
```

### Setup LXC

```bash
pct enter 201
apt update && apt upgrade -y
apt install -y curl ca-certificates gnupg
curl -fsSL https://get.docker.com | sh
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=ha-memory
```

### Build e avvio

```bash
mkdir -p /opt/ha_memory_service
cd /opt/ha_memory_service
# Copia i file del servizio qui

# Build per standalone (base image Python, non HA)
docker build \
  --build-arg BUILD_FROM=python:3.11-slim \
  -t jarvis-ha-memory .

# Cloud mode
docker run -d \
  --name jarvis_ha_memory_wagmi \
  -e AI_BACKEND=api \
  -e LOCATION_ID=wagmi \
  -e HA_URL=http://192.168.1.100:8123 \
  -e HA_TOKEN=eyJ0eXAiOiJKV1QiLCJhbGciOi... \
  -e REDIS_URL=redis://your_redis_host:6379/0 \
  -e MEM0_BASE_URL=http://your_mem0_host:8200 \
  -e OPENROUTER_API_KEY=sk-or-v1-xxx \
  -e GEMINI_API_KEY=AIzaSyxxx \
  -v ha_memory_wagmi:/data \
  -p 8100:8100 \
  --restart unless-stopped \
  jarvis-ha-memory \
  python /app/main.py

# Local mode
docker run -d \
  --name jarvis_ha_memory_wagmi \
  -e AI_BACKEND=local \
  -e LOCATION_ID=wagmi \
  -e HA_URL=http://192.168.1.100:8123 \
  -e HA_TOKEN=eyJ0eXAiOiJKV1QiLCJhbGciOi... \
  -e REDIS_URL=redis://your_redis_host:6379/0 \
  -e MEM0_BASE_URL=http://your_mem0_host:8200 \
  -e OLLAMA_URL=http://100.x.x.x:11434 \
  -v ha_memory_wagmi:/data \
  -p 8100:8100 \
  --restart unless-stopped \
  jarvis-ha-memory \
  python /app/main.py
```

**NOTA:** In standalone mode il CMD e `python /app/main.py` (non `/run.sh` che richiede bashio).

---

## Architettura di rete

```
                    TAILSCALE MESH
                         |
    ┌────────────────────┼────────────────────┐
    │                    │                    │
  MILANO              NAPOLI               VPS
  Proxmox             Proxmox
  ├─ HAOS VM          ├─ LXC Jarvis        ├─ LXC Openclaw
  │  ├─ HA Core       │  ├─ Orchestrator   │  ├─ Hermes
  │  ├─ ha_memory     │  ├─ Redis :6379    │  └─ (Tailscale)
  │  │  (addon)       │  ├─ mem0-stack     │
  │  └─ Tailscale     │  │  (repo separato)│
  │     (addon)       │  └─ Ollama         │
  │                   │                    │
  └─ 2ms latency      ~50ms Starlink       Cloud
```

### Latency

| Percorso | Latency | Note |
|----------|---------|------|
| ha_memory -> HA (addon) | <1ms | Stesso container network |
| ha_memory -> Redis (LXC Jarvis) | ~50ms | Tailscale, real-time push |
| ha_memory -> mem0 (LXC Jarvis) | ~50ms | Tailscale, solo nightly batch |
| ha_memory -> Ollama (Napoli) | ~50ms | Tailscale, solo ogni ora |
| ha_memory -> OpenRouter | ~100-200ms | Solo in API mode |

---

## API Endpoints

| Metodo | Path | Descrizione |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/memory` | Memoria stratificata (hot/warm/cold/longterm) |
| POST | `/memory/search` | Ricerca semantica eventi |

---

## Troubleshooting

### WebSocket non si connette (addon)

Riavvia l'addon. Il Supervisor token viene rigenerato al restart.

### WebSocket non si connette (standalone)

```bash
curl -H "Authorization: Bearer $HA_TOKEN" http://<HA_URL>/api/
```

### Embedding fallisce (local)

```bash
curl http://100.x.x.x:11434/api/tags
# Deve contenere "nomic-embed-text"
```

### Embedding fallisce (api)

```bash
curl -X POST "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": {"parts": [{"text": "test"}]}, "output_dimensionality": 768}'
```

### Summary fallisce (api)

```bash
curl -X POST "https://openrouter.ai/api/v1/chat/completions" \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen/qwen-2.5-7b-instruct", "messages": [{"role": "user", "content": "test"}], "max_tokens": 10}'
```
