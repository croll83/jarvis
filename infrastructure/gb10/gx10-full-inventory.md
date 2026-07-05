---
name: gx10-full-inventory
description: Complete inventory of all software, services, models, workflows on GX10 DGX Spark (GB10 128GB)
type: reference
---

# GX10 DGX Spark — Inventario Completo

**Hardware**: NVIDIA DGX Spark (GB10), 128 GB VRAM, ARM64, CUDA 13.2, Driver 595.71
**Hostname**: gx10-3b82
**IP**: 192.168.1.67 (LAN), 100.98.187.12 (Tailscale)
**OS**: Ubuntu 24.04 Noble
**Ultimo aggiornamento**: 2026-07-05

---

## 1. LLM Inference — llama.cpp stock + MTP (dark-jarvis)

**Cosa**: Server LLM heavy per inferenza locale con speculative decode MTP nativo
**Modello**: Qwopus3.6-27B-v2 Abliterated + MTP NVFP4 (testo NVFP4 + MTP head BF16, ctx 262K)
**Performance**: 13-26 t/s decode (workload-dependent), 700-880 t/s prefill, MTP acceptance 45-90%
**VRAM**: 30 GiB stabile (model + KV + MTP draft + scratch + cache-ram lazy 16 GiB)

**Installazione**:
- Repo: `ggml-org/llama.cpp` upstream stock (no fork). Build snapshot 2026-05-25.
- Sorgente: `/home/jarvis/llama-cpp-stock/`
- Build: `cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="120;121" -DGGML_CUDA_FA_ALL_QUANTS=ON -DCMAKE_BUILD_TYPE=Release && cmake --build build --target llama-server llama-quantize llama-cli -j$(nproc)`
- Modello GGUF: `/home/jarvis/qwopus36-v2-mtp-abl-nvfp4/Qwopus3.6-v2-Abl-MTP-NVFP4.gguf` (19 GB)
- mmproj: `/home/jarvis/qwopus36-v2-mtp-abl-nvfp4/mmproj-Qwopus3.6-v2-Abl-MTP-F16.gguf` (885 MB)

**Avvio/Spegnimento (systemd)**:
```bash
sudo systemctl start  dark-jarvis     # avvia
sudo systemctl stop   dark-jarvis     # ferma
sudo systemctl status dark-jarvis     # stato
tail -F /var/log/dark-jarvis.log      # log
```

**Servizio esposto**: `http://100.98.187.12:30000/v1` (API OpenAI-compatible)
- Model aliases: `dark-jarvis`, `dark-opus`
- Spec decode: `--spec-type draft-mtp --spec-draft-n-max 5`
- KV: `q8_0/q8_0`, 131k context × 2 slot
- Cache RAM 16 GiB lazy prefix cache

**Stack legacy (DEPRECATO 2026-05-25)**:
- Fork `croll83/llama.cpp-dgx` (v5) + `llama-cpp-tq` (TurboQuant) + `--dflash --dflash-draft` → archiviati. Vedi [[local-llm-heavy]] §2.1 per dettagli e ragioni. Path: `/home/jarvis/llama-cpp-v5/` (binari conservati ma servizio non attivo).

---

## 2. vLLM (non attivo, disponibile)

**Cosa**: Server LLM alternativo per modelli NVFP4/FP8. Non installato come pacchetto pip — solo sorgenti e Docker image.
**Docker images disponibili**:
- `vllm-node-eugr-tf5:sm121` (18.1 GB) — image base con fix eugr per SM121 + FlashInfer

**File su disco**:
- `/home/jarvis/nvfp4-fix/` — Dockerfile per varie configurazioni (tq-dense, tq-moe, nvfp4-fixed)
  - `Dockerfile` — build nvfp4-fixed:v2 (johnny_nv fixes: FlashInfer PR#2913 + vLLM PR#38423 + CUTLASS v4.4.2)
  - `Dockerfile.tq-dense` — TurboQuant overlay su sm121 (dense)
  - `Dockerfile.tq-moe-v4` — TurboQuant overlay su nvfp4-fixed (MoE)
  - `build.log`, `build-tq-dense.log` — log di build
- `/home/jarvis/vllm-build/` (15+ GB) — sorgente vLLM completo con modifiche GB10
  - `csrc/` — codice C++/CUDA (12 subdirs)
  - `vllm/` — pacchetto Python (33 subdirs)
  - `Dockerfile.gb10` — build specifico per DGX Spark
  - `CMakeLists.txt` (49 KB, pesantemente modificato)
  - `setup.py`, `pyproject.toml` — packaging
  - `requirements/` — dipendenze
  - `benchmarks/`, `tests/` — benchmark e test
- `/home/jarvis/spark-vllm-docker/` — framework Docker distribuito per vLLM su Spark
  - `Dockerfile`, `Dockerfile.mxfp4` — build standard e MxFP4
  - `recipes/` — recipe di runtime
  - `mods/` — 13 subdirs di modifiche a vLLM
  - `wheels/` — wheel pre-compilati
  - `fastsafetensors.patch`, `flashinfer_cache.patch` — patch
  - `build-and-copy.sh`, `launch-cluster.sh` — automazione
  - `hf-download.sh` — download modelli HF

**Ray**: NON installato. C'è un proxy nginx su porta 8266→8265 (configurato ma Ray non è in esecuzione).

**Avvio** (quando serve):
```bash
docker run -d --name vllm --gpus all --network host \
  -v /home/jarvis/.cache/huggingface:/cache/huggingface \
  -e HF_HOME=/cache/huggingface \
  vllm-node-eugr-tf5:sm121 vllm serve <model> \
  --served-model-name dark-opus --host 0.0.0.0 --port 30000 \
  --attention-backend flashinfer --kv-cache-dtype fp8 --enforce-eager
```

---

## 3. CosyVoice3 TTS (Text-to-Speech) — ATTIVO

**Cosa**: Server TTS con zero-shot voice cloning e normalizzazione testo italiana
**Modello**: Fun-CosyVoice3-0.5B (Alibaba, Apache 2.0)
**VRAM**: ~3.6 GiB
**RTF**: ~0.6x (più veloce del real-time)

**Installazione**:
- Script: `/home/jarvis/cosyvoice3-tts-server.py`
- Venv: `/home/jarvis/cosyvoice3-env/`
- Modello: `/home/jarvis/cosyvoice3/pretrained_models/Fun-CosyVoice3-0.5B/`
- Reference audio: `default_it_f.wav` (nella dir modello)
- Service: `/etc/systemd/system/cosyvoice3-tts.service`
- Sorgente repo: `/home/jarvis/sviluppo/jarvis/infrastructure/scripts/cosyvoice3-tts-server.py`

**Caratteristiche**:
- Zero-shot voice cloning da audio di riferimento (no preset speakers)
- Normalizzazione testo italiana server-side (num2words: numeri, unità, orari)
- Speaker default: `it_female` (voce italiana femminile, clonata da ref audio)
- Tutti i nomi voce (sofia, marco, ecc.) mappano allo stesso speaker default

**Avvio/Spegnimento**:
```bash
sudo systemctl start cosyvoice3-tts    # avvia
sudo systemctl stop cosyvoice3-tts     # ferma
sudo systemctl status cosyvoice3-tts   # stato
journalctl -u cosyvoice3-tts -f        # log
```

**Servizio esposto**: `http://100.98.187.12:9880`
- `POST /v1/audio/speech` — API OpenAI-compatible (drop-in)
- `GET /tts/voices` — Lista voci
- `GET /health` — Health check

### 3b. Qwen3-TTS (DEPRECATO — disabilitato, file preservati)

**Modello**: Qwen3-TTS-12Hz-1.7B (CustomVoice + Base)
**Stato**: `qwen3-tts.service` disabilitato dal 2026-06-28
**Motivo**: Scarsa pronuncia numeri/unità in italiano (wetext non ha regole IT)

**File preservati** (per eventuale rollback):
- Script: `/home/jarvis/qwen3-tts-server.py`
- Venv: `/home/jarvis/qwen3-tts-env/`
- Service: `/etc/systemd/system/qwen3-tts.service` (disabled)

**Rollback**: `sudo systemctl disable --now cosyvoice3-tts && sudo systemctl enable --now qwen3-tts`

---

## 4. Parakeet STT (Speech-to-Text)

**Cosa**: Server STT multilingue (25 lingue, auto-detection)
**Modello**: nvidia/canary-1b-v2 (backend swappato da Parakeet-TDT v3 a Canary, giu-lug 2026: l'auto-LID di Parakeet sbagliava IT→RU su audio corti; unit systemd `parakeet-stt` e porta :9000 invariate)
**VRAM**: ~5.1 GiB
**RTF**: ~0.05 (20x faster than real-time)

**Installazione**:
- Script: `/home/jarvis/parakeet-gpu-server.py` (4.2 KB)
- Venv: `/home/jarvis/parakeet-gpu-env/`
- Progetto: `/home/jarvis/parakeet-stt/`
- Service: `/etc/systemd/system/parakeet-stt.service`

**Avvio/Spegnimento**:
```bash
sudo systemctl start parakeet-stt
sudo systemctl stop parakeet-stt
sudo systemctl status parakeet-stt
```

**Servizio esposto**: `http://100.98.187.12:9000` (porta hardcoded in `parakeet-gpu-server.py`, mai cambiata)
- `POST /v1/audio/transcriptions` — API OpenAI-compatible
- `GET /health` — Health check

---

## 5. ComfyUI (Image/Video Generation)

**Cosa**: Framework di generazione immagini/video con workflow visuali
**VRAM**: ~170 MiB idle, fino a ~30+ GiB durante generazione

**Installazione**:
- Dir: `/home/jarvis/ComfyUI/`
- Venv: `/home/jarvis/comfyui-env/`
- Service: `/etc/systemd/system/comfyui.service`
- Output: `/home/jarvis/ComfyUI/output/` (12K+ immagini)

**Custom Nodes installati**:
| Node | Funzione |
|------|----------|
| `ComfyUI-Manager` | Package manager per nodes |
| `ComfyUI_essentials` | Nodi essenziali |
| `ComfyUI-KJNodes` | Utility nodes avanzati |
| `ComfyUI-ReActor` | Face swap (Reactor) |
| `ComfyUI-LivePortraitKJ` | LivePortrait video synthesis |
| `comfyui-hallo4` | Hallo4 video generation |
| `ComfyUI-VideoHelperSuite` | Video processing tools |
| `comfyui-ace-step` | ACE-Step music integration |
| `comfyui-ollama` | Ollama LLM integration |
| `comfyui-parakeet-stt` | Parakeet STT integration |
| `comfyui-qwen3-tts` | Qwen3 TTS integration |
| `comfyui_webcamcapture` | Webcam capture |
| `image_gate.py` | Custom gate node |
| `websocket_image_save.py` | WebSocket save node |

**Modelli installati** (~167 GB totali in `/home/jarvis/ComfyUI/models/`):

| Categoria | Dimensione | File |
|-----------|-----------|------|
| **diffusion_models/** | 100 GB | |
| | 17 GB | `wan2.2_fun_vace_high_noise_14B_fp8_scaled.safetensors` — Wan2.2 VACE video-to-video (high noise) |
| | 17 GB | `wan2.2_fun_vace_low_noise_14B_fp8_scaled.safetensors` — Wan2.2 VACE video-to-video (low noise) |
| | 14 GB | `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors` — Wan2.2 image-to-video (high noise) |
| | 14 GB | `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors` — Wan2.2 image-to-video (low noise) |
| | 14 GB | `wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors` — Wan2.2 text-to-video (high noise) |
| | 14 GB | `wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors` — Wan2.2 text-to-video (low noise) |
| | 14 GB | `Cosmos-1_0-Diffusion-7B-Text2World.safetensors` — NVIDIA Cosmos text-to-world |
| **checkpoints/** | 31 GB | |
| | 27 GB | `Qwen-Rapid-AIO-NSFW-v23.safetensors` — **Checkpoint principale** (HunyuanImage 3 NF4, text-to-image/edit/fusion) |
| | 4.0 GB | `v1-5-pruned-emaonly.safetensors` — SD 1.5 (base per Hallo4) |
| **Hallo/** | 13 GB | Modelli dedicati per Hallo4 video generation |
| | 4.6 GB | `hallo2/net.pth` — Hallo2 network principale |
| | 3.3 GB | `stable-diffusion-v1-5/unet/diffusion_pytorch_model.safetensors` — SD1.5 UNet |
| | 863 MB | `hallo2/net_g.pth` — Hallo2 generator |
| | 361 MB | `wav2vec/wav2vec2-base-960h/model.safetensors` — Audio encoder |
| | 360 MB | `CodeFormer/codeformer.pth` — Face restoration |
| | 320 MB | `sd-vae-ft-mse/diffusion_pytorch_model.safetensors` — VAE |
| | 249 MB | `face_analysis/models/glintr100.onnx` — Face recognition |
| | + vari | face_analysis, facelib, realesrgan, audio_separator |
| **text_encoders/** | 11 GB | |
| | 6.3 GB | `umt5_xxl_fp8_e4m3fn_scaled.safetensors` — UMT5-XXL FP8 (Wan2.2 text encoder) |
| | 4.6 GB | `oldt5_xxl_fp8_e4m3fn_scaled.safetensors` — T5-XXL FP8 (Cosmos text encoder) |
| **loras/** | 4.6 GB | LoRA per LightX2V (Wan2.2 accelerazione 4-step) |
| | 1.2 GB | `wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors` |
| | 1.2 GB | `wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors` |
| | 1.2 GB | `wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors` |
| | 1.2 GB | `wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors` |
| **vae/** | 2.1 GB | |
| | 1.4 GB | `wan2.2_vae.safetensors` — Wan2.2 VAE |
| | 320 MB | `vae-ft-mse-840000-ema-pruned.safetensors` — SD VAE |
| | 243 MB | `wan_2.1_vae.safetensors` — Wan2.1 VAE (legacy) |
| | 202 MB | `cosmos_cv8x8x8_1.0.safetensors` — Cosmos VAE |
| **facerestore_models/** | 1.3 GB | Face restoration |
| | 360 MB | `codeformer-v0.1.0.pth` — CodeFormer |
| | 333 MB | `GFPGANv1.4.pth` — GFPGAN v1.4 |
| | 333 MB | `GFPGANv1.3.pth` — GFPGAN v1.3 |
| | 272 MB | `GPEN-BFR-512.onnx` — GPEN blind face restoration |
| **liveportrait/** | 1.2 GB | LivePortrait video synthesis |
| | | `spade_generator.safetensors` (212M), `warping_module.safetensors` (174M), `landmark_model.pth` (110M), `motion_extractor.safetensors` (108M), `appearance_feature_extractor.safetensors` (3.3M), `stitching_retargeting_module.safetensors` (891K) |
| | | + `animal/` subdirectory con stessi modelli per animali |
| **clip_vision/** | 1.2 GB | `clip_vision_h.safetensors` — CLIP Vision H (per Wan2.2 I2V) |
| **insightface/** | 855 MB | Face detection/recognition (per ReActor) |
| | 529 MB | `inswapper_128.onnx` — Face swap model |
| | 167 MB | `models/buffalo_l/w600k_r50.onnx` — Face recognition |
| | + vari | `1k3d68.onnx`, `det_10g.onnx`, `2d106det.onnx`, `genderage.onnx` |
| **dwpose/** | 335 MB | Pose detection |
| | 207 MB | `yolox_l.onnx` — YOLOX body detector |
| | 129 MB | `dw-ll_ucoco_384.onnx` — DWPose landmark detector |
| **nsfw_detector/** | 329 MB | `vit-base-nsfw-detector/model.safetensors` — ViT NSFW classifier |
| **facedetection/** | 186 MB | `detection_Resnet50_Final.pth` (105M), `parsing_parsenet.pth` (82M) |
| **upscale_models/** | 64 MB | `RealESRGAN_x2plus.pth` — 2x upscaler |

**Blueprint Workflows** (37 file in `/home/jarvis/ComfyUI/blueprints/`):

| Workflow | Tipo |
|----------|------|
| **Generazione** | |
| Text to Image (Z-Image-Turbo) | Testo → immagine |
| Text to Video (Wan 2.2) | Testo → video |
| Text to Audio (ACE-Step 1.5) | Testo → musica |
| Image to Video (Wan 2.2) | Immagine → video |
| **Editing** | |
| Image Edit (Qwen 2511) | Editing immagine con prompt |
| Image Edit (Flux.2 Klein 4B) | Editing immagine (Flux) |
| Image Inpainting (Qwen-image) | Inpainting |
| Image Outpainting (Qwen-Image) | Outpainting |
| Image to Layers (Qwen-Image Layered) | Separazione layer |
| Video Inpaint (Wan2.1 VACE) | Inpainting video |
| **Condizionati** | |
| Canny to Image (Z-Image-Turbo) | Edge → immagine |
| Canny to Video (LTX 2.0) | Edge → video |
| Depth to Image (Z-Image-Turbo) | Depth map → immagine |
| Depth to Video (LTX 2.0) | Depth map → video |
| Pose to Image (Z-Image-Turbo) | Pose → immagine |
| Pose to Video (LTX 2.0) | Pose → video |
| **Analisi** | |
| Image Captioning (Gemini) | Descrizione immagine |
| Video Captioning (Gemini) | Descrizione video |
| Image to Depth Map (Lotus) | Immagine → depth map |
| Image to Model (Hunyuan3d 2.1) | Immagine → modello 3D |
| Prompt Enhance | Miglioramento prompt |
| **Post-processing** | |
| Image Upscale (Z-image-Turbo) | Upscaling |
| Video Upscale (GAN x4) | Upscaling video |
| Video Stitch | Concatenazione video |
| Brightness and Contrast | Luminosità/contrasto |
| Color Adjustment | Regolazione colore |
| Hue and Saturation | Tonalità/saturazione |
| Image Levels | Livelli |
| Sharpen | Nitidezza |
| Unsharp Mask | Maschera di contrasto |
| Image Blur | Sfocatura |
| Edge-Preserving Blur | Sfocatura preserva bordi |
| Film Grain | Grana pellicola |
| Glow | Effetto glow |
| Chromatic Aberration | Aberrazione cromatica |
| Image Channels | Canali colore |

**Avvio/Spegnimento**:
```bash
sudo systemctl start comfyui
sudo systemctl stop comfyui
```

**Servizio esposto**:
- `http://100.98.187.12:8188` (interfaccia web)
- `https://comfyui.mintwork.it` (via nginx reverse proxy)

---

## 6. ComfyUI Dashboard

**Cosa**: Dashboard custom FastAPI per generazione immagini, video, audio e galleria
**Dipende da**: comfyui.service
**Framework**: FastAPI + vanilla JS frontend (dark theme)

**Installazione**:
- Dir: `/home/jarvis/comfyui-dashboard/`
- App: `/home/jarvis/comfyui-dashboard/app.py` (1039 righe)
- Frontend: `/home/jarvis/comfyui-dashboard/static/index.html` (661 righe)
- Venv: `/home/jarvis/comfyui-dashboard/venv/`
- Service: `/etc/systemd/system/comfyui-dashboard.service`

**Workflow modes** (BUILDERS dict — chiamati via `POST /api/generate` con `mode`):

| Mode | Builder | Cosa fa |
|------|---------|---------|
| **Immagini** | | |
| `t2i` | `build_t2i` | Text-to-Image (prompt → immagine via Qwen-Rapid checkpoint) |
| `edit` | `build_edit` | Image editing (prompt + 1 immagine di riferimento) |
| `fusion` | `build_fusion` | Multi-image fusion (prompt + N immagini con gate on/off) |
| `faceswap` | `build_faceswap` | Face swap (ReActor: source face → target image) |
| **Video** | | |
| `t2v` | `build_t2v` | Text-to-Video (Wan2.2 14B FP8, 2-stage hi/lo noise + LightX2V LoRA, 4 steps) |
| `i2v` | `build_i2v` | Image-to-Video (Wan2.2 14B FP8 + CLIP Vision + LightX2V LoRA) |
| `v2v` | `build_v2v` | Video-to-Video (Wan2.2 VACE 14B FP8 + LightX2V LoRA) |
| `liveportrait` | `build_liveportrait` | LivePortrait (driving video + portrait → animated portrait) |
| `hallo4` | `build_hallo4` | Hallo4 (audio + portrait → talking head video) |
| **Audio** | | |
| `tts` | `build_tts` | Text-to-Speech (proxy a CosyVoice3 server :9880) |
| `clone` | `build_clone` | Voice cloning (proxy a CosyVoice3 server :9880/tts/clone) |
| `stt` | `build_stt` | Speech-to-Text (proxy al server STT :9000, unit `parakeet-stt`) |
| `music` | `build_music` | Music generation (proxy a ACE-Step server :7865) |

**API Endpoints**:

| Metodo | Endpoint | Funzione |
|--------|----------|----------|
| `POST` | `/api/generate` | Genera immagine/video/audio (body: `{mode, prompt, ...}`) |
| `GET` | `/api/samplers` | Lista sampler e scheduler disponibili da ComfyUI |
| `POST` | `/api/upload` | Upload immagine a ComfyUI (multipart) |
| `GET` | `/api/history?limit=50` | Cronologia generazioni |
| `GET` | `/api/image/{filename}` | Scarica immagine generata |
| `GET` | `/api/media/{filename}` | Scarica media (video/audio) |
| `GET` | `/api/gallery?type_filter=all&sort=newest` | Galleria con filtri tipo e ordinamento |
| `DELETE` | `/api/gallery/{filename}` | Elimina file dalla galleria |
| `WS` | `/api/ws` | WebSocket proxy per progresso real-time |
| `GET` | `/` | Serve index.html |

**Configurazione interna**:
- Checkpoint: `Qwen-Rapid-AIO-NSFW-v23.safetensors`
- Ollama (prompt refinement): `http://127.0.0.1:11434`, model `qwen3.5-128k:latest`
- Parametri default immagini: 1024x1024, 6 steps, cfg 1.0, euler_ancestral, scheduler beta
- Parametri default video: 640x640, 81 frames, 4 steps (2 hi-noise + 2 lo-noise)

**Avvio/Spegnimento**:
```bash
sudo systemctl start comfyui-dashboard
sudo systemctl stop comfyui-dashboard
```

**Servizio esposto**:
- `http://100.98.187.12:8189`
- `https://images.mintwork.it` (via nginx reverse proxy)

---

## 7. Hallo4 (Video Generation)

**Cosa**: Talking head video generation (audio + portrait → video animato)
**Installazione**:
- Dir: `/home/jarvis/hallo4/`
- Venv: `/home/jarvis/hallo4-env/`
- Script: `/home/jarvis/hallo4/inf.sh`
- Output: `/home/jarvis/hallo4/outputs/`

**Modelli** (`/home/jarvis/hallo4/pretrained_models/`, ~17 GB):
| File | Dimensione | Funzione |
|------|-----------|----------|
| `hallo4/model_weight.ckpt` | 5.2 GB | Rete Hallo4 principale |
| `Wan2.1_Encoders/models_t5_umt5-xxl-enc-bf16.pth` | 11 GB | UMT5-XXL text encoder |
| `Wan2.1_Encoders/Wan2.1_VAE.pth` | 485 MB | Wan2.1 VAE |
| `wav2vec2-base-960h/model.safetensors` | 361 MB | Wav2Vec2 audio encoder |
| `audio_separator/Kim_Vocal_2.onnx` | 64 MB | Separazione vocale |

**Avvio**: manuale via script o tramite ComfyUI node `comfyui-hallo4`
**Non è un servizio systemd** — si usa on-demand tramite ComfyUI/Dashboard (mode `hallo4`).

---

## 8. ACE-Step 1.5 (Music Generation)

**Cosa**: Server di generazione musicale AI
**Installazione**:
- Dir: `/home/jarvis/ACE-Step-1.5/`
- Venv: `/home/jarvis/ace-step-env/`
- Service: `/etc/systemd/system/ace-step.service`
- Output: `/home/jarvis/ace-step-output/` (24 subdirs)

**Avvio/Spegnimento**:
```bash
sudo systemctl start ace-step
sudo systemctl stop ace-step
```

**Servizio esposto**: `http://100.98.187.12:7865` (lazy pipeline, idle timeout 300s — porta invariata da sempre)
- Anche accessibile via ComfyUI node `comfyui-ace-step`

---

## 9. Open WebUI

**Cosa**: Chat interface web con supporto immagini, voice, web search, code interpreter
**Runtime**: Docker container

**Installazione**:
- Container: `open-webui` (image `ghcr.io/open-webui/open-webui:main`, 4.2 GB)
- Volume: `open-webui:/app/backend/data`
- Configurazione:
  - Image generation: ComfyUI (http://127.0.0.1:8188)
  - Web search: SearXNG (http://localhost:8888)
  - LLM backends: Ollama (locale + wagmi)
  - Image steps: 8, size: 1024x1024

**Avvio/Spegnimento**:
```bash
docker start open-webui
docker stop open-webui
```

**Servizio esposto**:
- `http://100.98.187.12:8080`
- `https://chat.mintwork.it` (via nginx reverse proxy)

---

## 10. sparkrun

**Cosa**: Tool CLI per gestire workload di inferenza su DGX Spark (container Docker)
**Versione**: 0.2.8

**Installazione**: `pip install sparkrun` (in `/home/jarvis/.local/`)

**Recipe salvate**: `/home/jarvis/sparkrun-recipes/`
- `qwen35-moe-q6k.yaml` — MoE Q6_K con llama.cpp stock
- `qwen35-dense-q6k.yaml` — Dense Q6_K con llama.cpp stock
- `qwen35-moe-abliterated-fp8.yaml` — MoE FP8 con vLLM (in home dir)

**Uso**:
```bash
python3 -m sparkrun run <recipe.yaml>       # lancia workload
python3 -m sparkrun status                   # stato containers
python3 -m sparkrun stop <id>                # ferma workload
python3 -m sparkrun logs <id>                # log
python3 -m sparkrun search <keyword>         # cerca recipe
```

**Container disponibile**: `scitrera/dgx-spark-llama-cpp:b8192-cu131` (2.71 GB)

---

## 11. Tailscale VPN

**Cosa**: Mesh VPN per connettere tutti i dispositivi
**Installazione**: systemd service `tailscaled.service` (auto-start)

**Peer connessi**:
| Device | IP Tailscale | Tipo |
|--------|-------------|------|
| gx10-3b82 (THIS) | 100.98.187.12 | Linux |
| jarvis-openclaw (VPS) | 100.116.99.9 | Linux |
| jarvis-wagmi | 100.88.84.81 | Linux |
| jarvis-workstation | 100.68.235.128 | Linux |
| jarvis-wakeword | 100.108.214.36 | Linux |
| homeassistant-albani20 | 100.119.78.126 | Linux |
| pve-albani20 | 100.74.248.45 | Linux |
| pve-wagmi | 100.99.14.73 | Linux |
| macbook-pro | 100.113.149.50 | macOS |
| iphone-xr | 100.94.245.54 | iOS |
| s24-ultra-di-marco | 100.73.147.39 | Android |

---

## 12. Nginx Reverse Proxy

**Cosa**: Reverse proxy HTTPS per tutti i servizi web
**Service**: `nginx.service` (auto-start)

**Siti configurati**:
| Dominio | Backend | Porta |
|---------|---------|-------|
| `chat.mintwork.it` | Open WebUI | 8080 |
| `comfyui.mintwork.it` | ComfyUI | 8188 |
| `images.mintwork.it` | ComfyUI Dashboard | 8189 |
| `dgx.mintwork.it` | DGX Dashboard | 11000 |
| Ray Dashboard | Ray | 8265→8266 |

Certificati SSL self-signed. WebSocket support su tutti i siti.

---

## 13. DGX Dashboard

**Cosa**: Dashboard NVIDIA nativa per monitoraggio e aggiornamenti
**Service**: `dgx-dashboard.service` + `dgx-dashboard-admin.service`
**Porta**: 11000 (localhost) → `https://dgx.mintwork.it`

---

## Riepilogo Porte

| Porta | Servizio | Auto-start |
|-------|----------|------------|
| 80/443 | Nginx (proxy) | ✅ systemd |
| 7865 | ACE-Step Music | ✅ systemd |
| 8080 | Open WebUI | ✅ docker restart |
| 8188 | ComfyUI | ✅ systemd |
| 8189 | ComfyUI Dashboard | ✅ systemd |
| 9000 | Canary STT (unit `parakeet-stt`) | ✅ systemd |
| 9880 | CosyVoice3 TTS | ✅ systemd |
| 11000 | DGX Dashboard | ✅ systemd |
| 30000 | llama.cpp TQ (LLM) | ✅ docker restart |

---

## Riepilogo VRAM (~40 GiB totali)

| Servizio | VRAM |
|----------|------|
| llama.cpp TQ (MoE 35B Q6_K) | ~29.6 GiB |
| Parakeet STT | ~5.1 GiB |
| CosyVoice3 TTS | ~3.6 GiB |
| ComfyUI (idle) | ~0.2 GiB |
| Sistema (Xorg + GNOME) | ~0.1 GiB |
| **Totale** | **~38.6 GiB** |

---

## Modelli HuggingFace cached

| Modello | Tipo | Uso |
|---------|------|-----|
| mradermacher/Huihui-Qwen3.5-35B-A3B-...-GGUF | Q6_K GGUF | **Produzione** (llama.cpp) |
| mradermacher/Huihui-Qwen3.5-27B-...-GGUF | Q6_K GGUF | Test (dense) |
| huihui-ai/Huihui-Qwen3.5-35B-A3B-abliterated-NVFP4 | NVFP4 | Backup (vLLM MoE) |
| huihui-ai/Huihui-Qwen3.5-35B-A3B-abliterated | FP8 | Backup (vLLM FP8) |
| lyf/Huihui-Qwen3.5-27B-...-NVFP4 | NVFP4 | Backup (vLLM dense) |
