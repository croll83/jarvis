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
| `HUB_PORT` | `0` | Porta TCP del Voice Live Hub per le sessioni live full-duplex (Gemini Live). `0` = funzione spenta, comportamento di oggi |
| `HUB_HOST` | `""` | Host del Voice Live Hub. Vuoto = funzione spenta, come `HUB_PORT=0` (l'host nel `live_session_start` dell'orchestrator non viene usato) |
| `AEC_BACKEND` | `webrtc` | Cancellazione d'eco dell'Atom in sessione hub: `webrtc` (AEC3 via pacchetto `livekit`), `speex` (libspeexdsp), `off` |
| `WAKE_PREROLL_S` | `2.0` | Secondi di pre-roll (ring PCM per device riempito in IDLE, anche durante il cooldown multiroom) |
| `WAKE_PREROLL_SHADOW` | `1` | A ogni rilevamento classico manda `wake_preroll` all'orchestrator (voice ID in shadow, solo punteggi nei log). Non cambia niente per il device |
| `WAKE_CLIP_ARCHIVE` | `1` | Salva il pre-roll di ogni rilevamento in WAV (`<UTC>_<device>_s<score>.wav`) per riaddestrare il modello con i falsi positivi |
| `WAKE_CLIP_DIR` | `/app/data/wakeclips` | Cartella dell'archivio clip (volume `wakeword_data`) |
| `WAKE_CLIP_MAX_FILES` / `WAKE_CLIP_MAX_MB` | `3000` / `200` | Tetti dell'archivio: oltre si cancellano le clip piu' vecchie. `0` = nessun tetto su quella misura |
| `WAKE_VOICE_GATE` | `auto` | Cancello a voce: `auto` segue il flag `wake_voice_gate` del welcome dell'orchestrator, `1` sempre, `0` mai. Con il cancello attivo la wake aspetta il verdetto `wake_gate`; `drop` = wake ignorata in silenzio |
| `WAKE_VOICE_GATE_TIMEOUT_S` | `1.0` | Attesa massima del verdetto; senza verdetto la wake passa |
| `LIVE_ON_WAKE` | `0` | Interruttore generale "dalla wake word (o dal tap) a Gemini Live". Serve anche `live_on_wake` nel welcome dell'orchestrator e, per le wake rilevate qui, la cap `live_wake` del firmware |
| `LIVE_ACK_TIMEOUT_S` / `LIVE_START_TIMEOUT_S` / `LIVE_ACCEPT_TIMEOUT_S` | `1.0` / `2.5` / `7.0` | Attese di `live_wake_ack` dal device, della risposta a `live_start` e dell'accept tardivo del hub |
| `LIVE_COMMAND_MAX_S` | `8` | Tetto del buffer del comando detto subito dopo la wake word o il tap |
| `LIVE_COMMAND_MIN_SPEECH_MS` / `LIVE_COMMAND_MARGIN_DB` / `LIVE_COMMAND_MIN_DBFS` / `LIVE_COMMAND_CHIRP_SKIP_MS` | `300` / `10` / `-65` / `250` | VAD a energia sul buffer: sopra la soglia di parlato l'offer al hub porta `command: true`; i ms dopo l'ack (chirp dell'Atom) non contano |

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

## Dalla wake word a Gemini Live (Fase 3, design 19)

Tutto spento di default tranne shadow del pre-roll e archivio clip. Il peer chiede,
l'orchestrator decide: il server non apre mai il hub senza `call_id` dell'orchestrator.
Ogni richiesta ha un `req_id` e ogni risposta lo riporta.

**Wake rilevata qui** (Atom con cap `live_wake`, abilitato):
1. snapshot del pre-roll (finisce sul rilevamento), clip in archivio, stato `LIVE_PENDING`;
2. con il cancello a voce: `wake_preroll{req_id,gate:true,...}` all'orchestrator e attesa di
   `wake_gate{req_id,action:"keep"|"drop"}`;
3. `wake_detected{live:true,req_id}` al device, che risponde `live_wake_ack{req_id,status:"ok"|"dnd"|"busy"}`;
4. con `ok`: `live_start{req_id,trigger:"wake",preroll:{codec,rate,b64},score}` all'orchestrator;
   intanto il PCM decodificato va nel buffer del comando (tetto 8 s);
5. risposta: `live_session_start{backend:"hub",req_id,origin,offer_extra,...}` -> offer al hub
   con `offer_extra` copiato (oggetto e chiavi al primo livello) piu' `command`, accept fino a 7 s, `live_session_active{call_id,req_id}`
   all'orchestrator, buffer al hub per primo, poi il duplex di oggi.
   `live_declined{req_id,reason}` o 2,5 s senza risposta (`live_cancel{req_id}`) o relay giu':
   `wake_detected` normale (classico). `reason` `duplicate` o `voice_gate`: `live_abort{req_id}`, a riposo.
6. Hub che rifiuta o non risponde: `live_session_end{call_id,reason,detail?}` e poi `wake_detected` normale.
7. Tap (`audio_start`), doppio tap (`speaker_stop`), pressione lunga (`audio_end`) in `LIVE_PENDING`:
   `live_cancel{req_id}` e poi il comportamento di oggi.

**Richiesta dal device**: `live_start{req_id,trigger:"wake"|"tap",preroll_b64?}` (NabuVoice con
la wake locale, o tap su qualunque device). Dopo il JSON la NabuVoice manda solo PCM, accettato
in `LIVE_PENDING` come buffer del comando. Uscite: `live_session_start` duplex,
`live_declined{req_id,reason}` (il device fa il suo classico), `live_abort{req_id}`.

**Flag in cache**: `live_on_wake` e `wake_voice_gate` si leggono dal `welcome` (a ogni apertura del
relay) e dal messaggio `live_on_wake{live_on_wake}` che l'orchestrator manda quando il toggle cambia
in admin; entrambi vanno anche al device come oggi.

**Annunci**: `announce{call_id,text,urgent}` dall'orchestrator va al hub come frame di controllo
sulla sessione attiva, mai al device.

## AGC software (pre-wakeword)

Il microfono dell'AtomS3R arriva con livelli molto bassi e openWakeWord rileva
male sotto ~1/4 del fondo scala (detection ~1/20 anche a 5 cm). Prima
dell'inferenza, `wakeword.py` applica un gain adattivo per-chunk:

- **Attack istantaneo**: se il chunk richiede più gain, si salta subito al
  target (la wakeword dura pochi chunk: l'onset deve essere già amplificato).
- **Release lento** (0.9/0.1) per non pompare il rumore di fondo.
- **Pre-arming in silenzio**: sotto il floor il gain risale gradualmente verso
  il massimo, così la prima sillaba di voce lontana parte già amplificata.

Tuning via env (docker-compose):

| Env | Default | Significato |
|---|---|---|
| `WAKEWORD_AGC_TARGET` | 24000 | picco int16 verso cui normalizzare |
| `WAKEWORD_AGC_MAX` | 20 | gain massimo |
| `WAKEWORD_AGC_FLOOR` | 80 | sotto questo picco il chunk è rumore |
| `WAKEWORD_THRESHOLD` | 0.5 | soglia openWakeWord (abbassare se serve) |

Deploy: i sorgenti vivono nel monorepo (`wakeword-server/`); su CT201
`pct push` dei file in `/opt/jarvis-wakeword/` + `docker compose up -d --build`.
