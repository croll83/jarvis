# Whisper (faster-whisper) — Riferimento e Troubleshooting

> **NOTA:** L'installazione di Whisper e' gestita automaticamente da Ansible
> (`jarvis.yml` — docker compose up). Questo file serve come riferimento per
> la configurazione e il troubleshooting.

Server Speech-to-Text locale per JARVIS. Converte audio vocale in testo usando
faster-whisper con accelerazione GPU CUDA.

---

## Prerequisiti

| Requisito | Minimo | Consigliato |
|-----------|--------|-------------|
| GPU NVIDIA | 2 GB VRAM libera | 4 GB VRAM libera |
| Driver NVIDIA | 535+ | 550+ |
| NVIDIA Container Toolkit | Installato | Vedi [DOCKER.md](DOCKER.md) |
| Docker | 24.0+ | latest |

> **Nota:** Whisper condivide la GPU con Ollama. Assicurati di avere VRAM sufficiente
> per entrambi. Il modello `base` usa circa 400 MB di VRAM.

### Requisiti VRAM per modello Whisper

| Modello | Dimensione | VRAM | Precisione | Velocita |
|---------|-----------|------|-----------|---------|
| `tiny` | 75 MB | ~200 MB | Bassa | Velocissimo |
| `base` | 142 MB | ~400 MB | Buona | Veloce |
| `small` | 466 MB | ~1 GB | Molto buona | Medio |
| `medium` | 1.5 GB | ~3 GB | Eccellente | Lento |
| `large-v3` | 3 GB | ~6 GB | Massima | Molto lento |

**Consigliato per JARVIS:** `base` — buon compromesso tra precisione e velocita,
soprattutto per comandi domotici in italiano.

---

## Configurazione nel docker-compose.yml

```yaml
whisper:
  image: fedirz/faster-whisper-server:latest-cuda
  container_name: jarvis_whisper
  ports:
    - "9000:8000"
  environment:
    - WHISPER__MODEL=base
    - WHISPER__DEVICE=cuda
    - WHISPER__COMPUTE_TYPE=float16
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
  restart: unless-stopped
```

### Variabili ambiente

| Variabile | Valore | Descrizione |
|-----------|--------|-------------|
| `WHISPER__MODEL` | `base` | Modello Whisper da usare |
| `WHISPER__DEVICE` | `cuda` | Device per inferenza (cuda = GPU NVIDIA) |
| `WHISPER__COMPUTE_TYPE` | `float16` | Precisione numerica (float16 per GPU) |

---

## Verifica

```bash
# Health check
curl http://localhost:9000/health

# Test trascrizione con un file audio
curl -X POST http://localhost:9000/v1/audio/transcriptions \
  -F "file=@test_audio.wav" \
  -F "language=it"

# Verifica GPU durante trascrizione
nvidia-smi

# Logs
docker logs jarvis_whisper --tail 30
```

---

## Configurazione

### Cambiare modello

```yaml
environment:
  - WHISPER__MODEL=small    # Piu preciso ma piu lento e usa piu VRAM
```

Poi riavvia: `docker compose up -d whisper`. Il nuovo modello verra' scaricato automaticamente.

### Configurazione lingua

L'orchestrator JARVIS passa il parametro lingua nelle richieste API.
La lingua di default e' configurabile nel `.env` dell'orchestrator:

```env
WHISPER_LANGUAGE=it
```

### Compute type

| Compute Type | Uso | Precisione | Velocita |
|-------------|-----|-----------|---------|
| `float16` | GPU NVIDIA | Alta | Veloce |
| `int8` | CPU o GPU con poca VRAM | Media | Medio |
| `float32` | CPU (massima precisione) | Massima | Lento |

Per il deploy locale con GPU, `float16` e' la scelta migliore.

---

## Integrazione con l'Orchestrator

```yaml
# Nel servizio orchestrator (docker-compose.yml)
environment:
  - WHISPER_URL=http://whisper:8000
```

Variabili di timeout configurabili nel `.env`:

```env
TIMEOUT_WHISPER=30        # Timeout trascrizione (secondi)
WHISPER_MODEL=base        # Modello usato (per logging)
WHISPER_LANGUAGE=it       # Lingua default
```

### Flusso audio in JARVIS

```
Microfono (AtomS3R) --> Wakeword Server --> Orchestrator --> Whisper (STT)
                                                |
                                                v
                                           Testo --> Routing (Qwen 7B Q4) --> Reasoning (Gemini 3 Pro)
```

---

## Troubleshooting

### Whisper non risponde

```bash
docker ps | grep whisper
docker logs jarvis_whisper --tail 50
curl http://localhost:9000/health
```

### GPU non rilevata

```bash
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
# Se fallisce, vedi DOCKER.md per reinstallare il toolkit
```

### Modello non si scarica

```bash
docker exec jarvis_whisper curl -s https://huggingface.co
df -h
docker compose restart whisper
```

### Trascrizione imprecisa

- Prova un modello piu grande (`small` o `medium`)
- Verifica che la lingua sia configurata correttamente (`WHISPER_LANGUAGE=it`)
- Assicurati che l'audio sia di qualita sufficiente (sample rate 16kHz+)

### Alta latenza

- Il modello `base` dovrebbe trascrivere in meno di 1 secondo su GPU
- Verifica che stia usando la GPU: `nvidia-smi` durante una richiesta
- Se la GPU e' piena (Ollama), Whisper potrebbe attendere VRAM libera
- Considera di ridurre `OLLAMA_MAX_LOADED_MODELS` a 1 per liberare VRAM

### Conflitto VRAM con Ollama

```bash
# Verifica utilizzo VRAM
nvidia-smi

# Opzione 1: Riduci i modelli Ollama caricati
# Nel docker-compose.yml, servizio ollama:
# OLLAMA_MAX_LOADED_MODELS=1

# Opzione 2: Usa un modello Whisper piu piccolo
# WHISPER__MODEL=tiny
```
