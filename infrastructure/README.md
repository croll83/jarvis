# JARVIS — Deploy Locale (GPU + AI Agent su LXC separato)

Guida completa per il deploy locale di JARVIS su Proxmox con GPU NVIDIA.
Il router locale (Qwen 2.5 3B) gira on-premise su un **LXC con GPU device sharing**
(driver NVIDIA installato sull'host Proxmox, GPU condivisa via cgroup2 — NON PCIe
passthrough esclusivo). STT e TTS girano sul **GX10 DGX Spark** (128 GB VRAM) via Tailscale.
Il reasoning e gestito da Cloud LLM via AI Agent che gira **bare-metal su un
LXC dedicato e separato** per isolamento di sicurezza.
Tailscale gira host-level (non in Docker) su tutti i container/VM per raggiungere
HA remoti e il LXC AI Agent.

> **NOTA IMPORTANTE — GPU Device Sharing vs Passthrough:**
> La GPU **non** e assegnata in esclusiva a nessuna VM/LXC tramite PCIe passthrough.
> Il driver NVIDIA gira sull'**host Proxmox** e la GPU e condivisa con l'LXC jarvis
> tramite device binding (`/dev/nvidia*` + cgroup2). Questo significa:
> - Zero overhead di virtualizzazione GPU
> - La GPU resta disponibile anche per l'host (e potenzialmente altri container)
> - Non servono IOMMU, vfio-pci, o UEFI
> - Setup piu semplice e performance native

---

## Architettura Hardware

```
+=====================================================================+
|  HARDWARE: Proxmox Host (es. AtomMan G7 Pro)                        |
|  CPU: 32 vCPU (Intel Core i9) | RAM: 64 GB | GPU: RTX 5070 Laptop 8GB|
|  Disco: 2 TB NVMe                                                   |
|  NVIDIA Driver: installato SULL'HOST (nvidia-smi funziona qui)       |
|  Schermo + tastiera + mouse collegati fisicamente                    |
+=====================================================================+
|                                                                      |
|  [1] LXC-JARVIS (Ubuntu 22.04) — GPU device sharing via cgroup2     |
|  Device /dev/nvidia* montati dall'host (NO PCIe passthrough)         |
|  NVIDIA Container Toolkit installato (no-cgroups=true per LXC)       |
|  Tailscale host-level (100.x.x.x) - jarvis-wagmi                   |
|                                                                      |
|  docker-compose.yml                                                  |
|  +----------------------------------------------------------------+ |
|  |                     jarvis_network                              | |
|  |                                                                 | |
|  |  ollama:11434   postgres:5432   mongo:27017                     | |
|  |  GPU: ~2.6 GB   (side proj)    (side proj)                     | |
|  |  Qwen 2.5 3B                                                   | |
|  |  (LLM only)                                                    | |
|  |  ctx=32768                                                     | |
|  |                                                                 | |
|  |  fastembed:11435 (CPU, no GPU)                                  | |
|  |  nomic-embed-text-v1.5 ONNX — Ollama-compat API                | |
|  |  (mem0-stack — vector + graph + LLM router —                    | |
|  |   vive in repo separato croll83/mem0-stack;                     | |
|  |   consumato via MEM0_BASE_URL)                                  | |
|  |  orchestrator:5000 (network_mode: host)                        | |
|  |  FastAPI + Admin UI                                             | |
|  |  Speaker ID (Resemblyzer)                                       | |
|  |  Internal TTS (Qwen3-TTS@GX10 + Opus streaming per AtomS3R)   | |
|  |  SQLite + Redis (context bus) + mem0 (long-term)               | |
|  |                                                                 | |
|  |  ontology-server:8100 (127.0.0.1 only)                         | |
|  |  Knowledge Graph API — SQLite + ACL (X-Speaker-Id)             | |
|  +----------------------------------------------------------------+ |
|                                                                      |
|  Nginx (:80, :443) — TLS proxy per jarvis.mintwork.it               |
|  Cloudflared — tunnel per jarvis-pub.mintwork.it                     |
|                                                                      |
|  GPU VRAM Budget (misurato — GPU dedicata, no display):              |
|  +-- Qwen 2.5 3B Q4_K_M (weights+KV) . ~2.6 GB (@ ctx=32768)     |
|  +-- TOTALE ........................... ~2.6 GB / 8.15 GB VRAM     |
|  +-- LIBERI ........................... ~5.5 GB (per router upgrade)|
|  Nota: STT (Parakeet) e TTS (Qwen3-TTS) su GX10 — zero VRAM locale|
|  Nota: Embeddings su fastembed CPU (:11435) — zero VRAM             |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [2] LXC-AI-Agent (Ubuntu 22.04, NO Docker, NO GPU)                 |
|  Node.js bare-metal | systemd service                                |
|  Tailscale host-level - jarvis-ai-agent                              |
|                                                                      |
|  AI Agent gateway :18789 (localhost only)                            |
|  Chrome headless  :18800 (CDP, solo localhost)                       |
|  Cloud LLM (API cloud)                                               |
|  Telegram bot integrato                                              |
|  Linuxbrew + skill dependencies                                      |
|  XTTS Proxy (:8891) — traduce OpenAI TTS → XTTSv2 nativo            |
|                                                                      |
|  Nginx reverse proxy (TLS termination):                              |
|    :18789 (Tailscale IP) -> ws://127.0.0.1:18789  (API + WSS)       |
|    :443   (Tailscale IP) -> http://127.0.0.1:18789 (Dashboard)      |
|  Certbot + Cloudflare DNS per TLS termination                        |
|                                                                      |
|  Skill (copiata nella directory dell'agent)                          |
|                                                                      |
|  Plugin:                                                             |
|  browser-dom (DOM automation via CDP)                                |
|                                                                      |
|  Raggiungibile via:                                                  |
|  - API/WS (TLS): https://your-agent-host:18789 (wss://)             |
|  - Dashboard:    https://your-agent-host (porta 443)                 |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [3] LXC-Wakeword (1 per casa — stessa LAN degli AtomS3R)           |
|  CPU: 1 vCPU | RAM: 2 GB | Disco: 10 GB                            |
|  Docker: jarvis-wakeword-server :8200                                |
|  Tailscale host-level (100.x.x.x) — raggiungibile dall'orchestrator |
|                                                                      |
|  openWakeWord: modelli ~2MB, ~80ms/inference su CPU                  |
|  Opus decode + wake word detection per 4-5 device                    |
|  Multi-room cooldown (5s)                                            |
|                                                                      |
|  Rete: AtomS3R → LAN locale (:8200/ws/audio)                        |
|  Tailscale: orchestrator VPS → push config + trigger_listen          |
|  Relay: → LXC-JARVIS/VPS orchestrator via Tailscale (on-demand)     |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [4] VM-Workstation (Ubuntu + XFCE — vera VM KVM, NO GPU dedicata)   |
|  CPU: 6 core | RAM: 12 GB | Disco: 400 GB                           |
|  Display: VirtIO-GPU (QXL) | Accesso: RDP (xrdp) + noVNC Proxmox   |
|                                                                      |
|  Chrome (reale, non headless) + AI Agent browser extension           |
|  Cursor IDE, Git, Node.js (nvm), Python 3                            |
|  Workspace di sviluppo, email, browsing                              |
|                                                                      |
|  Accesso da host Proxmox: desktop XFCE locale + Remmina (RDP)       |
|  Accesso da LAN: RDP diretto all'IP della VM                        |
|  Accesso da ovunque: noVNC da Proxmox Web UI                        |
|                                                                      |
|  Vedi: infrastructure/WORKSTATION.md per setup completo              |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [5] VM-HAOS (opzionale — Home Assistant OS, vera VM KVM)            |
|  CPU: 2 core | RAM: 8 GB | Disco: 64 GB                            |
|  Porta: :8123 (HA API)                                               |
|  Zigbee/Z-Wave dongle via USB passthrough                            |
|  Raggiungibile dall'orchestrator via LAN o Tailscale                 |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|  [6] LXC-Alexa (opzionale — Alexa Media Server)                     |
|  CPU: 1 core | RAM: 1 GB | Disco: 5 GB                             |
|  Fa funzionare gli Amazon Echo come speaker di JARVIS                |
|  Comunica con HAOS via integrazione alexa_media                      |
+---------------------------------------------------------------------+

  Topologia di rete:

  LXC-JARVIS <--- LAN / Tailscale ---> LXC-AI-Agent
      ^
      |  (Tailscale, on-demand relay + push config)
      |
  LXC-Wakeword (LAN + Tailscale) <--- WiFi --- AtomS3R devices

  LXC-JARVIS <--- LAN ---> VM-HAOS (:8123)
  LXC-JARVIS <--- Tailscale ---> HA remoti (Milano, ecc.)

  Host Proxmox --- schermo locale --- desktop XFCE + Remmina
      |
      +--- RDP locale -----> VM-Workstation (Chrome + IDE + dev)
      +--- RDP locale -----> altre VM (se servono)
      +--- Browser locale -> Proxmox Web UI (:8006)
```

### Ordine di boot

I container/VM sono indipendenti su Proxmox e si avviano in parallelo.

**LXC-AI-Agent** (boot autonomo):
```
systemd -> tailscaled.service -> ai-agent-chrome.service (Chrome CDP :18800)
                              -> ai-agent.service (Node.js, porta 18789)
```
`ai-agent-chrome.service` ha `Before=ai-agent.service`, quindi Chrome parte prima del gateway.

**LXC-JARVIS** (boot sequenziale):
```
1. tailscaled      -> host-level service, si connette alla tailnet
2. ollama          -> diventa healthy (modelli caricati)
   (mem0-stack    -> vive in repo separato croll83/mem0-stack;
                     deploy indipendente — consumato via MEM0_BASE_URL)
3. orchestrator    -> aspetta ollama, poi parte (network_mode: host)
                      vede Tailscale direttamente, raggiunge GX10 + AI Agent
                      STT (Parakeet :7865) e TTS (Qwen3-TTS :9880) su GX10
4. nginx           -> started (TLS per jarvis.mintwork.it)
5. cloudflared     -> started (tunnel per jarvis-pub.mintwork.it)
```

**LXC-Wakeword** (boot autonomo, 1 per casa):
```
1. tailscaled      -> host-level service, si connette alla tailnet
2. docker          -> started
3. wakeword-server -> healthcheck :8200/health, si connette a VPS orchestrator via relay
```

### Cosa gira dove

| Servizio | Dove gira | Container/Processo | CPU | RAM | GPU/VRAM | Funzione |
|----------|-----------|-------------------|-----|-----|----------|----------|
| **NVIDIA Driver** | **Host Proxmox** | kernel module | - | - | - | Driver GPU, `nvidia-smi` funziona qui |
| **Tailscale** | LXC-JARVIS | host-level (`tailscaled.service`) | - | 64 MB | - | VPN mesh per HA remoti + AI Agent |
| **Ollama** | LXC-JARVIS | `jarvis_ollama` (Docker, `--gpus all`) | - | - | ~2.5 GB | Qwen 2.5 3B routing + tool calling (ctx=32768) — solo LLM |
| **fastembed** | LXC-JARVIS | `jarvis_fastembed` (Docker, CPU) | 0.5 | 300 MB | - | nomic-embed-text-v1.5 ONNX embeddings (Ollama-compat :11435) |
| **Parakeet STT** | GX10 DGX Spark | `parakeet-stt.service` (systemd) | - | - | ~5.1 GB | STT multilingue (nvidia/parakeet-tdt-0.6b-v3) |
| **Qwen3-TTS** | GX10 DGX Spark | `qwen3-tts.service` (systemd) | - | - | ~4.4 GB | TTS voice cloning IT/EN (Qwen3-TTS-12Hz-1.7B) |
| **Dark Jarvis (heavy LLM)** | GX10 DGX Spark | `dark-jarvis.service` (systemd) | - | - | ~30 GB | Qwopus3.6-27B-Abl-MTP-NVFP4 + MTP spec decode su `:30000` (`dark-jarvis`/`dark-opus`). Setup: [`gb10/dark-jarvis.md`](gb10/dark-jarvis.md) |
| **Orchestrator** | LXC-JARVIS | `jarvis_core` (`network_mode: host`) | 1-2 | 2 GB | - | FastAPI, HA control, memory, security |
| **mem0-stack** | esterno | repo `croll83/mem0-stack` | - | - | - | Long-term semantic + procedural memory (vector + graph + LLM router). Consumato via `MEM0_BASE_URL` |
| **Ontology Server** | LXC-JARVIS | `jarvis_ontology` (Docker, 127.0.0.1:8100) | 0.5 | 256 MB | - | Knowledge Graph API + ACL |
| **PostgreSQL** | LXC-JARVIS | `jarvis_postgres` (Docker) | 0.5 | 512 MB | - | Database side projects |
| **MongoDB** | LXC-JARVIS | `jarvis_mongo` (Docker) | 0.5 | 512 MB | - | Database side projects |
| **Nginx** | LXC-JARVIS | `nginx` (systemd) | 0.1 | 64 MB | - | TLS proxy per jarvis.mintwork.it (:80, :443) |
| **Cloudflared** | LXC-JARVIS | `cloudflared` (systemd) | 0.1 | 64 MB | - | Tunnel per jarvis-pub.mintwork.it |
| **AI Agent** | LXC-AI-Agent (bare-metal) | `ai-agent.service` (systemd) | 0.5 | 512 MB | - | Cloud LLM brain (API cloud) |
| **Chrome Headless** | LXC-AI-Agent (bare-metal) | `ai-agent-chrome.service` (systemd) | 0.5 | <=1 GB | - | Browser automation via CDP :18800 |
| **TTS Proxy** | LXC-AI-Agent (bare-metal) | `xtts-proxy.service` (systemd) | 0.1 | 64 MB | - | Proxy TTS per AI Agent → Qwen3-TTS@GX10 (:8891) |
| **Wakeword Server** | LXC-Wakeword (1/casa) | `jarvis_wakeword` (Docker) | 1 | 2 GB | - | openWakeWord detection + relay :8200 |
| **Workstation** | VM-Workstation (opz.) | KVM VM (Ubuntu + XFCE) | 6 | 12 GB | - | Chrome reale + AI Agent ext + IDE + dev |
| **HAOS** | VM-HAOS (opz.) | KVM VM | 2 | 8 GB | - | Home Assistant OS + MASS + add-ons |
| **Alexa Media** | LXC-Alexa (opz.) | Docker/nativo | 1 | 1 GB | - | Echo come speaker JARVIS |

### Speaker Interno (AtomS3R Mobile)

Un AtomS3R con batteria accessoria può funzionare in mobilità senza speaker HA esterno.
Quando "Speaker Interno" è attivo per un device, il TTS viene generato dall'orchestrator
via XTTSv2 (locale) o Kokoro (cloud) e inviato come frame Opus via WebSocket direttamente
allo speaker integrato del device (ES8311 DAC + NS4150B amp).

**Flusso (locale — Qwen3-TTS su GX10):**
```
AI Response → Qwen3-TTS@GX10 (PCM 24kHz streaming) → resample 16kHz → Opus encode → WS binary → Device speaker
```

**Flusso (cloud):**
```
AI Response → Kokoro (PCM 24kHz streaming) → resample 16kHz → Opus encode → WS binary → Device speaker
```

**Nessuna modifica firmware**: il firmware AtomS3R gestisce già la ricezione e decodifica
di frame Opus binari via WebSocket (opcode 0x02 in `jarvis_ws_audio.c`).

**Configurazione**: Dashboard orchestrator → Dispositivi → checkbox "Speaker Interno".
Quando attivo, i campi Speaker Principale e Speaker Fallback vengono ignorati.

**Voice**: Qwen3-TTS ha voci preconfigurate (sofia, marco, emma, james) + voice cloning.
Configurabile via `QWEN3_TTS_VOICE` (default: `sofia`).

---

## Requisiti

### Host Proxmox

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 8 core x86_64 | 32 vCPU (es. Intel Core i9) |
| RAM | 32 GB | 64 GB |
| GPU | NVIDIA 8 GB VRAM | RTX 5070 Laptop 8 GB / RTX 5070 Ti 16 GB |
| Disco | 500 GB SSD | 2 TB NVMe |
| OS | Proxmox VE 8.x | latest |
| NVIDIA Driver | installato sull'host | latest |

### LXC-JARVIS (GPU)

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 4 core | 8 core |
| RAM | 16 GB | 20 GB |
| Disco | 100 GB | 250 GB |
| OS | Ubuntu 22.04+ | - |
| Docker | 24.0+ | latest |
| Docker Compose | 2.20+ | latest |
| NVIDIA Container Toolkit | installato (`no-cgroups=true`) | latest |
| Tailscale | installato host-level | latest |

### LXC-AI-Agent

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 1 core | 2 core |
| RAM | 1 GB | 2 GB |
| Disco | 10 GB | 20 GB |
| OS | Ubuntu 22.04+ / Debian 12+ | - |
| Node.js | 22+ | 22 LTS |
| Google Chrome | stable | latest |
| GPU | Non richiesta | - |
| Tailscale | installato host-level | latest |

### LXC-Wakeword (1 per casa)

| Componente | Minimo | Consigliato |
|------------|--------|-------------|
| CPU | 1 core | 1 core |
| RAM | 1 GB | 2 GB |
| Disco | 5 GB | 10 GB |
| OS | Ubuntu 22.04+ / Debian 12+ | - |
| Docker | 24.0+ | latest |
| Tailscale | installato host-level | latest |
| GPU | Non richiesta | - |

---

## Installazione (Step-by-Step)

### Prerequisiti

Prima di iniziare, raccogli:
- **API keys**: Cloud LLM ([aistudio.google.com](https://aistudio.google.com/app/apikey))
- **Telegram bot tokens**: 2 bot da @BotFather (uno per AI Agent, uno per Approval)
- **HA long-lived token**: Home Assistant > Profilo > Token di lunga durata
- **Proxmox API token**: vedi [PROXMOX.md](PROXMOX.md) sezione 3
- **Cloudflare API token**: per certbot DNS challenge e Cloudflare Tunnel
- **Cloudflare Tunnel**: configurato da dashboard Zero Trust
- **Terraform + Ansible** installati sul Mac (`brew install terraform ansible`)

### FASE 1 — Proxmox Host (manuale, una tantum)

Operazioni sull'host che non possono essere automatizzate.

1. **Installa driver NVIDIA** sull'host → [PROXMOX.md](PROXMOX.md) sezione 1
2. **Verifica device** → `ls -la /dev/nvidia*` — annota i major number per `terraform.tfvars`
3. **Crea API token** per Terraform → [PROXMOX.md](PROXMOX.md) sezione 3
4. **(Opzionale) Desktop XFCE + Remmina** sull'host → [PROXMOX.md](PROXMOX.md) sezione 9

### FASE 2 — Terraform (dal Mac — crea infrastruttura)

Terraform crea i container LXC e le VM su Proxmox via API.

```bash
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars    # IP Proxmox, API token, scegli componenti

terraform init           # scarica il provider Proxmox (prima volta)
terraform plan           # anteprima di cosa verra' creato
terraform apply          # crea tutto
```

Scegli cosa creare abilitando i flag in `terraform.tfvars`:

```hcl
jarvis_enabled      = true     # LXC-JARVIS (GPU + Ollama + Orchestrator)
ai_agent_enabled    = true     # LXC-AI-Agent (Gateway Cloud LLM)
workstation_enabled = true     # VM-Workstation (Ubuntu Desktop + Chrome)
# wakeword: si abilita aggiungendo istanze a wakeword_instances
```

**Dopo `terraform apply`** — se hai abilitato LXC-JARVIS, esegui lo script GPU sull'host:

```bash
# Copia ed esegui configure-gpu.sh sull'host Proxmox
scp configure-gpu.sh root@<proxmox-ip>:~/
ssh root@<proxmox-ip> "bash ~/configure-gpu.sh"
```

### FASE 3 — VM Workstation (manuale + Ansible)

Prerequisito: `workstation_enabled = true` in Fase 2.

1. **Scarica ISO Ubuntu** su Proxmox → [WORKSTATION.md](WORKSTATION.md) Step 1
2. **Installa Ubuntu** via noVNC (manuale) → [WORKSTATION.md](WORKSTATION.md) Step 3
3. **Prepara la VM per Ansible** (dalla console noVNC della VM):
   ```bash
   sudo apt update && sudo apt install -y openssh-server
   echo "jarvis ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/jarvis
   ```
4. **Copia chiave SSH** (dal Mac):
   ```bash
   ssh-copy-id jarvis@<IP_VM_WORKSTATION>
   ```
5. **Lancia Ansible**:

```bash
cd infrastructure/ansible
cp inventory/hosts.yml.example inventory/hosts.yml
nano inventory/hosts.yml   # inserisci IP della VM workstation

ansible-playbook playbooks/workstation.yml
```

Installa: GNOME Remote Desktop (RDP nativo), Chrome, Git, nvm + Node.js, Python 3, Zed IDE, Tailscale, UFW.

6. Post-setup manuale: configura Git, connetti Tailscale, installa estensione AI Agent su Chrome.
   Vedi [WORKSTATION.md](WORKSTATION.md) Step 4 per dettagli.

### FASE 4 — LXC-JARVIS (Ansible)

Prerequisito: `jarvis_enabled = true` in Fase 2, `configure-gpu.sh` eseguito.

```bash
cd infrastructure/ansible
cp group_vars/all.yml.example group_vars/all.yml
nano group_vars/all.yml    # API keys, AI_AGENT_URL, HA token, password DB
nano inventory/hosts.yml   # aggiungi IP del LXC-JARVIS

ansible-playbook playbooks/site.yml --tags common,nvidia,jarvis,verify
```

Il playbook:
1. Installa Docker + NVIDIA Container Toolkit (con `no-cgroups=true` per LXC)
2. Clona il repository, genera `.env` da template
3. Esegue `docker compose up -d` (Ollama, Orchestrator, Ontology, Postgres, Mongo — STT/TTS su GX10)
4. Scarica i modelli AI (`setup.sh` — Qwen 2.5 3B) + avvia fastembed (embeddings CPU)
5. Verifica health di tutti i servizi
6. Installa Nginx + Certbot (cert SSL via Cloudflare DNS per jarvis.mintwork.it)
7. Installa Cloudflared (tunnel per jarvis-pub.mintwork.it)

Post-setup manuale:

```bash
# SSH nel LXC-JARVIS
tailscale up --hostname=jarvis-wagmi    # auth manuale

# Accedi alla dashboard
# http://<IP>:5000/admin
# Crea utenti, enroll voci, configura location + entity map
```

### FASE 5 — LXC-AI-Agent (Ansible + onboarding manuale)

Prerequisito: `ai_agent_enabled = true` in Fase 2.

Installa il tuo AI Agent (Hermes/OpenClaw/altri) seguendo la documentazione specifica
del software scelto. L'orchestrator lo tratta come blackbox e si connette via
`AI_AGENT_URL` (HTTPS/WSS) autenticandosi con `AI_AGENT_TOKEN`.

Post-setup manuale:

```bash
# SSH nel LXC-AI-Agent
su - jarvis

# Installa e configura l'AI Agent secondo la sua documentazione
# Copia le skill JARVIS nella directory dell'agent:
cp /opt/jarvis/jarvis-orchestrator/skill/* <agent-skills-dir>/jarvis-orchestrator/
cp -r /opt/jarvis/ontology-server/skill/* <agent-skills-dir>/ontology/

# Connetti Tailscale
tailscale up --hostname=jarvis-ai-agent

# Avvia l'agent e verifica
curl http://localhost:18789/health
```

### FASE 6 — LXC-Wakeword (Ansible, opzionale)

Prerequisito: istanze wakeword configurate in `terraform.tfvars`.

```bash
cd infrastructure/ansible
ansible-playbook playbooks/wakeword.yml -e "wakeword_host=<IP_LXC_WAKEWORD>"
```

Post-setup:

```bash
# Nel LXC wakeword
tailscale up --hostname=jarvis-wakeword-casa1

# Nel .env dell'orchestrator (LXC-JARVIS), aggiungi:
WAKEWORD_SERVER_URLS={"tua_location_id": "http://<TAILSCALE_IP_WAKEWORD>:8200"}

# Riavvia orchestrator
docker compose restart orchestrator
```

### Post-installazione

1. **Telegram webhook** — configura il webhook del bot AI Agent:
   ```bash
   curl "https://api.telegram.org/bot<AI_AGENT_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<tuo-dominio>/telegram_webhook"
   ```

2. **Dashboard admin** — `http://<JARVIS_IP>:5000/admin`:
   - Crea utenti e assegna ruoli
   - Enroll voci (speaker identification con Resemblyzer)
   - Configura location e entity maps
   - Monitora stato servizi

3. **Dashboard AI Agent** — `https://your-agent-host` (TLS via Nginx):
   - Gestisci skill registrate
   - Vedi log conversazioni
   - Monitora stato gateway

4. **Estensione AI Agent** — nella VM Workstation:
   - Apri Chrome, installa l'estensione dal Web Store
   - Configura URL gateway: `https://your-agent-host:18789`

5. **Nginx + Tunnel** — verifica accesso:
   ```bash
   # HTTPS via Tailscale
   curl -k https://jarvis.mintwork.it/health

   # Tunnel (pubblico)
   curl https://jarvis-pub.mintwork.it/health
   ```

---

## Configurazione Avanzata

### Parametri LLM (runtime, da dashboard)

I parametri LLM sono in `global_preferences` nel database. Modificabili dalla dashboard admin senza riavvio:

| Chiave | Descrizione |
|--------|-------------|
| `llm_params` | Temperature, max_tokens, timeout per routing/reasoning/quick/summary/gemini |
| `default_location_id` | Location di default |
| `default_fallback_speaker` | Speaker entity_id di fallback |
| `security_announcement_speaker` | Speaker per annunci security |

Esempio modifica via API:
```bash
curl -X PUT http://localhost:5000/admin/preferences/llm_params \
  -H "Content-Type: application/json" \
  -d '{
    "routing": {"temperature": 0.1, "max_tokens": 500, "timeout": 10},
    "reasoning": {"temperature": 0.7, "max_tokens": 2000, "timeout": 120},
    "quick_response": {"temperature": 0.7, "max_tokens": 200, "timeout": 10},
    "gemini": {"temperature": 0.7, "max_tokens": 2000}
  }'
```

### Parametri restart-required (.env)

Questi richiedono riavvio del container orchestrator sulla LXC-JARVIS:

```env
# Timeouts (secondi)
AI_AGENT_TIMEOUT=30
TAILSCALE_TIMEOUT_REMOTE=15.0
TAILSCALE_TIMEOUT_LOCAL=10.0
TIMEOUT_STT=30
TIMEOUT_HA_READ=5

# Intervalli (secondi)
INTERVAL_CLEANUP=1800
INTERVAL_HEALTH_CHECK=30
INTERVAL_PROACTIVE=300
INTERVAL_PROACTIVE_COOLDOWN=1800

# Context budget (token)
CTX_ROUTING_USER_SQL=300
CTX_ROUTING_USER_VECTOR=200
CTX_REASONING_USER_SQL=800
CTX_REASONING_USER_VECTOR=600

# Soglie
VECTOR_SCORE_MIN_MESSAGES=0.3
VECTOR_SCORE_MIN_FACTS=0.4
VOICE_SIMILARITY_THRESHOLD=0.75

# Memory jobs
MEMORY_HOURLY_MINUTE=5
MEMORY_DAILY_HOUR=3

# STT/TTS (su GX10 via Tailscale)
STT_URL=http://100.98.187.12:7865
STT_ENGINE=parakeet
TTS_ENGINE=qwen3tts
QWEN3_TTS_URL=http://100.98.187.12:9880
QWEN3_TTS_VOICE=sofia

# Proactive monitoring
PROACTIVE_DOOR_OPEN_MINUTES=30
PROACTIVE_NO_MOTION_HOURS=12
PROACTIVE_NIGHT_START=2
PROACTIVE_NIGHT_END=5
```

### Prompt templates

I prompt di sistema sono in `jarvis-orchestrator/prompts/`. Modificabili senza toccare il codice:

```
prompts/
  quick_response_system.txt     # System prompt per chat semplice
  user_hourly_summary.txt       # Prompt per summary orario
  user_daily_summary.txt        # Prompt per summary giornaliero
```

Per ricaricare senza riavvio:
```bash
curl -X POST http://localhost:5000/admin/prompts/reload
```

---

## Tailscale Multi-Location

Tailscale gira host-level su tutti i container/VM (LXC-JARVIS, LXC-AI-Agent, LXC-Wakeword), non come container Docker.
L'orchestrator usa `network_mode: host` e vede l'interfaccia Tailscale direttamente.
Permette all'orchestrator di raggiungere HA remoti e il LXC-AI-Agent senza aprire porte.

### Dove serve Tailscale

| Nodo | Dove gira | Ruolo | Hostname |
|------|-----------|-------|----------|
| **Napoli (Wagmi)** | Host-level nel LXC-JARVIS | Gateway VPN per lo stack | `jarvis-wagmi` |
| **LXC-AI-Agent** | Host-level nel LXC dedicato | Espone AI Agent sulla tailnet | `jarvis-ai-agent` |
| **LXC-Wakeword** | Host-level nel LXC wakeword | Orchestrator → push config, trigger_listen | `jarvis-wakeword-<casa>` |
| **GX10 DGX Spark** | Host-level (Ubuntu) | Parakeet STT, Qwen3-TTS, ACE-Step, ComfyUI | `gx10-dgx` |
| **Milano (Albani)** | Add-on HAOS o host-level | Espone HA sulla tailnet | `ha-albani` |

### Schema di rete

```
+---------------------------------------------------------------+
|                    TAILSCALE MESH (100.x.x.x)                  |
|                                                                 |
|   Napoli LXC-JARVIS                    LXC-AI-Agent (bare-metal)  |
|   +-------------------+           +-------------------+        |
|   | jarvis-wagmi      |<--------->| jarvis-ai-agent   |        |
|   | Tailscale (host)  |           | Tailscale (host)  |        |
|   |                   |           |                   |        |
|   | Docker:           |           | AI Agent Gateway  |        |
|   |   Ollama (router) |           | Cloud LLM         |        |
|   |   Orchestrator    |           | Telegram bot      |        |
|   |    (net: host)    |           | JARVIS skill      |        |
|   |   Postgres, Mongo |           +-------------------+        |
|   |                   |                                        |
|   | HA Wagmi (locale) |                                        |
|   +-------------------+                                        |
|           |                                                     |
|           v                                                     |
|   Napoli GX10 DGX Spark           Milano (Mini PC)             |
|   +-------------------+           +-------------------+        |
|   | gx10-dgx          |           | ha-albani         |        |
|   | Tailscale (host)  |           | Home Assistant    |        |
|   |                   |           | Zigbee/Z-Wave     |        |
|   | Parakeet STT:7865 |           | Automazioni       |        |
|   | Qwen3-TTS  :9880  |           +-------------------+        |
|   | ACE-Step   :8760  |                                        |
|   | ComfyUI    :8188  |                                        |
|   +-------------------+                                        |
|                                                                 |
|   Napoli (LXC su stesso host Proxmox)                          |
|   +-------------------+                                        |
|   | jarvis-wakeword-  |  LAN :8200 <- AtomS3R devices         |
|   |  casa1            |  Tailscale <- orchestrator (config/    |
|   | Docker: wakeword  |              trigger_listen)           |
|   +-------------------+                                        |
|                                                                 |
|   wagmi -> ai-agent: https://your-agent-host:18789 (TLS)      |
|   wagmi -> albani: 100.x.x.x:8123 (HA API via Tailscale)     |
|   wagmi -> gx10: 100.98.187.12:7865/9880 (STT/TTS Tailscale) |
|   wagmi -> wakeword: http://jarvis-wakeword-casa1:8200        |
|   Zero porte aperte, NAT traversal automatico                  |
+---------------------------------------------------------------+
```

### Come funziona nel codice

L'orchestrator rileva automaticamente se un URL HA e remoto:

```
URL contiene "100." o ".ts.net"?
  Si: timeout = 15s (TAILSCALE_TIMEOUT_REMOTE)
  No: timeout = 10s (TAILSCALE_TIMEOUT_LOCAL)
```

Le location HA (URL + token) sono nel database SQLite, gestibili dalla dashboard admin.

L'orchestrator raggiunge AI Agent tramite la variabile `AI_AGENT_URL`.
Nginx sul LXC-AI-Agent termina TLS (Let's Encrypt via Cloudflare DNS) e proxya a `ws://127.0.0.1:18789`.
AI Agent richiede `wss://` per connessioni non-loopback.
Poiche l'orchestrator usa `network_mode: host`, vede l'interfaccia Tailscale direttamente senza bisogno di un container dedicato.

---

## Porte di Rete

### LXC-JARVIS

| Porta Host | Servizio | Protocollo | Accesso |
|------------|----------|------------|---------|
| 5000 | Orchestrator + Admin UI | HTTP | LAN / Tailscale |
| — | Parakeet STT (GX10 :7865) | HTTP | Via Tailscale |
| — | Qwen3-TTS (GX10 :9880) | HTTP | Via Tailscale |
| 11434 | Ollama API (LLM) | HTTP | Interno |
| 11435 | fastembed API (embeddings) | HTTP | Interno / Tailscale |
| 8200 | mem0-stack API (esterno; vedi repo `croll83/mem0-stack`) | HTTP | configurabile via MEM0_BASE_URL |
| 5432 | PostgreSQL | TCP | Interno |
| 27017 | MongoDB | TCP | Interno |
| 80 | Nginx HTTP (redirect + health) | HTTP | LAN / Tailscale |
| 443 | Nginx HTTPS (jarvis.mintwork.it) | HTTPS | Tailscale |
| 41641/udp | Tailscale NAT traversal | UDP | WAN (host-level) |

### LXC-AI-Agent

| Porta | Servizio | Protocollo | Accesso |
|-------|----------|------------|---------|
| 18789 (Tailscale IP) | Nginx TLS proxy -> AI Agent Gateway (API + WSS) | HTTPS/WSS | Tailscale (via TLS domain) |
| 443 (Tailscale IP) | Nginx TLS proxy -> AI Agent Dashboard | HTTPS | Tailscale (via TLS domain) |
| 18789 (localhost) | AI Agent Gateway (diretto) | HTTP/WS | Solo localhost (127.0.0.1) |
| 18800 | Chrome Headless (CDP) | HTTP/WS | Solo localhost (127.0.0.1) |
| 8891 (localhost) | XTTS Proxy (OpenAI→XTTS) | HTTP | Solo localhost (AI Agent TTS) |

### LXC-Wakeword (per ogni casa)

| Porta | Servizio | Protocollo | Accesso |
|-------|----------|------------|---------|
| 8200 | Wakeword Server (HTTP + WS) | HTTP/WS | LAN (AtomS3R) + Tailscale (orchestrator) |
| 41641/udp | Tailscale NAT traversal | UDP | WAN (host-level) |

---

## Monitoring

```bash
# === LXC-JARVIS ===

# Stato container
docker compose ps

# Tailscale status
tailscale status

# Logs orchestrator
docker compose logs -f orchestrator

# Health servizi interni
curl http://localhost:5000/health/services

# Risorse e GPU
docker stats
nvidia-smi

# Nginx
sudo systemctl status nginx
sudo nginx -t
curl -k https://jarvis.mintwork.it/health

# Cloudflare Tunnel
sudo systemctl status cloudflared
curl https://jarvis-pub.mintwork.it/health

# AI Agent raggiungibile? (via TLS)
curl https://your-agent-host:18789/health

# Tailscale ping test
tailscale ping jarvis-ai-agent

# === LXC-Wakeword (dal Proxmox host) ===

# Health check
curl http://<WAKEWORD_LAN_IP>:8200/health

# Devices connessi
curl http://<WAKEWORD_LAN_IP>:8200/api/devices

# Logs
pct exec <CT_ID> -- docker logs -f jarvis_wakeword

# Tailscale
pct exec <CT_ID> -- tailscale status

# === LXC-AI-Agent ===

# Stato servizi (adatta ai nomi del tuo agent)
sudo systemctl status ai-agent-chrome ai-agent

# Chrome CDP attivo?
curl -s http://127.0.0.1:18800/json/version

# Tailscale status
tailscale status

# Logs AI Agent
sudo journalctl -u ai-agent -f

# Logs Chrome headless
sudo journalctl -u ai-agent-chrome -f

# Health
curl http://localhost:18789/health
```

---

## Troubleshooting

### GPU non rilevata (LXC-JARVIS)

```bash
nvidia-smi                           # Verifica driver NVIDIA
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
docker info | grep -i nvidia         # Runtime configurato?
```

### Ollama non risponde (LXC-JARVIS)

```bash
docker logs jarvis_ollama
curl http://localhost:11434/api/tags  # Deve rispondere con lista modelli
```

### Memoria VRAM insufficiente (LXC-JARVIS)

```bash
nvidia-smi   # Verifica utilizzo VRAM

# Se OOM, opzioni:
# 1. Riduci context Qwen: OLLAMA_NUM_CTX=1024 (salva ~0.3 GB vs 2048)
# 2. Usa Whisper piu piccolo: WHISPER__MODEL=small (salva ~0.5 GB)
# 3. Riduci modelli Ollama caricati: OLLAMA_MAX_LOADED_MODELS=1
# 4. Disabilita DeepSpeed se abilitato in xtts
```

### Tailscale non si connette (LXC-JARVIS)

```bash
tailscale status
sudo systemctl status tailscaled
sudo journalctl -u tailscaled --since '5 min ago'
# Se serve ri-autenticare:
sudo tailscale up --hostname=jarvis-wagmi
```

### AI Agent non raggiungibile (dalla LXC-JARVIS)

```bash
# Verifica che AI Agent sia attivo sul suo LXC
ssh user@jarvis-ai-agent "sudo systemctl status ai-agent"

# Test connettivita Tailscale
tailscale ping jarvis-ai-agent

# Test TLS endpoint (API gateway)
curl https://your-agent-host:18789/health

# Test TLS endpoint (dashboard, porta 443)
curl https://your-agent-host/health

# Test diretto localhost (dal LXC-AI-Agent stesso)
curl http://localhost:18789/health

# Verifica nginx
ssh user@jarvis-ai-agent "sudo systemctl status nginx"
ssh user@jarvis-ai-agent "sudo nginx -t"

# Verifica certificato TLS
openssl s_client -connect your-agent-host:18789 -servername your-agent-host </dev/null 2>/dev/null | openssl x509 -noout -dates
```

### AI Agent non parte (LXC-AI-Agent)

```bash
# Adatta i nomi dei servizi al tuo AI Agent
sudo systemctl status ai-agent
sudo journalctl -u ai-agent -e
node --version
sudo systemctl restart ai-agent
```

### Chrome headless non parte (LXC-AI-Agent)

```bash
sudo systemctl status ai-agent-chrome
sudo journalctl -u ai-agent-chrome --since '5 min ago'
google-chrome --version
curl -s http://127.0.0.1:18800/json/version
sudo systemctl restart ai-agent-chrome
```

### browser-dom plugin non carica (LXC-AI-Agent)

```bash
# Verifica installazione e log del plugin browser-dom
# (adatta i path e i nomi dei servizi al tuo AI Agent)
journalctl -u ai-agent --since '5 min ago' | grep -i 'browser-dom\|plugin'
curl -s http://127.0.0.1:18800/json/list
sudo systemctl restart ai-agent-chrome && sudo systemctl restart ai-agent
```

### Wakeword server non risponde (LXC-Wakeword)

```bash
pct status <CT_ID>
pct exec <CT_ID> -- docker ps
pct exec <CT_ID> -- curl -sf http://localhost:8200/health
pct exec <CT_ID> -- docker logs --tail=50 jarvis_wakeword
```

### HA non raggiungibile

```bash
curl -H "Authorization: Bearer $TOKEN" http://<HA_IP>:8123/api/
tailscale ping 100.x.x.x    # Se via Tailscale
```

### Database corrotto (LXC-JARVIS)

```bash
cp data/jarvis_state.db data/jarvis_state.db.bak
docker compose restart orchestrator   # Il DB viene ricreato se mancante
```

### Nginx non risponde (LXC-JARVIS)

```bash
sudo systemctl status nginx
sudo nginx -t
sudo journalctl -u nginx --since '5 min ago'
# Verifica certificato SSL
openssl s_client -connect jarvis.mintwork.it:443 -servername jarvis.mintwork.it </dev/null 2>/dev/null | openssl x509 -noout -dates
```

### Cloudflare Tunnel non funziona

```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared --since '5 min ago'
# Verifica dal browser/curl
curl https://jarvis-pub.mintwork.it/health
```

---

## Aggiornamenti

### LXC-JARVIS (Docker stack)

```bash
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Nginx + Cloudflared (LXC-JARVIS)

```bash
# Nginx + Cloudflared: aggiornamenti via apt
sudo apt update && sudo apt upgrade -y nginx cloudflared
```

### LXC-AI-Agent (bare-metal)

```bash
# Sulla LXC-AI-Agent
# Aggiorna il tuo AI Agent secondo la sua documentazione
# Poi riavvia il servizio
sudo systemctl restart ai-agent
curl http://localhost:18789/health
```

Per aggiornare la skill JARVIS:

```bash
cd /opt/jarvis
git pull
# Copia le skill aggiornate nella directory del tuo agent
cp jarvis-orchestrator/skill/SKILL.md <agent-skills-dir>/jarvis-orchestrator/
cp jarvis-orchestrator/skill/skill.json <agent-skills-dir>/jarvis-orchestrator/
# Non serve riavvio — AI Agent ricarica le skill automaticamente
```

### LXC-Wakeword

```bash
pct exec <CT_ID> -- bash -c '
  cd /opt/jarvis-wakeword
  git pull --depth 1
  cd wakeword-server
  docker compose up -d --build
  sleep 5
  curl -sf http://localhost:8200/health && echo " OK" || echo " FAIL"
'
```

### Plugin browser-dom

```bash
# Sulla LXC-AI-Agent — aggiorna il plugin browser-dom
# (adatta i path alla directory del tuo agent)
cd /opt/jarvis && git pull
# Copia i file aggiornati nella directory extensions dell'agent
# Riavvia l'agent per ricaricare il plugin
```

---

## File di Riferimento

| File | Contenuto |
|------|-----------|
| [PROXMOX.md](PROXMOX.md) | Setup manuale host Proxmox (driver NVIDIA, cgroup, desktop locale) |
| [WORKSTATION.md](WORKSTATION.md) | VM Workstation Ubuntu Desktop (Terraform + Ansible) |
| [DOCKER.md](DOCKER.md) | Docker + NVIDIA Toolkit — riferimento e troubleshooting |
| [OLLAMA.md](OLLAMA.md) | Ollama — modelli AI, configurazione avanzata, troubleshooting |
| [WHISPER.md](WHISPER.md) | faster-whisper STT — configurazione e troubleshooting |
| [terraform/](terraform/) | IaC per Proxmox (LXC-JARVIS + LXC-AI-Agent + LXC-Wakeword + VM-Workstation) |
| [ansible/](ansible/) | Playbook di configurazione (LXC-JARVIS + LXC-AI-Agent + VM-Workstation + Wakeword) |
| [../docker-compose.yml](../docker-compose.yml) | Stack Docker dentro LXC-JARVIS |
| [../wakeword-server/](../wakeword-server/) | Wakeword server (openWakeWord + relay) |
| [../security/](../security/) | Stack security (Frigate + DoubleTake) |
| [../extensions/browser-dom/](../extensions/browser-dom/) | Plugin DOM automation (CDP) |
| [whisper-custom/](whisper-custom/) | Dockerfile custom Whisper (CUDA 12.9 Blackwell + CTranslate2 4.7) |
| [xtts-custom/](xtts-custom/) | Dockerfile custom XTTSv2 (PyTorch 2.7 + CUDA 12.8) |
