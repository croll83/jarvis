# Docker + NVIDIA Container Toolkit — Riferimento

> **NOTA:** L'installazione di Docker e NVIDIA Container Toolkit e' completamente
> automatizzata da Ansible (`common.yml` + `nvidia.yml`). Questo file serve come
> riferimento per troubleshooting e configurazione avanzata.

---

## Prerequisiti

| Requisito | Valore |
|-----------|--------|
| OS | Ubuntu 22.04 LTS o 24.04 LTS |
| Accesso | `sudo` o root |
| Rete | Accesso a internet |

---

## NVIDIA Container Toolkit (riferimento)

Il NVIDIA Container Toolkit permette ai container Docker di accedere alla GPU.
Necessario per Ollama e Whisper. Ansible `nvidia.yml` lo installa automaticamente.

### Cosa installa Ansible

```bash
# Repository NVIDIA
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

### Configurazione critica per LXC

In un container LXC, e' necessario disabilitare i cgroups nel toolkit NVIDIA:

```bash
# /etc/nvidia-container-runtime/config.toml
# Ansible imposta questa riga automaticamente:
no-cgroups = true
```

Senza `no-cgroups = true`, Docker non puo' accedere alla GPU dentro un LXC.

### Verifica GPU in Docker

```bash
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

Deve mostrare la stessa GPU vista con `nvidia-smi` sull'host.

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

L'orchestrator usa `network_mode: host` — vede direttamente le porte dell'host.
Ollama e Whisper espongono le porte sull'host, l'orchestrator li raggiunge su `localhost`.

```
Host (LXC-JARVIS)
+-- Tailscale (host-level)          — VPN mesh
+-- nginx              (:80, :443)  — Host (reverse proxy, TLS)
+-- jarvis_ollama       (:11434)    — Docker
+-- jarvis_whisper      (:9000)     — Docker (build: infrastructure/whisper-custom/)
+-- jarvis_xtts         (:8890)     — Docker (build: infrastructure/xtts/)
+-- jarvis_core         (:5000)     — Docker (network_mode: host)
+-- jarvis_ontology     (:8100)     — Docker (127.0.0.1)
+-- jarvis_postgres     (:5432)     — Docker
+-- jarvis_mongo        (:27017)    — Docker
```

L'orchestrator raggiunge i servizi su `localhost`:
- `http://localhost:11434` per Ollama
- `http://localhost:9000` per Whisper
- `https://your-agent-host:18789` per AI Agent (LXC separato, TLS, via Tailscale MagicDNS)

> **Nota**: AI Agent gira bare-metal su un **LXC separato** (non in Docker).
> Tailscale gira **host-level** (non in Docker) — l'orchestrator vede l'interfaccia Tailscale direttamente.

> **Build custom**: `jarvis_whisper` e `jarvis_xtts` usano immagini custom con Dockerfile
> in sottodirectory di `infrastructure/` (`infrastructure/whisper-custom/` e `infrastructure/xtts/`).
> I build context sono specificati nel `docker-compose.yml` alla voce `build.context`.

---

## Configurazione Risorse Docker

### Limiti memoria (consigliati)

| Container | Limite |
|-----------|--------|
| Ollama | Nessun limite (usa GPU VRAM) |
| Whisper | Nessun limite (usa GPU VRAM) |
| Orchestrator | 2 GB |
| PostgreSQL | 512 MB |
| MongoDB | 512 MB |

Questi limiti sono gia configurati nei rispettivi `docker-compose.yml`.

### Log rotation

Per evitare che i log Docker riempiano il disco:

```json
{
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
```

> Ansible `common.yml` configura automaticamente il `daemon.json` con log rotation e runtime NVIDIA.

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
