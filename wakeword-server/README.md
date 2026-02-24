# JARVIS Wakeword Server

Server-side wake word detection + HTTP management proxy per i device AtomS3R.

## Architettura

```
AtomS3R (LAN)                 Wakeword-Server (LXC)              Orchestrator (VPS)
┌──────────┐    WebSocket     ┌─────────────────────┐  Tailscale  ┌──────────────┐
│          │ ──────────────── │  /ws/audio           │ ─────────── │              │
│  Audio   │   Opus 16kHz    │  openWakeWord detect │  WS relay   │  main.py     │
│  Mgmt    │                 │                      │             │  :5000       │
│          │    HTTP          │  /device_config      │   httpx     │              │
│          │ ──────────────── │  /heartbeat          │ ─────────── │              │
│          │   Bearer token   │  /device_status      │  HTTP proxy │              │
│          │   (LAN only)     │  /room_temperature   │  (Tailscale)│              │
│          │                 │  /speaker/suppress   │             │              │
└──────────┘                 └─────────────────────┘              └──────────────┘
```

**Flusso audio:**
1. AtomS3R invia audio Opus continuo via WebSocket al wakeword-server (LAN)
2. openWakeWord rileva la wake word ("hey jarvis")
3. Il relay apre una connessione WS on-demand all'orchestrator via Tailscale
4. L'audio viene inoltrato in pass-through (zero decodifica)
5. Al termine (tts_done), il relay si chiude. Zero bandwidth quando idle.

**Flusso management:**
1. AtomS3R invia chiamate HTTP (config, heartbeat, etc.) al wakeword-server (LAN)
2. Il wakeword-server valida il bearer token localmente
3. Inoltra la richiesta all'orchestrator via httpx (Tailscale/WireGuard)
4. Ritorna la risposta al device

**Beneficio sicurezza:** Il bearer token non esce mai dalla LAN + tunnel Tailscale.

## Configurazione

Variabili d'ambiente (`.env` o Docker environment):

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `ORCHESTRATOR_WS_URL` | *(richiesto)* | WebSocket URL dell'orchestrator per il relay audio. Es: `ws://100.100.74.71:5000/ws/audio` |
| `ORCHESTRATOR_URL` | *(derivato da WS URL)* | HTTP base URL dell'orchestrator per il proxy management. Es: `http://100.100.74.71:5000`. Se vuoto, derivato automaticamente da `ORCHESTRATOR_WS_URL` |
| `DEVICE_API_TOKEN` | `""` | Bearer token per autenticazione device. Se vuoto, nessuna autenticazione (dev mode) |
| `WAKEWORD_MODEL` | `hey_jarvis` | Modello openWakeWord da usare |
| `WAKEWORD_THRESHOLD` | `0.5` | Soglia di rilevamento (0.1-1.0) |
| `MULTIROOM_COOLDOWN_S` | `5` | Cooldown multi-room in secondi |
| `LISTEN_PORT` | `8200` | Porta di ascolto del server |

## Endpoint

### WebSocket

| Endpoint | Descrizione |
|----------|-------------|
| `WS /ws/audio?device_id=XX&token=YY` | Connessione audio persistente per wake word detection + relay |

### REST API (gestione interna)

| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/health` | GET | Health check + lista device connessi |
| `/api/devices` | GET | Stato dettagliato dei device connessi |
| `/api/config/{device_id}` | POST | Push soglia wake word a un device connesso |
| `/api/trigger_listen/{device_id}` | POST | Trigger ascolto (enrollment da dashboard) |

### Proxy Management (AtomS3R → Orchestrator via Tailscale)

| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/device_config` | GET | Configurazione device (friendly_name, location_id) |
| `/heartbeat` | POST | Heartbeat periodico del device |
| `/device_status` | GET/POST | Stato device (speaking, DND) |
| `/room_temperature/{room}` | GET | Temperatura della stanza |
| `/speaker/suppress` | POST | Silenzia speaker (multiroom) |

## Deploy

### Prerequisiti
- LXC container su Proxmox con Docker + Tailscale
- Connettivita Tailscale verso l'orchestrator VPS

### Deploy rapido (da Proxmox host)
```bash
bash jarvis/cloud/scripts/deploy-wakeword.sh
```

### Deploy Ansible (da qualsiasi host con SSH)
```bash
ansible-playbook jarvis/infrastructure/ansible/playbooks/wakeword.yml \
  -i inventory/hosts.yml
```

### Deploy manuale
```bash
ssh root@<wakeword-server-ip>
cd /opt/jarvis-wakeword/wakeword-server
git pull
docker compose up -d --build
```

### Firmware AtomS3R
Nel file `sdkconfig.local`:
```
CONFIG_JARVIS_SERVER_HOST="<IP_WAKEWORD_SERVER>"
CONFIG_JARVIS_SERVER_PORT=8200
CONFIG_JARVIS_WS_URL="ws://<IP_WAKEWORD_SERVER>:8200/ws/audio"
CONFIG_USE_LOCAL_WAKEWORD=n
```

## Troubleshooting

```bash
# Log container
docker logs -f jarvis_wakeword

# Health check
curl http://localhost:8200/health

# Device connessi
curl http://localhost:8200/api/devices

# Test proxy verso orchestrator
curl -H "Authorization: Bearer TOKEN" http://localhost:8200/device_config?device_id=TEST

# Test WebSocket (da dentro il container)
docker exec jarvis_wakeword python3 -c "
import asyncio, websockets
async def test():
    async with websockets.connect('ws://ORCHESTRATOR_TAILSCALE_IP:5000/ws/audio?device_id=test&token=TOKEN') as ws:
        print('Connected!')
        await ws.close()
asyncio.run(test())
"
```
