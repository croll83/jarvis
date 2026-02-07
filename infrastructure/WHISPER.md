# Whisper (faster-whisper-server) — Guida Installazione

Server Speech-to-Text locale per JARVIS. Converte audio vocale in testo usando faster-whisper con accelerazione GPU CUDA.

> **DEPLOY CLOUD**: Salta questo file. In modalita cloud (`AI_BACKEND=api`), Whisper non e necessario. Lo STT viene gestito da Groq API (`GROQ_API_KEY`), che offre trascrizione Whisper via cloud con latenza molto bassa.

---

## Prerequisiti

| Requisito | Minimo | Consigliato |
|-----------|--------|-------------|
| GPU NVIDIA | 2 GB VRAM libera | 4 GB VRAM libera |
| Driver NVIDIA | 535+ | 550+ |
| NVIDIA Container Toolkit | Installato | Vedi [DOCKER.md](DOCKER.md) Step 5 |
| Docker | 24.0+ | latest |

> **Nota:** Whisper condivide la GPU con Ollama. Assicurati di avere VRAM sufficiente per entrambi. Il modello `base` usa circa 400 MB di VRAM.

### Requisiti VRAM per modello Whisper

| Modello | Dimensione | VRAM | Precisione | Velocita |
|---------|-----------|------|-----------|---------|
| `tiny` | 75 MB | ~200 MB | Bassa | Velocissimo |
| `base` | 142 MB | ~400 MB | Buona | Veloce |
| `small` | 466 MB | ~1 GB | Molto buona | Medio |
| `medium` | 1.5 GB | ~3 GB | Eccellente | Lento |
| `large-v3` | 3 GB | ~6 GB | Massima | Molto lento |

**Consigliato per JARVIS:** `base` — buon compromesso tra precisione e velocita, soprattutto per comandi domotici in italiano.

---

## Step 1 — Avvio via Docker Compose

Whisper e gia configurato nel `docker-compose.yml` principale di JARVIS. Non serve installarlo separatamente.

### Configurazione nel docker-compose.yml

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
| `WHISPER__COMPUTE_TYPE` | `float16` | Precisione numerica (float16 per GPU, int8 per CPU) |

### Port mapping

| Porta host | Porta container | Descrizione |
|-----------|----------------|-------------|
| `9000` | `8000` | API HTTP del server Whisper |

L'orchestrator JARVIS si connette a Whisper tramite `http://whisper:8000` (porta interna del container), mentre dall'esterno e raggiungibile su `http://localhost:9000`.

### Avvia solo Whisper

```bash
cd jarvis/
docker compose up -d whisper
```

Il primo avvio sara piu lento perche deve scaricare il modello Whisper.

---

## Step 2 — Verifica

### Health check

```bash
curl http://localhost:9000/health
```

### Test trascrizione

Puoi testare la trascrizione con un file audio:

```bash
# Registra un breve audio (richiede arecord/sox)
# oppure usa un file WAV esistente

# Test con un file audio
curl -X POST http://localhost:9000/v1/audio/transcriptions \
  -F "file=@test_audio.wav" \
  -F "language=it"
```

### Verifica GPU

Durante una trascrizione, verifica che la GPU sia utilizzata:

```bash
nvidia-smi
```

Deve mostrare un processo legato al container Whisper con VRAM allocata.

### Logs

```bash
docker logs jarvis_whisper --tail 30
```

Al primo avvio vedrai il download del modello, poi messaggi di startup del server.

---

## Configurazione

### Cambiare modello

Per usare un modello diverso, modifica la variabile ambiente nel `docker-compose.yml`:

```yaml
environment:
  - WHISPER__MODEL=small    # Piu preciso ma piu lento e usa piu VRAM
```

Poi riavvia:

```bash
docker compose up -d whisper
```

Il nuovo modello verra scaricato automaticamente al primo avvio.

### Configurazione lingua

L'orchestrator JARVIS passa il parametro lingua nelle richieste API. La lingua di default per la trascrizione e configurabile nel `.env` dell'orchestrator:

```env
WHISPER_LANGUAGE=it
```

Lingue supportate: `it`, `en`, `de`, `fr`, `es`, e molte altre (tutte le lingue supportate da Whisper).

### Compute type

| Compute Type | Uso | Precisione | Velocita |
|-------------|-----|-----------|---------|
| `float16` | GPU NVIDIA | Alta | Veloce |
| `int8` | CPU o GPU con poca VRAM | Media | Medio |
| `float32` | CPU (massima precisione) | Massima | Lento |

Per il deploy locale con GPU, `float16` e la scelta migliore.

---

## Integrazione con l'Orchestrator

L'orchestrator si connette a Whisper con queste variabili (gia configurate nel `docker-compose.yml`):

```yaml
# Nel servizio orchestrator
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
Microfono (AtomS3R) --> Home Assistant --> Orchestrator --> Whisper (STT)
                                              |
                                              v
                                         Testo --> Routing (Qwen 7B Q4) --> Reasoning (Gemini 3 Pro)
```

1. L'audio arriva all'orchestrator via Home Assistant (evento assist)
2. L'orchestrator invia l'audio a Whisper per la trascrizione
3. Il testo risultante viene passato al pipeline di routing (Qwen 7B Q4 locale) e reasoning (Gemini 3 Pro via OpenClaw)

---

## Deploy Standalone (opzionale)

Se vuoi eseguire Whisper su un host separato:

```bash
docker run -d \
  --name jarvis_whisper \
  --gpus all \
  -e WHISPER__MODEL=base \
  -e WHISPER__DEVICE=cuda \
  -e WHISPER__COMPUTE_TYPE=float16 \
  -p 9000:8000 \
  --restart unless-stopped \
  fedirz/faster-whisper-server:latest-cuda
```

Poi configura l'orchestrator per puntare all'host remoto:

```env
WHISPER_URL=http://<whisper-host>:9000
```

---

## Troubleshooting

### Whisper non risponde

```bash
# Verifica che il container giri
docker ps | grep whisper

# Logs
docker logs jarvis_whisper --tail 50

# Test diretto
curl http://localhost:9000/health
```

### GPU non rilevata

```bash
# Verifica NVIDIA Container Toolkit
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi

# Se fallisce, vedi DOCKER.md Step 5 per reinstallare il toolkit
```

### Modello non si scarica

```bash
# Verifica connettivita internet dal container
docker exec jarvis_whisper curl -s https://huggingface.co

# Verifica spazio disco
df -h
docker system df

# Riavvia per ritentare il download
docker compose restart whisper
```

### Trascrizione imprecisa

- Prova un modello piu grande (`small` o `medium`)
- Verifica che la lingua sia configurata correttamente (`WHISPER_LANGUAGE=it`)
- Assicurati che l'audio sia di qualita sufficiente (sample rate 16kHz+)
- Verifica che non ci sia troppo rumore di fondo

### Alta latenza

- Il modello `base` dovrebbe trascrivere in meno di 1 secondo su GPU
- Se e lento, verifica che stia usando la GPU: `nvidia-smi` durante una richiesta
- Se la GPU e piena (Ollama), Whisper potrebbe attendere VRAM libera
- Considera di ridurre `OLLAMA_MAX_LOADED_MODELS` a 1 per liberare VRAM

### Conflitto VRAM con Ollama

Se Ollama e Whisper competono per la VRAM:

```bash
# Verifica utilizzo VRAM
nvidia-smi

# Opzione 1: Riduci i modelli Ollama caricati
# Nel docker-compose.yml, servizio ollama:
# OLLAMA_MAX_LOADED_MODELS=1

# Opzione 2: Usa un modello Whisper piu piccolo
# WHISPER__MODEL=tiny
```

---

## Prossimo Step

L'infrastruttura AI locale e completa. Procedi con i servizi JARVIS:

1. **[Deploy Locale](README.md)** — Setup completo con GPU
2. **[Deploy Cloud](../cloud/README.md)** — Setup VPS senza GPU
3. **[Security Stack](../security/)** — Object detection e facial recognition (opzionale)
