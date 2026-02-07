# Docker + Docker Compose — Guida Installazione

Installazione di Docker Engine e Docker Compose plugin su Ubuntu 22.04/24.04, sia per VM/LXC Proxmox che per VPS cloud.

---

## Prerequisiti

| Requisito | Valore |
|-----------|--------|
| OS | Ubuntu 22.04 LTS o 24.04 LTS |
| Accesso | `sudo` o root |
| Rete | Accesso a internet |

---

## Step 1 — Aggiorna il sistema

```bash
sudo apt update && sudo apt upgrade -y
```

---

## Step 2 — Installa Docker Engine

### Metodo rapido (script ufficiale)

```bash
curl -fsSL https://get.docker.com | sh
```

### Metodo manuale (se preferisci controllare ogni step)

```bash
# Rimuovi versioni vecchie
sudo apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null

# Installa dipendenze
sudo apt install -y \
    ca-certificates \
    curl \
    gnupg \
    lsb-release

# Aggiungi la GPG key di Docker
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Aggiungi il repository Docker
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Installa Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
```

---

## Step 3 — Installa Docker Compose Plugin

```bash
sudo apt install -y docker-compose-plugin
```

Verifica:

```bash
docker compose version
```

Output atteso: `Docker Compose version v2.x.x`

> **Nota:** JARVIS usa la sintassi `docker compose` (con spazio, plugin nativo), non `docker-compose` (versione standalone legacy).

---

## Step 4 — Post-installazione

### Aggiungi il tuo utente al gruppo docker

Per evitare di usare `sudo` per ogni comando Docker:

```bash
# Sostituisci "jarvis" con il tuo username
sudo usermod -aG docker jarvis

# Applica il cambio (oppure fai logout/login)
newgrp docker
```

### Abilita Docker all'avvio

```bash
sudo systemctl enable docker
sudo systemctl enable containerd
```

### Testa l'installazione

```bash
docker run --rm hello-world
```

Deve stampare un messaggio di successo.

### Verifica versioni

```bash
docker --version
docker compose version
```

Output atteso:

```
Docker version 27.x.x, build ...
Docker Compose version v2.x.x
```

---

## Step 5 — NVIDIA Container Toolkit (solo deploy locale con GPU)

> **DEPLOY CLOUD**: Salta questo step. Il toolkit NVIDIA non serve se usi `AI_BACKEND=api`.

Il NVIDIA Container Toolkit permette ai container Docker di accedere alla GPU. Necessario per Ollama e Whisper in modalita locale.

### Prerequisiti GPU

Verifica che i driver NVIDIA siano installati sull'host:

```bash
nvidia-smi
```

Se non funziona, installa prima i driver (vedi [PROXMOX.md](PROXMOX.md) sezione A.9).

### Installa il toolkit

```bash
# Aggiungi il repository NVIDIA
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Installa
sudo apt update
sudo apt install -y nvidia-container-toolkit

# Configura Docker per usare il runtime NVIDIA
sudo nvidia-ctk runtime configure --runtime=docker

# Riavvia Docker
sudo systemctl restart docker
```

### Verifica GPU in Docker

```bash
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

Deve mostrare la stessa GPU vista con `nvidia-smi` sull'host.

Se il comando fallisce con errore, verifica:

```bash
# Il runtime NVIDIA e configurato?
docker info | grep -i nvidia

# Il file di configurazione e stato aggiornato?
cat /etc/docker/daemon.json
```

Il file `daemon.json` deve contenere:

```json
{
    "runtimes": {
        "nvidia": {
            "args": [],
            "path": "nvidia-container-runtime"
        }
    }
}
```

---

## Panoramica Rete Docker per JARVIS

### Deploy Locale (VM-GPU)

Il `docker-compose.yml` crea automaticamente la rete `jarvis_network`. Tutti i container JARVIS comunicano su questa rete:

```
jarvis_network (bridge)
├── jarvis_ollama       (ollama:11434)
├── jarvis_whisper      (whisper:8000)
├── jarvis_core         (orchestrator:5000)
├── jarvis_tailscale    (tailscale)
├── jarvis_postgres     (postgres:5432)
└── jarvis_mongo        (mongo:27017)
```

I container si raggiungono per **nome del servizio** (non per IP):
- `http://ollama:11434` dall'orchestrator
- `http://whisper:8000` dall'orchestrator

> **Nota**: OpenClaw gira bare-metal su una **VM separata** (non in Docker).
> L'orchestrator lo raggiunge via Tailscale MagicDNS (`http://jarvis-openclaw:18789`)
> o via LAN (`http://192.168.x.x:18789`), configurabile con la variabile `OPENCLAW_URL`.

### Deploy Cloud

Il `docker-compose.cloud.yml` crea la rete `jarvis_cloud`:

```
jarvis_cloud (bridge)
├── jarvis_orchestrator  (orchestrator:5000)
└── jarvis_tailscale     (tailscale)
```

> **Nota**: OpenClaw gira bare-metal sullo **stesso host** (non in Docker).
> L'orchestrator lo raggiunge via `http://host.docker.internal:18789` (mappato con `extra_hosts` nel compose).

### Security Stack

Lo stack security usa una rete separata definita in `security/docker-compose.security.yml`:

```
security_net (bridge)
├── jarvis_frigate    (frigate:5001)
├── jarvis_doubletake (doubletake:3000)
└── jarvis_mqtt       (mqtt:1883)
```

Lo stack security comunica con l'orchestrator tramite le porte esposte sull'host.

---

## Configurazione Risorse Docker

### Limiti memoria (consigliati)

Per evitare che un container monopolizzi la RAM:

| Container | Deploy Locale | Deploy Cloud |
|-----------|--------------|-------------|
| Ollama | Nessun limite (usa GPU VRAM) | N/A |
| Whisper | Nessun limite (usa GPU VRAM) | N/A |
| Orchestrator | 2 GB | 1 GB |
| OpenClaw | 1 GB | 512 MB |
| PostgreSQL | 512 MB | 512 MB |

Questi limiti sono gia configurati nei rispettivi `docker-compose.yml`.

### Log rotation

Per evitare che i log Docker riempiano il disco:

```bash
sudo nano /etc/docker/daemon.json
```

Aggiungi (o integra se esiste gia):

```json
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
```

```bash
sudo systemctl restart docker
```

---

## Verifica Installazione

```bash
# Docker Engine
docker --version

# Docker Compose
docker compose version

# Docker funzionante
docker ps

# (Solo locale) GPU in Docker
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

---

## Troubleshooting

### "Permission denied" su docker

```bash
# Verifica che il tuo utente sia nel gruppo docker
groups
# Deve contenere "docker"

# Se non c'e:
sudo usermod -aG docker $USER
# Poi fai logout e login
```

### Docker non parte

```bash
sudo systemctl status docker
sudo journalctl -u docker -f
```

### Container non trova la GPU

```bash
# Verifica runtime NVIDIA
docker info 2>/dev/null | grep -i runtime

# Verifica driver
nvidia-smi

# Reinstalla il toolkit se necessario
sudo apt install -y --reinstall nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Spazio disco esaurito

```bash
# Vedi spazio usato da Docker
docker system df

# Pulizia (rimuove container/immagini/volumi non usati)
docker system prune -a --volumes
```

---

## Prossimo Step

- **Deploy Locale:** Procedi con l'installazione di Ollama: **[OLLAMA.md](OLLAMA.md)**
- **Deploy Cloud:** Salta Ollama e Whisper, vai direttamente al [deploy cloud](../cloud/README.md)
