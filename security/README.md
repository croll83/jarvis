# JARVIS Security - Frigate + DoubleTake

## Overview

Stack per video analytics e riconoscimento facciale integrato con JARVIS:

- **Frigate**: Object detection in tempo reale (persone, veicoli, animali)
- **DoubleTake**: Facial recognition per identificare membri della famiglia

## Architettura

```
┌─────────────────┐     ┌─────────────────┐
│  IP Cameras     │────▶│    Frigate      │
│  (RTSP)         │     │  (Detection)    │
└─────────────────┘     └────────┬────────┘
                                 │ Events
                                 ▼
                        ┌─────────────────┐
                        │   DoubleTake    │
                        │ (Face Recog.)   │
                        └────────┬────────┘
                                 │ Identified
                                 ▼
                        ┌─────────────────┐
                        │    JARVIS       │
                        │  (Orchestrator) │
                        └─────────────────┘
```

## Requisiti Hardware

### Minimo (CPU only)
- CPU con istruzioni AVX2
- 4GB RAM dedicati
- SSD per recording (velocità scrittura importante)

### Consigliato (con accelerazione)
- Google Coral USB/M.2 per detection
- 8GB RAM
- SSD NVMe

## Setup

### 1. Preparazione

```bash
cd /opt/jarvis/security
```

### 2. Configurazione Frigate

```bash
cp frigate/config.yml.example frigate/config.yml
nano frigate/config.yml
```

Configura le tue camere RTSP. Vedi `frigate/README.md` per dettagli.

### 3. Avvio Stack

```bash
docker compose -f docker-compose.security.yml up -d
```

### 4. Accesso UI

| Servizio | URL | Porta |
|----------|-----|-------|
| Frigate | http://localhost:5001 | 5001 |
| DoubleTake | http://localhost:3000 | 3000 |

## Enrollment Volti (DoubleTake)

### Procedura

1. Accedi a DoubleTake UI: `http://localhost:3000`
2. Vai in **Train** > **Add Person**
3. Inserisci il nome (deve corrispondere esattamente al nome utente in JARVIS)
4. Carica 5-10 foto del volto
5. Clicca **Train**

### Best Practices per Training

| Aspetto | Raccomandazione |
|---------|-----------------|
| Numero foto | 5-10 minimo, 20+ ideale |
| Illuminazione | Varia (giorno, sera, luce artificiale) |
| Angolazioni | Frontale, 3/4, laterale |
| Espressioni | Neutrale, sorridente, serio |
| Accessori | Con/senza occhiali, cappello |
| Risoluzione | Minimo 200x200 pixel per volto |
| Formato | JPG o PNG |

### Test Riconoscimento

1. Dopo il training, vai in **Match** > **Test**
2. Carica una nuova foto
3. Verifica che venga identificata correttamente

## Integrazione con JARVIS

### Eventi Gestiti

JARVIS riceve eventi da Frigate via MQTT:

| Evento | Azione JARVIS |
|--------|---------------|
| Persona sconosciuta | Alert Telegram con snapshot |
| Membro famiglia riconosciuto | Log silenzioso (privacy mode) |
| Veicolo rilevato | Notifica (configurabile) |
| Movimento notturno | Alert con priorità alta |

### Configurazione in JARVIS

Gli utenti con face enrollment hanno un campo `face_enrolled` nel database.

```python
# database.py - User model
@dataclass
class User:
    id: int
    name: str
    face_enrolled: bool = False  # True se registrato in DoubleTake
    # ...
```

### Privacy Mode

Quando un membro della famiglia viene riconosciuto:
- Nessun alert inviato
- Solo log locale per audit
- Rispetta impostazioni DND individuali

## Troubleshooting

### Frigate non parte

```bash
# Check logs
docker compose -f docker-compose.security.yml logs frigate

# Verifica memoria condivisa
df -h /dev/shm
```

### Detection lento

- Verifica utilizzo CPU: `htop`
- Considera Google Coral per accelerazione
- Riduci risoluzione detect nel config

### DoubleTake non riconosce

- Aggiungi più foto di training
- Verifica qualità delle foto
- Check che il nome utente corrisponda esattamente

### Camere non raggiungibili

```bash
# Test connessione RTSP
ffprobe -v error rtsp://user:pass@camera_ip:554/stream
```

## Manutenzione

### Backup Face Models

```bash
# Backup DoubleTake storage
tar -czf doubletake_backup_$(date +%Y%m%d).tar.gz doubletake/
```

### Pulizia Recording

Frigate gestisce automaticamente la retention. Configura in `frigate/config.yml`:

```yaml
record:
  retain:
    days: 7
    mode: motion
```

### Update Stack

```bash
docker compose -f docker-compose.security.yml pull
docker compose -f docker-compose.security.yml up -d
```
