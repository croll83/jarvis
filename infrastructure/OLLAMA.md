# Ollama — Riferimento Modelli e Troubleshooting

> **NOTA:** L'installazione di Ollama e il download dei modelli sono gestiti
> automaticamente da Ansible (`jarvis.yml` + `setup.sh`). Questo file serve come
> riferimento per la configurazione avanzata e il troubleshooting.

Server LLM locale per JARVIS. Esegue Qwen3.5 4B (routing e pre-routing) e
nomic-embed-text (embeddings) su GPU NVIDIA.

---

## Prerequisiti

| Requisito | Minimo | Consigliato |
|-----------|--------|-------------|
| GPU NVIDIA | 6 GB VRAM | 8+ GB VRAM |
| Driver NVIDIA | 535+ | 550+ |
| NVIDIA Container Toolkit | Installato | Vedi [DOCKER.md](DOCKER.md) |
| Docker | 24.0+ | latest |
| RAM sistema | 16 GB | 32 GB |
| Disco | 10 GB | 15 GB (per i modelli) |

### Requisiti VRAM per modello

| Modello | Dimensione disco | VRAM misurata | Uso in JARVIS |
|---------|-----------------|---------------|---------------|
| `qwen3.5:4b` (ctx=3072) | ~3.4 GB | ~3.5 GB (weights + KV cache) | Routing (classificazione intent) e pre-routing |
| `nomic-embed-text` | ~270 MB | ~270 MB | Embedding (vector store, on demand) |

**VRAM Budget totale** (GPU dedicata, no display):
- Qwen3.5 4B (ctx=3072): ~3.5 GB
- XTTSv2 (PyTorch fp16): ~3.2 GB
- Whisper large-v3-turbo (int8_float16): ~1.0 GB
- **Totale: ~7.7 GB / 8.15 GB** (~0.45 GB buffer CUDA)

> La pipeline voce e' sequenziale (Whisper→Qwen→TTS): i modelli non fanno inference
> contemporaneamente, quindi il buffer di 0.45 GB e' sufficiente per i tensor temp.
> Con `OLLAMA_NUM_CTX=3072`, il KV cache di Qwen usa ~0.9 GB (vs ~1.3 GB a 4096).
> Il routing prompt tipico e' ~1655 token (1 location), ~2280 token (2 location).

---

## Configurazione nel docker-compose.yml

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

---

## Download modelli (riferimento)

Ansible esegue `setup.sh` automaticamente. Per download manuale o re-pull:

```bash
# Scarica Qwen3.5 4B (routing + pre-routing)
docker exec jarvis_ollama ollama pull qwen3.5:4b

# Scarica nomic-embed-text (embedding per vector store)
docker exec jarvis_ollama ollama pull nomic-embed-text

# Warmup Qwen (carica in VRAM)
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen3.5:4b",
  "prompt": "ciao",
  "options": {"num_predict": 1}
}' > /dev/null && echo "Qwen warmup OK"
```

---

## Verifica

```bash
# Lista modelli installati
docker exec jarvis_ollama ollama list

# Health check
curl http://localhost:11434/api/tags

# Test generazione con Qwen
curl http://localhost:11434/api/generate -d '{
  "model": "qwen3.5:4b",
  "prompt": "Rispondi in una parola: qual e la capitale d Italia?",
  "stream": false
}'

# Test embedding con nomic-embed-text
curl http://localhost:11434/api/embeddings -d '{
  "model": "nomic-embed-text",
  "prompt": "Test embedding per vector store"
}'

# Verifica utilizzo GPU
nvidia-smi
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
docker ps | grep ollama
docker logs jarvis_ollama --tail 50
curl http://localhost:11434/api/tags
```

### GPU non rilevata dal container

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
# Se fallisce, vedi DOCKER.md per reinstallare il toolkit
```

### Out of Memory (OOM)

Riduci il numero di modelli caricati contemporaneamente:

```yaml
environment:
  - OLLAMA_MAX_LOADED_MODELS=1
```

### Download modello fallisce

```bash
# Riprova (Ollama riprende da dove si era fermato)
docker exec jarvis_ollama ollama pull qwen3.5:4b

# Verifica spazio disco
df -h
docker system df
```

### Modello lento

1. Verifica che la GPU sia effettivamente usata: `nvidia-smi` durante una richiesta
2. Assicurati che tutti i layer siano su GPU (Qwen3.5 4B dovrebbe stare interamente in ~2.6 GB VRAM)
3. Riduci `num_ctx` se non servono context window grandi
