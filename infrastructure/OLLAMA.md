# Ollama — Router LLM lightweight (Qwen 2.5 3B)

> **AMBITO:** Questo file documenta **solo** il router LLM lightweight su atomman
> (Qwen 2.5 3B, intent classification). Per l'**heavy LLM** Qwopus3.6-27B su GB10
> vedi [`gb10/dark-jarvis.md`](gb10/dark-jarvis.md).
>
> **NOTA:** L'installazione di Ollama e il download dei modelli sono gestiti
> automaticamente da Ansible (`jarvis.yml` + `setup.sh`). Questo file serve come
> riferimento per la configurazione avanzata e il troubleshooting.

Server LLM locale per JARVIS — **routing + pre-routing soltanto**. Esegue
**Qwen 2.5 3B** su GPU NVIDIA. Per reasoning agentic profondo l'AI Agent delega
al heavy LLM su GB10 (Qwopus3.6-27B-Abl-MTP-NVFP4, vedi `gb10/dark-jarvis.md`).

Gli embeddings sono gestiti da **fastembed** (container separato, CPU-only,
porta 11435) — vedi `infrastructure/fastembed/` e `docker-compose.yml`.

> **Modello attuale:** `qwen2.5:3b` — `qwen3.5:4b` e' disponibile come upgrade
> futuro se il budget VRAM lo consente.

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
| `qwen2.5:3b` (ctx=3072) | ~2.0 GB | ~2.5 GB (weights + KV cache) | **Deploy attuale** — Routing + pre-routing |
| `qwen3.5:4b` (ctx=3072) | ~3.4 GB | ~3.5 GB (weights + KV cache) | Upgrade futuro (migliore reasoning) |

**VRAM Budget totale** (GPU dedicata, no display):
- Qwen 2.5 3B (ctx=3072): ~2.5 GB
- XTTSv2 (PyTorch fp16): ~2.0 GB
- Whisper large-v3-turbo (int8_float16): ~1.3 GB
- **Totale: ~5.8 GB / 8.15 GB** (~2.35 GB buffer CUDA)

> La pipeline voce e' sequenziale (Whisper->Qwen->TTS): i modelli non fanno inference
> contemporaneamente, quindi il buffer e' ampio per tensor temp.
> **Nota:** nomic-embed-text e' stato spostato su fastembed (CPU, ONNX, porta 11435)
> per eliminare il context switch CUDA che rallentava Qwen di ~1.3s per ogni embedding call.
> **Nota:** Qwen 2.5 NON attiva Flash Attention — il KV cache cresce dinamicamente con
> la lunghezza del contesto. Con `OLLAMA_NUM_CTX=3072`, il KV cache usa ~0.6 GB.
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
    - OLLAMA_MAX_LOADED_MODELS=1      # Solo Qwen — embeddings su fastembed :11435
    - OLLAMA_CONTEXT_LENGTH=32768
    - OLLAMA_KEEP_ALIVE=-1
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
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Solo Qwen — embeddings su fastembed CPU |
| `OLLAMA_CONTEXT_LENGTH` | `32768` | Context window massima (default Ollama) |
| `OLLAMA_KEEP_ALIVE` | `-1` | Mai scaricare il modello dalla VRAM |
| `NVIDIA_VISIBLE_DEVICES` | `all` | GPU visibili al container |

---

## Download modelli (riferimento)

Ansible esegue `setup.sh` automaticamente. Per download manuale o re-pull:

```bash
# Scarica Qwen 2.5 3B (deploy attuale — routing + pre-routing)
docker exec jarvis_ollama ollama pull qwen2.5:3b

# Scarica Qwen3.5 4B (upgrade futuro, opzionale)
# docker exec jarvis_ollama ollama pull qwen3.5:4b

# NOTA: nomic-embed-text rimosso da Ollama — gestito da fastembed (CPU, :11435)
# Per test embedding: curl http://localhost:11435/api/embeddings -d '{"model":"nomic-embed-text","prompt":"test"}'

# Warmup Qwen (carica in VRAM)
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen2.5:3b",
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
  "model": "qwen2.5:3b",
  "prompt": "Rispondi in una parola: qual e la capitale d Italia?",
  "stream": false
}'

# Test embedding (via fastembed, porta 11435)
curl http://localhost:11435/api/embeddings -d '{
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
docker exec jarvis_ollama ollama pull qwen2.5:3b

# Verifica spazio disco
df -h
docker system df
```

### Modello lento

1. Verifica che la GPU sia effettivamente usata: `nvidia-smi` durante una richiesta
2. Assicurati che tutti i layer siano su GPU (Qwen 2.5 3B dovrebbe stare interamente in ~2.5 GB VRAM)
3. Riduci `num_ctx` se non servono context window grandi
4. Qwen 2.5 non attiva Flash Attention — il KV cache cresce con il contesto
