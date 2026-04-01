# ⚠️ DEPRECATO — Whisper (faster-whisper-large-v3-turbo)

> **DEPRECATO**: Whisper è stato sostituito da **Parakeet STT** (nvidia/parakeet-tdt-0.6b-v3)
> che gira sul **GX10 DGX Spark** (porta 7865, via Tailscale).
> Questo file è mantenuto come riferimento storico. Il Dockerfile è in `whisper-custom-deprecated/`.
>
> **Nuovo STT**: Parakeet — multilingue, auto-detection, 20x realtime, ~5.1 GiB VRAM su GX10.
> Vedi `infrastructure/gb10/gx10-full-inventory.md` per la configurazione.

---

*Contenuto originale (per riferimento):*

Server Speech-to-Text locale per JARVIS (DEPRECATO). Usava una build custom di
[speaches](https://github.com/speaches-ai/speaches) con CTranslate2 >=4.7.0
per supporto INT8 su GPU Blackwell (sm_120).

---

## Prerequisiti

| Requisito | Minimo | Consigliato |
|-----------|--------|-------------|
| GPU NVIDIA | 2 GB VRAM libera | 4 GB VRAM libera |
| Driver NVIDIA | 535+ | 550+ |
| NVIDIA Container Toolkit | Installato | Vedi [DOCKER.md](DOCKER.md) |
| Docker | 24.0+ | latest |

> **Nota:** Whisper condivide la GPU con Ollama e XTTSv2. Il modello
> `faster-whisper-large-v3-turbo` in `int8_float16` usa circa **1.3 GB** di VRAM.

### Requisiti VRAM per modello/compute type

| Modello | Compute Type | VRAM misurata | Note |
|---------|-------------|---------------|------|
| `faster-whisper-large-v3-turbo` | `int8_float16` | ~1.3 GB | **Deploy attuale** |
| `faster-whisper-large-v3-turbo` | `float16` | ~1.6 GB | Maggiore precisione, piu VRAM |
| `faster-whisper-large-v3` | `int8_float16` | ~2.0 GB | Full large-v3, molto piu lento |

---

## Immagine Custom: jarvis/whisper-blackwell

L'immagine standard `speaches` non supporta INT8 su Blackwell (sm_120) perche'
CTranslate2 < 4.7.0 disabilita INT8 su architetture sconosciute.

La build custom risolve questo:
- **Base**: `ghcr.io/speaches-ai/speaches:latest-cuda` (CUDA 12.9)
- **Upgrade**: CTranslate2 >=4.7.0 (PR #1982 — abilita INT8 su Blackwell)
- **Dockerfile**: `infrastructure/whisper-custom/Dockerfile`
- **Build**: `docker build -t jarvis/whisper-blackwell:latest infrastructure/whisper-custom/`

### Modello pre-montato (no download a runtime)

Il modello NON viene scaricato al primo avvio. E' montato come volume dal host
tramite una struttura di symlink che simula la cache HuggingFace:

```
models/whisper-large-v3-turbo-ct2/
  ├── model.bin
  ├── config.json
  ├── tokenizer.json
  ├── vocabulary.json
  └── ...
```

Il volume monta questa directory nella posizione attesa dalla fake HF cache,
cosi' `speaches` la trova senza accesso a internet.

- **Nome modello API**: `deepdml/faster-whisper-large-v3-turbo-ct2`

---

## Configurazione nel docker-compose.yml

```yaml
whisper:
  image: jarvis/whisper-blackwell:latest
  build:
    context: ./infrastructure/whisper-custom
  container_name: jarvis_whisper
  ports:
    - "9000:8000"
  volumes:
    - ./models/whisper-large-v3-turbo-ct2:/models/whisper-large-v3-turbo-ct2:ro
    # Fake HF cache: symlink structure montata nel container
  environment:
    - WHISPER__MODEL=deepdml/faster-whisper-large-v3-turbo-ct2
    - WHISPER__DEVICE=cuda
    - WHISPER__COMPUTE_TYPE=int8_float16
    - WHISPER__TTL=-1
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
| `WHISPER__MODEL` | `deepdml/faster-whisper-large-v3-turbo-ct2` | Modello CTranslate2 pre-convertito |
| `WHISPER__DEVICE` | `cuda` | Device per inferenza (cuda = GPU NVIDIA) |
| `WHISPER__COMPUTE_TYPE` | `int8_float16` | INT8 weights + float16 activations (Blackwell) |
| `WHISPER__TTL` | `-1` | Mai scaricare il modello dalla VRAM |

### Comportamento lazy loading

Il modello viene caricato in GPU **alla prima richiesta**, non all'avvio del
container. Il primo `POST /v1/audio/transcriptions` sara' lento (~5-10s), le
successive saranno immediate grazie a `TTL=-1` (il modello resta in VRAM).

---

## Verifica

```bash
# Health check
curl http://localhost:9000/health

# Test trascrizione con un file audio
curl -X POST http://localhost:9000/v1/audio/transcriptions \
  -F "file=@test_audio.wav" \
  -F "model=deepdml/faster-whisper-large-v3-turbo-ct2" \
  -F "language=it"

# Verifica GPU durante trascrizione
nvidia-smi

# Logs
docker logs jarvis_whisper --tail 30
```

---

## Configurazione

### Compute type

| Compute Type | Uso | VRAM | Precisione |
|-------------|-----|------|-----------|
| `int8_float16` | GPU Blackwell (sm_120) | ~1.3 GB | Ottima (quasi pari a float16) |
| `float16` | GPU NVIDIA (qualsiasi) | ~1.6 GB | Alta |
| `int8` | CPU o GPU con poca VRAM | Bassa | Media |

Per il deploy su Blackwell, `int8_float16` e' la scelta ottimale: minore VRAM
con precisione quasi identica a float16.

### Configurazione lingua

L'orchestrator JARVIS passa il parametro lingua nelle richieste API.
La lingua di default e' configurabile nel `.env` dell'orchestrator:

```env
WHISPER_LANGUAGE=it
```

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
WHISPER_LANGUAGE=it       # Lingua default
```

### Flusso audio in JARVIS

```
Microfono (AtomS3R) --> Wakeword Server --> Orchestrator --> Whisper (STT)
                                                |
                                                v
                                           Testo --> Routing (Qwen 2.5 3B) --> OpenClaw
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

### Prima richiesta lenta

Comportamento normale: il modello viene caricato in GPU al primo utilizzo
(lazy loading). Le richieste successive saranno immediate.

```bash
# Verifica che il modello sia stato caricato (dopo la prima richiesta)
nvidia-smi  # Deve mostrare ~1.3 GB usati dal processo whisper
```

### Modello non trovato

Il modello e' montato via volume, non scaricato da HuggingFace.
Verifica che la struttura di symlink sia corretta:

```bash
# Verifica che il volume sia montato
docker exec jarvis_whisper ls -la /models/whisper-large-v3-turbo-ct2/

# Verifica che il model.bin esista
docker exec jarvis_whisper ls -la /models/whisper-large-v3-turbo-ct2/model.bin
```

### Trascrizione imprecisa

- `faster-whisper-large-v3-turbo` e' il miglior compromesso velocita/precisione
- Verifica che la lingua sia configurata (`WHISPER_LANGUAGE=it` o param nella richiesta)
- Assicurati che l'audio sia di qualita sufficiente (sample rate 16kHz+)

### Alta latenza

- La prima richiesta e' lenta per lazy loading (normale)
- Le richieste successive devono completare in <1s su GPU
- Verifica che stia usando la GPU: `nvidia-smi` durante una richiesta
- Con `TTL=-1` il modello non viene mai scaricato dalla VRAM

### Conflitto VRAM con Ollama/XTTS

```bash
# Verifica utilizzo VRAM
nvidia-smi

# Budget VRAM totale:
# Qwen 2.5 3B:               ~2.5 GB
# XTTSv2:                    ~2.0 GB
# Whisper large-v3-turbo:    ~1.3 GB
# Totale:                    ~5.8 / 8.15 GB
```
