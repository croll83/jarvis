# Security Stack — Guida Installazione

Object detection (Frigate) + facial recognition (DoubleTake) + MQTT broker per eventi.

---

## Prerequisiti

| Requisito | Minimo | Consigliato |
|-----------|--------|-------------|
| CPU | AVX2 | i5/Ryzen 5+ |
| RAM | 4 GB dedicati | 8 GB |
| Disk | 50 GB (recording) | SSD 256 GB+ |
| Camere | RTSP compatibili | H.264/H.265 |
| (Opzionale) | Google Coral USB | Accelerazione inferenza 10x |

**Nota:** Lo stack security puo girare su un host **separato** dal resto di JARVIS, purche raggiungibile via rete.

---

## Struttura File

```
security/
  docker-compose.security.yml
  frigate/
    config.yml              # Configurazione camere RTSP
    storage/                # Recording e snapshot
  doubletake/
    .storage/               # Modelli e volti enrollati
  mqtt/
    config/                 # Configurazione Mosquitto
    data/
    log/
```

---

## Step 1 — Configura MQTT

Crea il file di configurazione Mosquitto:

```bash
mkdir -p security/mqtt/config
```

**File: `security/mqtt/config/mosquitto.conf`**

```
listener 1883
allow_anonymous true

listener 9001
protocol websockets
```

> Per produzione, aggiungi autenticazione:
> ```
> allow_anonymous false
> password_file /mosquitto/config/passwd
> ```
> E crea il file password con: `mosquitto_passwd -c /mosquitto/config/passwd jarvis`

---

## Step 2 — Configura Frigate

**File: `security/frigate/config.yml`**

```yaml
mqtt:
  host: mqtt
  port: 1883

cameras:
  # Esempio: camera salotto
  salotto:
    ffmpeg:
      inputs:
        - path: rtsp://user:password@192.168.1.50:554/stream1
          roles:
            - detect
            - record
    detect:
      width: 1280
      height: 720
      fps: 5

  # Esempio: camera ingresso
  ingresso:
    ffmpeg:
      inputs:
        - path: rtsp://user:password@192.168.1.51:554/stream1
          roles:
            - detect
            - record
    detect:
      width: 1920
      height: 1080
      fps: 5

# Object detection
detectors:
  cpu:
    type: cpu
    # Per Google Coral, commenta sopra e usa:
    # coral:
    #   type: edgetpu
    #   device: usb

# Recording
record:
  enabled: true
  retain:
    days: 7
    mode: motion
  events:
    retain:
      default: 14

# Snapshots
snapshots:
  enabled: true
  retain:
    default: 14

# Oggetti da rilevare
objects:
  track:
    - person
    - car
    - cat
    - dog
  filters:
    person:
      min_score: 0.6
      min_area: 5000
```

> **Importante:** Sostituisci gli URL RTSP con quelli delle tue camere reali.

---

## Step 3 — Avvia lo stack

```bash
cd jarvis/security

# Imposta password RTSP (opzionale)
export FRIGATE_RTSP_PASSWORD=tua_password

# Avvia
docker compose -f docker-compose.security.yml up -d
```

---

## Step 4 — Verifica servizi

```bash
# Frigate Web UI
open http://localhost:5001

# DoubleTake Web UI
open http://localhost:3000

# Verifica MQTT
docker logs jarvis_mqtt --tail 10
```

---

## Step 5 — Enrollment volti (DoubleTake)

1. Apri `http://localhost:3000`
2. Vai su **Train** nel menu
3. Carica 3-5 foto per ogni persona della famiglia
4. Assegna un nome a ciascun volto
5. Clicca **Train** per addestrare il modello

DoubleTake notifichera automaticamente Frigate quando riconosce un volto.

---

## Step 6 — Integrazione con JARVIS

L'orchestrator JARVIS riceve eventi di detection tramite il modulo `security.py`.
Configura la connessione nell'orchestrator:

```env
# Nel .env dell'orchestrator
FRIGATE_URL=http://<security-host>:5001
MQTT_HOST=<security-host>
MQTT_PORT=1883
```

L'integrazione gestisce:
- **Riconoscimento persone** — "Bentornato [nome]!" via speaker security
- **Allarme intrusi** — Notifica Telegram + annuncio vocale se persona sconosciuta
- **Animali** — Filtra gatti/cani per evitare falsi allarmi

---

## Configurazione Avanzata

### Google Coral USB (accelerazione 10x)

Se hai un Google Coral USB:

1. Decommenta in `docker-compose.security.yml`:
   ```yaml
   volumes:
     - /dev/bus/usb:/dev/bus/usb
   ```

2. Aggiorna `frigate/config.yml`:
   ```yaml
   detectors:
     coral:
       type: edgetpu
       device: usb
   ```

3. Riavvia:
   ```bash
   docker compose -f docker-compose.security.yml up -d
   ```

### Risorse di memoria

I limiti sono gia configurati nel compose:

| Servizio | Limit | Reservation |
|----------|-------|-------------|
| Frigate | 2 GB | 1 GB |
| DoubleTake | 1 GB | — |
| MQTT | — | — |

Modifica in `docker-compose.security.yml` se necessario.

### Registrazione continua vs eventi

Per risparmiare spazio, puoi registrare solo eventi:

```yaml
record:
  enabled: true
  retain:
    days: 0        # Non tenere recording continui
    mode: motion   # Solo con movimento
  events:
    retain:
      default: 30  # 30 giorni per eventi
```

---

## Porte e Servizi

| Servizio | Porta | Descrizione |
|----------|-------|-------------|
| Frigate Web UI | 5001 | Dashboard, live view, events |
| Frigate RTSP | 8554 | Restream RTSP |
| Frigate WebRTC | 8555 | Streaming WebRTC |
| DoubleTake | 3000 | Dashboard, face enrollment |
| MQTT | 1883 | Broker messaggi |
| MQTT WebSocket | 9001 | MQTT via WebSocket |

---

## Troubleshooting

### Camera non visualizzata in Frigate

```bash
# Test RTSP diretto
ffprobe rtsp://user:password@192.168.1.50:554/stream1

# Verifica logs Frigate
docker logs jarvis_frigate -f --tail 50
```

### DoubleTake non riconosce volti

- Verifica che ci siano almeno 3 foto per persona
- Le foto devono avere il volto ben visibile e illuminato
- Dopo l'enrollment, attendi 1-2 minuti per il training

### MQTT non connesso

```bash
# Test connessione
docker exec jarvis_mqtt mosquitto_sub -t '#' -v

# Verifica che Frigate punti a mqtt:1883
docker logs jarvis_frigate 2>&1 | grep mqtt
```

### Alto utilizzo CPU

Riduci il framerate di detection:
```yaml
cameras:
  salotto:
    detect:
      fps: 3  # Riduci da 5 a 3
```
