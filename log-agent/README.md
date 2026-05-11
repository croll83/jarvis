# JARVIS Log Collector System

Architettura push-based: un **log-agent** leggero (Python, zero dipendenze extra oltre PyYAML) gira su ogni host e inoltra i log al **collector** nell'orchestrator, che li salva su SQLite e li espone nella dashboard admin.

Per HAOS (Home Assistant OS) i log vengono **pullati** dall'orchestrator via Supervisor API (non serve agent sulla VM).

## Architettura

```
┌─────────────────────────────────────────────────────────────────┐
│ LXC-JARVIS (100.88.84.81)                                       │
│  ┌─────────────┐  ┌─────────────────────────┐                   │
│  │ log-agent    │→→│ orchestrator (jarvis_core)│                  │
│  │ (system svc) │  │  ├─ /api/admin/logs/ingest │← da tutti gli │
│  └─────────────┘  │  ├─ /api/admin/logs/query  │   agent remoti │
│                    │  ├─ HAOS puller (async)     │                │
│                    │  ├─ Telegram alerter        │                │
│                    │  └─ SQLite (data/logs.db)   │                │
│                    └─────────────────────────────┘                │
├─────────────────────────────────────────────────────────────────┤
│ Ogni altro host:  log-agent → POST /api/admin/logs/ingest       │
└─────────────────────────────────────────────────────────────────┘
```

## File principali

```
jarvis/
├─ jarvis-orchestrator/
│  └─ log_collector.py        # Backend: ingest API, HAOS puller, query API, prune, alerting
├─ log-agent/
│  ├─ agent.py                # Agent standalone: tails + forwards
│  ├─ configs/                # YAML config per host
│  │  ├─ lxc-jarvis.yaml
│  │  ├─ lxc-ai-agent.yaml
│  │  ├─ vm-workstation.yaml
│  │  ├─ gx10.yaml
│  │  ├─ lxc-wakeword-albani.yaml
│  │  ├─ pve-albani.yaml
│  │  └─ pve-wagmi.yaml
│  ├─ jarvis-log-agent.service       # systemd unit (system-level)
│  └─ jarvis-log-agent-user.service  # systemd unit (user-level, per journalctl --user)
└─ README.md  ← questo file
```

## Requisiti agent

- Python 3.10+
- `pyyaml` (pacchetto: `python3-yaml` su Debian/Ubuntu, o `pip install pyyaml`)
- Accesso in lettura ai journal/docker logs dei servizi configurati
- Connettività HTTP verso il collector (LXC-JARVIS:5000)

## Installazione agent su un nuovo host

### 1. Copia i file

```bash
HOST=user@<tailscale-ip>
scp log-agent/agent.py $HOST:~/log-agent/agent.py
scp log-agent/configs/<host>.yaml $HOST:~/log-agent/config.yaml
```

### 2. Installa PyYAML (se mancante)

```bash
ssh $HOST 'python3 -c "import yaml" 2>/dev/null || sudo apt-get install -y python3-yaml'
```

### 3. Crea il config YAML

```yaml
host_id: nome-host          # Identificativo unico nel collector
collector_url: http://100.88.84.81:5000/api/admin/logs/ingest
# auth_token: <dal env var LOG_AGENT_TOKEN>

batch_interval: 2.0         # Secondi tra un batch e l'altro
batch_size: 100              # Max entries per batch HTTP

sources:
  - name: mio-servizio       # Nome mostrato in dashboard
    type: journalctl          # journalctl | journalctl-user | docker | file
    target: nome-unit         # Unit systemd / container name / file path
    level_filter: WARN        # Opzionale: filtra sotto questo livello (DEBUG < INFO < WARN < ERROR < FATAL)
    extra_args: []            # Opzionale: argomenti extra per il comando
```

### 4. Scegli il tipo di systemd service

**System service** — per host dove i servizi girano come system units o docker:

```bash
sudo tee /etc/systemd/system/jarvis-log-agent.service > /dev/null << 'EOF'
[Unit]
Description=JARVIS Log Agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=<user>
Group=docker
WorkingDirectory=/home/<user>/log-agent
ExecStart=/usr/bin/python3 /home/<user>/log-agent/agent.py /home/<user>/log-agent/config.yaml
Environment=LOG_AGENT_TOKEN=<token>
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now jarvis-log-agent
```

**User service** — per host con servizi user systemd (es. hermes gateways su LXC-AI-Agent):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/jarvis-log-agent.service << 'EOF'
[Unit]
Description=JARVIS Log Agent
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/log-agent/agent.py %h/log-agent/config.yaml
Environment=LOG_AGENT_TOKEN=<token>
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now jarvis-log-agent
```

### 5. Verifica

```bash
# System service
systemctl status jarvis-log-agent
journalctl -u jarvis-log-agent -n10 --no-pager

# User service
systemctl --user status jarvis-log-agent
journalctl --user-unit jarvis-log-agent -n10 --no-pager
```

## Tipi di source

| Tipo | Comando generato | Uso |
|------|-----------------|-----|
| `journalctl` | `journalctl --follow -u <target>` | Servizi system systemd |
| `journalctl-user` | `journalctl --follow --user --user-unit <target>` | Servizi user systemd |
| `docker` | `docker logs --follow --timestamps <target>` | Container Docker |
| `file` | `tail -F <target>` | File di log tradizionali |

## Aggiungere un servizio a un host esistente

1. Modifica il file `configs/<host>.yaml` nel monorepo
2. Aggiungi la nuova source:
   ```yaml
   - name: nuovo-servizio
     type: journalctl
     target: nome-unit-systemd
   ```
3. Copia il config aggiornato sull'host:
   ```bash
   scp configs/<host>.yaml $HOST:~/log-agent/config.yaml
   ```
4. Riavvia l'agent:
   ```bash
   ssh $HOST 'sudo systemctl restart jarvis-log-agent'
   # oppure per user service:
   ssh $HOST 'systemctl --user restart jarvis-log-agent'
   ```

## Aggiungere un nuovo host

1. Crea `configs/<nuovo-host>.yaml` con le source desiderate
2. Segui i passi 1-5 della sezione "Installazione agent"

## Configurazione HAOS (Home Assistant)

HAOS non richiede agent. L'orchestrator pulla i log via Supervisor API.

Configurazione nell'`.env` dell'orchestrator:

```bash
HAOS_INSTANCES={"albani":{"url":"https://albani20.mintwork.it","token":"<long-lived-access-token>"}}
```

Per aggiungere un'altra istanza HAOS:

```bash
HAOS_INSTANCES={"albani":{"url":"https://albani20.mintwork.it","token":"<token1>"},"wagmi":{"url":"https://wagmi.mintwork.it","token":"<token2>"}}
```

Il token è un **Long-Lived Access Token** creato da HA → Profilo → Token di accesso a lunga durata.

## Variabili d'ambiente orchestrator

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `LOG_AGENT_TOKEN` | — | Token condiviso per autenticare gli agent |
| `LOG_RETENTION_HOURS` | `72` | Ore di retention prima del pruning |
| `HAOS_INSTANCES` | `{}` | JSON con istanze HAOS da pullare |

## Troubleshooting

### Agent non invia log
```bash
# Controlla stato
systemctl status jarvis-log-agent
# Controlla log dell'agent
journalctl -u jarvis-log-agent -n20 --no-pager
# Errori comuni:
#   "Connection refused" → orchestrator non raggiungibile
#   "process exited, restarting" → il servizio target è crashato
#   "401 Unauthorized" → LOG_AGENT_TOKEN non corrisponde
```

### Servizio presente ma 0 entries
1. **`level_filter: WARN`** — il servizio non ha generato WARN/ERROR. Rimuovi il filtro per catturare tutto.
2. **Tipo sbagliato** — nginx logga su file, non su journalctl. Usa `type: file` con `target: /var/log/nginx/error.log`.
3. **Servizio inattivo** — controlla `systemctl is-active <service>`.

### DB troppo grande
- Riduci `LOG_RETENTION_HOURS`
- Aggiungi `level_filter: WARN` ai servizi verbosi (es. ha-core produce 750K INFO/giorno)
- Il pruning gira ogni 5 minuti automaticamente

## Host attualmente monitorati

| Host | IP Tailscale | Service type | Agent |
|------|-------------|--------------|-------|
| LXC-JARVIS | 100.88.84.81 | system (docker+journal) | `jarvis-log-agent.service` |
| LXC-AI-Agent | 100.116.99.9 | user (hermes user units) | `~/.config/systemd/user/jarvis-log-agent.service` |
| VM-Workstation | 100.68.235.128 | user (companion) | `~/.config/systemd/user/jarvis-log-agent.service` |
| GX10 | 100.98.187.12 | system (GPU services) | `jarvis-log-agent.service` |
| LXC-Wakeword | 100.108.214.36 | system (docker) | `jarvis-log-agent.service` |
| PVE-Albani | 100.74.248.45 | system (pve daemons) | `jarvis-log-agent.service` |
| PVE-WAGMI | 100.99.14.73 | system (pve+nvidia) | `jarvis-log-agent.service` |
| HAOS-Albani | 100.119.78.126 | HAOS API pull | (nessun agent, pull da orchestrator) |
