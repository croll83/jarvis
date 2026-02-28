# openclaw-lab-server

Container Docker isolato per servire progetti statici da `projects/`.

## Quick Start

```bash
cd /home/jarvis/.openclaw/workspace/projects/openclaw-lab-server
docker compose up -d --build
```

## Accesso

- **Index:** http://100.100.74.71:8003/
- **Dashboard:** http://100.100.74.71:8003/dashboard/
- **Routing Dashboard:** http://100.100.74.71:8003/routing-dashboard/
- **Health:** http://100.100.74.71:8003/health

## Sicurezza

- Volume `/projects` montato **read-only**
- Nessun accesso a workspace secrets, MEMORY.md, SOUL.md
- User non-root (`app`)
- `cap_drop: ALL`, `no-new-privileges`, filesystem read-only
- `network_mode: host` per accesso diretto su Tailscale IP

## Funzionalità

- Auto-discover subfolder in `/projects/`
- SPA fallback: se il file non esiste, serve `index.html` del progetto
- Health check endpoint `/health`
- File changes riflessi immediatamente (volume mount live)

## Logs

```bash
docker logs -f openclaw-lab-server
```
