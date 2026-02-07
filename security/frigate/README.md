# Frigate Configuration Guide

## Overview

Frigate è il sistema di object detection in tempo reale usato da JARVIS per:
- Rilevamento persone, veicoli, animali
- Generazione snapshot per DoubleTake
- Recording basato su motion/detection

## Configurazione Base

### 1. Copia il template

```bash
cp config.yml.example config.yml
```

### 2. Configura le camere

Ogni camera necessita:
- URL RTSP (username/password della camera)
- Dimensioni del frame
- Zone di detection (opzionale)

## Formato URL RTSP

| Brand | Formato tipico |
|-------|---------------|
| Hikvision | `rtsp://user:pass@ip:554/Streaming/Channels/101` |
| Dahua | `rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `rtsp://user:pass@ip:554/h264Preview_01_main` |
| Ubiquiti | `rtsp://ip:7447/camera_id` |
| Generic | `rtsp://user:pass@ip:554/stream` |

## Ottimizzazioni

### Detection Performance

```yaml
detect:
  width: 1280    # Riduci per migliori performance
  height: 720
  fps: 5         # 5 FPS sufficiente per detection
```

### Google Coral

Se hai un Coral USB:

```yaml
detectors:
  coral:
    type: edgetpu
    device: usb
```

### CPU Only

Per CPU only (più lento ma funziona):

```yaml
detectors:
  cpu:
    type: cpu
    num_threads: 4
```

## Zones

Le zone permettono detection selettivo:

```yaml
cameras:
  ingresso:
    zones:
      porta:
        coordinates: 0.1,0.1,0.3,0.1,0.3,0.9,0.1,0.9
        objects:
          - person
        filters:
          person:
            min_area: 5000
```

## Maschere

Per ignorare aree con falsi positivi (es. TV, finestre):

```yaml
cameras:
  salotto:
    motion:
      mask:
        - 0.7,0.1,0.9,0.1,0.9,0.3,0.7,0.3  # Area TV
```

## Recording

### Solo Clips (eventi)

```yaml
record:
  enabled: true
  retain:
    days: 0
  events:
    retain:
      default: 7
```

### Recording Continuo

```yaml
record:
  enabled: true
  retain:
    days: 3
    mode: motion
```

## Integrazione DoubleTake

Frigate invia automaticamente snapshot a DoubleTake quando rileva una persona.

Configura in `doubletake`:

```yaml
frigate:
  url: http://frigate:5000
  events:
    - camera: ingresso
      type: person
    - camera: cortile
      type: person
```

## Troubleshooting

### Camera non visibile

1. Verifica URL RTSP:
```bash
ffprobe -v error rtsp://user:pass@camera_ip:554/stream
```

2. Controlla firewall:
```bash
nc -vz camera_ip 554
```

### Alto uso CPU

- Riduci `detect.fps` a 3-5
- Riduci risoluzione detect
- Usa Coral se disponibile

### Detection mancate

- Aumenta `detect.fps`
- Verifica `min_score` non troppo alto
- Controlla che l'oggetto sia nelle zone configurate

## Logs

```bash
docker compose -f docker-compose.security.yml logs frigate -f
```
