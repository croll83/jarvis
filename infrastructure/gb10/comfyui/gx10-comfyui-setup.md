# ComfyUI + HunyuanImage 3.0 NF4 — Setup GX10

## Stato attuale

| Componente | Versione | Percorso |
|---|---|---|
| ComfyUI | 0.17.0 | `/home/jarvis/ComfyUI/` |
| Python venv | 3.12 | `/home/jarvis/comfyui-env/` |
| PyTorch | 2.10.0+cu130 | nel venv |
| transformers | 5.3.0 | nel venv |
| bitsandbytes | 0.49.2 | nel venv |
| Custom node | Comfy_HunyuanImage3 v1.3.0 | `/home/jarvis/ComfyUI/custom_nodes/Comfy_HunyuanImage3/` |
| Modello | HunyuanImage-3-Instruct-Distil-NF4-v2 | `/home/jarvis/ComfyUI/models/HunyuanImage-3-Instruct-Distil-NF4-v2/` |

## Servizi (persistono al riavvio)

- **ComfyUI**: systemd service `comfyui.service`, enabled, porta 8188
  - `sudo systemctl {start|stop|restart|status} comfyui`
  - Si avvia automaticamente al boot
- **Open WebUI**: Docker container `open-webui`, restart=always, porta 8080 (host network)
  - Configurato con `IMAGE_GENERATION_ENGINE=comfyui`, `COMFYUI_BASE_URL=http://127.0.0.1:8188`
  - Si avvia automaticamente al boot via Docker restart policy

## Cosa succede al riavvio

**Tutto riparte automaticamente.** ComfyUI è un systemd service enabled, Open WebUI è un container Docker con `restart: always`. Al primo prompt di generazione immagine dopo il boot, il modello viene caricato in ~5 minuti (poi resta cached in VRAM).

## Patch applicate al custom node

Il file `hunyuan_instruct_nodes.py` del custom node `Comfy_HunyuanImage3` ha **2 modifiche** rispetto al commit `e1caeb7` (v1.3.0):

### Patch 1: Rimosso `quantization_config` esplicita dal caricamento NF4

**Problema**: Il codice originale non passava `quantization_config` (corretto), ma una versione intermedia l'aveva aggiunta causando OOM. Il codice originale caricava con `device_map={"": "cuda:0"}` che è equivalente ma meno diretto.

**Fix**: Caricamento con `device_map="cuda:0"` senza `quantization_config`. Transformers rileva automaticamente la quantizzazione da `config.json` del modello e usa il pipeline `Bnb4bitDeserialize` per ricostruire `Params4bit` con `quant_state` correttamente.

### Patch 2: Post-load fixup per moduli Linear4bit rotti (WORKAROUND)

**Problema**: Il `config.json` del modello ha una `llm_int8_skip_modules` list con pattern come `"shared_mlp"`, ma `transformers` matcha questi pattern solo con `re.match()` (ancorato all'inizio) o `endswith()`. Un path come `layers.0.mlp.shared_mlp.gate_and_up_proj` non matcha `"shared_mlp"` con nessuno dei due metodi. Risultato: i moduli `shared_mlp.*` vengono erroneamente convertiti a `Linear4bit` senza dati di quantizzazione → errore `AssertionError: assert module.weight.shape[1] == 1` al forward pass.

**Fix**: Dopo il caricamento, il fixup itera tutti i `Linear4bit` modules, trova quelli senza `quant_state` valido (= non erano realmente quantizzati), e li sostituisce con `nn.Linear` standard ricaricando i pesi bf16 originali dal safetensors.

## Quando la patch potrebbe rompersi

| Evento | Rischio | Azione |
|---|---|---|
| **Riavvio macchina** | Nessuno | Tutto riparte automaticamente |
| **`git pull` del custom node** | **ALTO** | Il pull sovrascrive `hunyuan_instruct_nodes.py` e perde entrambe le patch. Dopo il pull, verificare se il codice originale funziona (potrebbe essere stato fixato upstream). Se non funziona, riapplicare le patch. |
| **Update PyTorch** | Basso | Le patch non dipendono dalla versione PyTorch. |
| **Update transformers** | **MEDIO** | Se transformers fixa il matching della skip list (`should_convert_module`), la Patch 2 diventa inutile (ma non dannosa — fixup troverebbe 0 moduli da fixare). Se cambiano le API di caricamento, entrambe le patch potrebbero non funzionare. |
| **Update bitsandbytes** | Basso | Le patch non toccano bitsandbytes. |
| **Nuovo modello NF4** | **MEDIO** | La Patch 2 è generica (funziona con qualsiasi modello NF4 che ha la stessa skip list bug). Ma un modello con architettura diversa potrebbe avere problemi diversi. |
| **Update Open WebUI** | Basso | Il workflow JSON e node mappings sono salvati nel DB di Open WebUI, non nel container. Un update del container (docker pull + recreate) preserva i dati nel volume `open-webui`. |

## Come riapplicare le patch dopo un git pull

```bash
# Verifica se servono ancora (prova prima senza)
sudo systemctl restart comfyui
# Genera un'immagine di test. Se funziona, non servono patch.

# Se non funziona, applica le patch:
cd /home/jarvis/ComfyUI/custom_nodes/Comfy_HunyuanImage3
# Vedi diff salvato in questo repo:
# /home/jarvis/sviluppo/jarvis/infrastructure/comfyui-nf4-blackwell.patch
git apply /home/jarvis/sviluppo/jarvis/infrastructure/comfyui-nf4-blackwell.patch
sudo systemctl restart comfyui
```

## Performance

- **Primo caricamento modello**: ~5 minuti (298s loading + fixup)
- **Generazione 1024x1024 a 8 step**: ~52s
- **VRAM**: 47.8GB allocata su 121.6GB (42.6GB libera)
- Modello resta cached in VRAM tra le generazioni

## Architettura

```
Open WebUI (:8080) ──→ ComfyUI API (:8188) ──→ HunyuanImage 3.0 NF4
  │                       │
  │ workflow JSON          │ loads model via transformers
  │ prompt + seed          │ Params4bit + quant_state (experts)
  │                        │ nn.Linear bf16 (shared_mlp, attn, etc.)
  └─ mostra immagine       └─ SaveImage → /home/jarvis/ComfyUI/output/
```
