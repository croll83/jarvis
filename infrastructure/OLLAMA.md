# Ollama — Guida Installazione

Server LLM locale per JARVIS. Esegue il modello Qwen 2.5 (routing e pre-routing) e nomic-embed-text (embeddings) su GPU NVIDIA.

> **DEPLOY CLOUD**: Salta questo file. In modalita cloud (`AI_BACKEND=api`), Ollama non e necessario. Il routing viene gestito da OpenRouter (Qwen 2.5 7B) e lo STT da Groq API.

---

## Prerequisiti

| Requisito | Minimo | Consigliato |
|-----------|--------|-------------|
| GPU NVIDIA | 6 GB VRAM | 8+ GB VRAM |
| Driver NVIDIA | 535+ | 550+ |
| NVIDIA Container Toolkit | Installato | Vedi [DOCKER.md](DOCKER.md) Step 5 |
| Docker | 24.0+ | latest |
| RAM sistema | 16 GB | 32 GB |
| Disco | 10 GB | 15 GB (per i modelli) |

### Requisiti VRAM per modello

| Modello | Dimensione disco | VRAM richiesta | Uso in JARVIS |
|---------|-----------------|----------------|---------------|
| `qwen2.5:7b-instruct-q4_K_M` | ~4.4 GB | ~4.4 GB | Routing (classificazione intent) e pre-routing |
| `nomic-embed-text` | ~270 MB | ~270 MB | Embedding (vector store) |

**Totale VRAM stimata** con tutti i modelli caricati + Whisper: ~6 GB.

> Con `OLLAMA_MAX_LOADED_MODELS=2`, Ollama tiene in VRAM solo 2 modelli alla volta. Con l'architettura semplificata (qwen + nomic-embed-text), questo e piu che sufficiente.

---

## Step 1 — Avvio via Docker Compose

Ollama e gia configurato nel `docker-compose.yml` principale di JARVIS. Non serve installarlo separatamente.

### Configurazione nel docker-compose.yml

```yaml
ollama:
  image: ollama/ollama:latest
  container_name: jarvis_ollama
  volumes:
    - ollama_storage:/root/.ollama
    - ./models:/models
  ports:
    - "11434:11434"
  environment:
    - OLLAMA_NUM_PARALLEL=2
    - OLLAMA_MAX_LOADED_MODELS=2
    - NVIDIA_VISIBLE_DEVICES=all
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
    interval: 30s
    timeout: 10s
    retries: 3
  restart: unless-stopped
```

### Variabili ambiente

| Variabile | Valore | Descrizione |
|-----------|--------|-------------|
| `OLLAMA_NUM_PARALLEL` | `2` | Richieste parallele per modello |
| `OLLAMA_MAX_LOADED_MODELS` | `2` | Modelli in VRAM contemporaneamente |
| `NVIDIA_VISIBLE_DEVICES` | `all` | GPU visibili al container |

### Avvia solo Ollama

```bash
cd jarvis/
docker compose up -d ollama
```

Attendi che sia healthy:

```bash
# Controlla lo stato
docker compose ps ollama

# Attendi che il healthcheck passi
watch -n 2 'docker inspect --format="{{.State.Health.Status}}" jarvis_ollama'
```

---

## Step 2 — Download modelli e setup

Usa lo script `setup.sh` incluso nel repository:

```bash
cd jarvis/
bash setup.sh
```

### Cosa fa setup.sh

Lo script esegue queste operazioni in sequenza:

1. **Attende che Ollama sia pronto** — Polling su `http://localhost:11434/api/tags`
2. **Scarica Qwen 2.5 7B Instruct** — Modello per il routing e pre-routing (~4.4 GB download)
3. **Scarica nomic-embed-text** — Modello per gli embeddings del vector store (~270 MB download)
4. **Warmup del modello** — Carica Qwen in VRAM con una richiesta test

### Contenuto dello script setup.sh

```bash
#!/bin/bash
set -e

# Attendi che Ollama sia pronto
until curl -s http://localhost:11434/api/tags > /dev/null 2>&1; do
    sleep 2
done

# Download modelli
docker exec jarvis_ollama ollama pull qwen2.5:7b-instruct-q4_K_M
docker exec jarvis_ollama ollama pull nomic-embed-text

# Warmup Qwen
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "prompt": "test",
  "options": {"num_predict": 1}
}' > /dev/null
```

### Esecuzione manuale (se preferisci step-by-step)

Se preferisci eseguire i comandi manualmente:

```bash
# 1. Scarica Qwen 2.5 7B Instruct (routing + pre-routing)
docker exec jarvis_ollama ollama pull qwen2.5:7b-instruct-q4_K_M

# 2. Scarica nomic-embed-text (embedding per vector store)
docker exec jarvis_ollama ollama pull nomic-embed-text
```

---

## Step 3 — Warmup

Dopo il download, e buona pratica fare un warmup per caricare il modello in VRAM:

```bash
# Warmup Qwen (routing + pre-routing)
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "prompt": "ciao",
  "options": {"num_predict": 1}
}' > /dev/null && echo "Qwen warmup OK"
```

Il primo warmup puo richiedere 10-30 secondi (caricamento pesi in VRAM). Le richieste successive saranno immediate.

> **Nota**: nomic-embed-text non richiede warmup separato; viene caricato automaticamente alla prima richiesta di embedding.

---

## Step 4 — Verifica

### Lista modelli installati

```bash
docker exec jarvis_ollama ollama list
```

Output atteso:

```
NAME                              ID              SIZE      MODIFIED
qwen2.5:7b-instruct-q4_K_M       abc123...       4.4 GB    ...
nomic-embed-text:latest           def456...       274 MB    ...
```

### Test API

```bash
# Health check
curl http://localhost:11434/api/tags

# Test generazione con Qwen
curl http://localhost:11434/api/generate -d '{
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "prompt": "Rispondi in una parola: qual e la capitale d Italia?",
  "stream": false
}'

# Test embedding con nomic-embed-text
curl http://localhost:11434/api/embeddings -d '{
  "model": "nomic-embed-text",
  "prompt": "Test embedding per vector store"
}'
```

### Verifica utilizzo GPU

```bash
# Sull'host (fuori dal container)
nvidia-smi

# Deve mostrare il processo ollama con VRAM utilizzata
watch -n 1 nvidia-smi
```

---

## Configurazione Avanzata

### Cambiare il numero di modelli in parallelo

Se hai molta VRAM e vuoi piu modelli caricati:

```yaml
# Nel docker-compose.yml
environment:
  - OLLAMA_NUM_PARALLEL=4
  - OLLAMA_MAX_LOADED_MODELS=3
```

### Usare una GPU specifica (multi-GPU)

```yaml
environment:
  - NVIDIA_VISIBLE_DEVICES=0   # Solo la prima GPU
  # oppure
  - NVIDIA_VISIBLE_DEVICES=0,1  # Prime due GPU
```

### Persistenza dei modelli

I modelli sono salvati nel volume Docker `ollama_storage`. Questo persiste anche se il container viene ricreato:

```bash
# Verifica il volume
docker volume inspect jarvis_ollama_storage

# Backup manuale (opzionale)
docker run --rm -v ollama_storage:/data -v $(pwd):/backup alpine tar czf /backup/ollama_backup.tar.gz /data
```

---

## Troubleshooting

### Ollama non risponde

```bash
# Verifica che il container giri
docker ps | grep ollama

# Logs del container
docker logs jarvis_ollama --tail 50

# Test diretto
curl http://localhost:11434/api/tags
```

### GPU non rilevata dal container

```bash
# Verifica driver sull'host
nvidia-smi

# Verifica NVIDIA Container Toolkit
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi

# Se fallisce, reinstalla il toolkit (vedi DOCKER.md Step 5)
```

### Out of Memory (OOM)

Se Ollama va in OOM, riduci il numero di modelli caricati contemporaneamente:

```yaml
environment:
  - OLLAMA_MAX_LOADED_MODELS=1
```

> Con l'architettura semplificata (solo Qwen 2.5 7B + nomic-embed-text), gli OOM dovrebbero essere rari su GPU con almeno 6 GB VRAM.

### Download modello fallisce

```bash
# Riprova (Ollama riprende da dove si era fermato)
docker exec jarvis_ollama ollama pull qwen2.5:7b-instruct-q4_K_M

# Verifica spazio disco
df -h
docker system df
```

### Modello lento

Se il routing e troppo lento:
1. Verifica che la GPU sia effettivamente usata: `nvidia-smi` durante una richiesta
2. Assicurati che tutti i layer siano su GPU (Qwen 2.5 7B Q4 dovrebbe stare interamente in ~4.4 GB VRAM)
3. Riduci `num_ctx` se non servono context window grandi

---

## Prossimo Step

Procedi con l'installazione di Whisper: **[WHISPER.md](WHISPER.md)**
