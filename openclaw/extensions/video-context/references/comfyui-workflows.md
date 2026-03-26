# ComfyUI — Comandi esecutivi

COMFY=http://100.98.187.12:8188

## Step operativi per ogni generazione

### 1. Upload immagine (se serve)
```bash
curl -s -X POST $COMFY/upload/image -F "image=@/path/to/file.jpg" -F "overwrite=true"
```
Restituisce: `{"name":"file.jpg","subfolder":"","type":"input"}`
Usa il campo `name` nel workflow come valore di `"image"`.

### 2. Inviare workflow
```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{"prompt": {WORKFLOW_QUI}}'
```
Restituisce: `{"prompt_id":"UUID"}`

### 3. Polling risultato (ripeti ogni 5 secondi)
```bash
curl -s $COMFY/history/PROMPT_ID | python3 -c "
import json,sys
d=json.load(sys.stdin)
if not d: print('PENDING'); sys.exit(0)
v=list(d.values())[0]
if not v.get('status',{}).get('completed'): print('RUNNING'); sys.exit(0)
for nid,out in v.get('outputs',{}).items():
  for img in out.get('images',[])+out.get('gifs',[]):
    print(f'DONE: {img[\"filename\"]}')
"
```

### 4. Scaricare output
```bash
curl -s "$COMFY/view?filename=FILENAME&type=output" -o /path/to/save/file
```

### 5. Interrompere (se serve)
```bash
curl -s -X POST $COMFY/interrupt
```

---

### 1. Text-to-Image

Sostituisci PROMPT, SEED, WIDTH, HEIGHT. Copia ed esegui così com'è.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "Qwen-Rapid-AIO-NSFW-v23.safetensors"}},
    "2": {"class_type": "EmptyLatentImage", "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
    "3": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 1], "prompt": "PROMPT", "vae": ["1", 2], "target_latent": ["2", 0]}},
    "4": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 1], "prompt": "", "vae": ["1", 2], "target_latent": ["2", 0]}},
    "5": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0], "latent_image": ["2", 0], "seed": SEED, "steps": 6, "cfg": 1.0, "sampler_name": "euler_ancestral", "scheduler": "beta", "denoise": 1.0}},
    "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
    "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "T2I"}}
  }
}'
```

### 2. Image Edit (single reference)

Prima fai upload dell'immagine (step 1 sopra). Sostituisci PROMPT, SEED, WIDTH, HEIGHT, UPLOADED_NAME.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "Qwen-Rapid-AIO-NSFW-v23.safetensors"}},
    "2": {"class_type": "EmptyLatentImage", "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
    "3": {"class_type": "LoadImage", "inputs": {"image": "UPLOADED_NAME", "upload": "image"}},
    "4": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 1], "prompt": "PROMPT", "vae": ["1", 2], "target_latent": ["2", 0], "image1": ["3", 0]}},
    "5": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["1", 1], "prompt": "", "vae": ["1", 2], "target_latent": ["2", 0]}},
    "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["2", 0], "seed": SEED, "steps": 6, "cfg": 1.0, "sampler_name": "euler_ancestral", "scheduler": "beta", "denoise": 1.0}},
    "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
    "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "Edit"}}
  }
}'
```

### 3. Multi-Fusion (2-4 reference images)

Come Edit ma con più immagini. Immagini 2-4 passano attraverso ImageGate. Aggiungi nodi per ogni immagine extra:

```json
"imgN": {"class_type": "LoadImage", "inputs": {"image": "UPLOADED_N", "upload": "image"}},
"gateN": {"class_type": "ImageGate", "inputs": {"enabled": true, "image": ["imgN", 0]}}
```
E aggiungi `"imageN": ["gateN", 0]` agli inputs del nodo positive (TextEncodeQwenImageEditPlus). Image1 va diretta senza gate.

### 4. Text-to-Video (WAN 2.2)

Genera ~5 secondi di video (81 frame a 16fps). Sostituisci PROMPT e SEED.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "3": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors", "strength_model": 1.0}},
    "4": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["2", 0], "lora_name": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors", "strength_model": 1.0}},
    "5": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["3", 0], "shift": 5.0}},
    "6": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["4", 0], "shift": 5.0}},
    "7": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
    "8": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
    "9": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["7", 0], "text": "PROMPT"}},
    "10": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["7", 0], "text": ""}},
    "11": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": 640, "height": 640, "length": 81, "batch_size": 1}},
    "12": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["5", 0], "positive": ["9", 0], "negative": ["10", 0], "latent_image": ["11", 0], "add_noise": "enable", "noise_seed": SEED, "control_after_generate": "randomize", "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 0, "end_at_step": 2, "return_with_leftover_noise": "enable"}},
    "13": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["6", 0], "positive": ["9", 0], "negative": ["10", 0], "latent_image": ["12", 0], "add_noise": "disable", "noise_seed": SEED, "control_after_generate": "fixed", "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 2, "end_at_step": 4, "return_with_leftover_noise": "disable"}},
    "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["8", 0]}},
    "15": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["14", 0], "filename_prefix": "T2V", "fps": 16.0, "lossless": false, "quality": 85, "method": "default"}}
  }
}'
```

### 5. Image-to-Video (WAN 2.2)

Come T2V ma con immagine di partenza. Prima fai upload. Sostituisci PROMPT, SEED, UPLOADED_NAME.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "3": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors", "strength_model": 1.0}},
    "4": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["2", 0], "lora_name": "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors", "strength_model": 1.0}},
    "5": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["3", 0], "shift": 5.0}},
    "6": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["4", 0], "shift": 5.0}},
    "7": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
    "8": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
    "9": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["7", 0], "text": "PROMPT"}},
    "10": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["7", 0], "text": ""}},
    "11": {"class_type": "LoadImage", "inputs": {"image": "UPLOADED_NAME", "upload": "image"}},
    "12": {"class_type": "WanImageToVideo", "inputs": {"positive": ["9", 0], "negative": ["10", 0], "vae": ["8", 0], "start_image": ["11", 0], "width": 640, "height": 640, "length": 81, "batch_size": 1}},
    "13": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["5", 0], "positive": ["12", 0], "negative": ["12", 1], "latent_image": ["12", 2], "add_noise": "enable", "noise_seed": SEED, "control_after_generate": "randomize", "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 0, "end_at_step": 2, "return_with_leftover_noise": "enable"}},
    "14": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["6", 0], "positive": ["12", 0], "negative": ["12", 1], "latent_image": ["13", 0], "add_noise": "disable", "noise_seed": SEED, "control_after_generate": "fixed", "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 2, "end_at_step": 4, "return_with_leftover_noise": "disable"}},
    "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["8", 0]}},
    "16": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["15", 0], "filename_prefix": "I2V", "fps": 16.0, "lossless": false, "quality": 85, "method": "default"}}
  }
}'
```

### 6. Video-to-Video (WAN 2.2 Fun VACE — Dual Stage)

Rigenera un video sorgente con un prompt. Upload il video prima (step 1). Sostituisci PROMPT, SEED, UPLOADED_VIDEO.
VACE usa dual-stage sampling (high_noise + low_noise) e NON ha LoRA di accelerazione → servono 20 step totali.
`ref_image` è opzionale — usa un'immagine di riferimento per guidare lo stile.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_fun_vace_high_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_fun_vace_low_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "3": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
    "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["2", 0], "shift": 8.0}},
    "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
    "6": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["5", 0], "text": "PROMPT"}},
    "8": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["5", 0], "text": ""}},
    "9": {"class_type": "LoadVideo", "inputs": {"file": "UPLOADED_VIDEO"}},
    "10": {"class_type": "GetVideoComponents", "inputs": {"video": ["9", 0]}},
    "11": {"class_type": "Wan22FunControlToVideo", "inputs": {"positive": ["7", 0], "negative": ["8", 0], "vae": ["6", 0], "control_video": ["10", 0], "width": 640, "height": 640, "length": 81, "batch_size": 1}},
    "12": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["3", 0], "positive": ["11", 0], "negative": ["11", 1], "latent_image": ["11", 2], "add_noise": "enable", "noise_seed": SEED, "control_after_generate": "randomize", "steps": 20, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 0, "end_at_step": 10, "return_with_leftover_noise": "enable"}},
    "13": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["4", 0], "positive": ["11", 0], "negative": ["11", 1], "latent_image": ["12", 0], "add_noise": "disable", "noise_seed": SEED, "control_after_generate": "fixed", "steps": 20, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 10, "end_at_step": 10000, "return_with_leftover_noise": "disable"}},
    "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["6", 0]}},
    "15": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["14", 0], "filename_prefix": "V2V", "fps": 16.0, "lossless": false, "quality": 85, "method": "default"}}
  }
}'
```

Per aggiungere ref_image opzionale, aggiungi:
```json
"16": {"class_type": "LoadImage", "inputs": {"image": "REF_IMAGE", "upload": "image"}}
```
E aggiungi `"ref_image": ["16", 0]` agli inputs del nodo "11" (Wan22FunControlToVideo).

### 7. Face Swap (ReActor)

Upload sia la faccia sorgente che l'immagine target. Sostituisci FACE_SOURCE e TARGET.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "LoadImage", "inputs": {"image": "FACE_SOURCE", "upload": "image"}},
    "2": {"class_type": "LoadImage", "inputs": {"image": "TARGET", "upload": "image"}},
    "3": {"class_type": "ReActorFaceSwap", "inputs": {"input_image": ["2", 0], "source_image": ["1", 0], "swap_model": "inswapper_128.onnx", "facedetection": "retinaface_resnet50", "face_restore_model": "codeformer-v0.1.0.pth", "face_restore_visibility": 1.0, "codeformer_weight": 0.5, "detect_gender_input": "no", "detect_gender_source": "no", "input_faces_index": "0", "source_faces_index": "0", "console_log_level": 1}},
    "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "FaceSwap"}}
  }
}'
```

### 8. LivePortrait (lipsync)

Upload faccia sorgente e immagine/video driving. Sostituisci FACE e DRIVING.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "DownloadAndLoadLivePortraitModels", "inputs": {"precision": "fp16", "mode": "human"}},
    "2": {"class_type": "LoadImage", "inputs": {"image": "FACE", "upload": "image"}},
    "3": {"class_type": "LoadImage", "inputs": {"image": "DRIVING", "upload": "image"}},
    "4": {"class_type": "LivePortraitCropper", "inputs": {"liveportrait_model": ["1", 0], "source_image": ["2", 0], "driving_images": ["3", 0], "dsize": 512, "scale": 2.3, "vx_ratio": 0.0, "vy_ratio": -0.125, "lip_zero": true, "eye_close_ratio": 0.4, "smile": 0.0, "source_video_eye_smooth": false, "mismatch_method": "constant"}},
    "5": {"class_type": "LivePortraitProcess", "inputs": {"liveportrait_model": ["1", 0], "source_image": ["4", 0], "driving_images": ["4", 1], "crop_info": ["4", 2], "delta_multiplier": 1.0, "relative_motion": true, "stitching": true, "expression_friendly": true, "driving_smooth_observation_variance": 1e-7}},
    "6": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["5", 0], "filename_prefix": "LivePortrait", "fps": 24.0, "lossless": false, "quality": 85, "method": "default"}}
  }
}'
```

## Parametri modificabili

| Parametro | Default | Note |
|---|---|---|
| WIDTH/HEIGHT (immagini) | 1024 | Max ~1536, multipli di 8 |
| WIDTH/HEIGHT (video) | 640 | Max ~832, multipli di 16 |
| SEED | random | Intero, stesso seed = stesso risultato |
| steps (immagini) | 6 | Più step = più dettaglio, più lento |
| cfg (immagini) | 1.0 | Più alto = più aderente al prompt |
| length (video) | 81 | Frame totali. 81=~5s, 49=~3s a 16fps |
| fps | 16.0 (video), 24.0 (lipsync) | Frame per secondo |

## Note importanti
- Output immagini: `/home/jarvis/ComfyUI/output/`
- Output video WEBP: `/home/jarvis/ComfyUI/output/`
- VAE per WAN 2.2: `wan_2.1_vae.safetensors` (NON wan2.2)
- ComfyUI processa un job alla volta — aspetta che finisca prima di inviare il successivo

### 9. Text-to-Speech — CustomVoice (Qwen3-TTS)

Genera audio con voce espressiva. Voci: sofia (IT donna), marco (IT uomo), emma (EN donna), james (EN uomo).
Parametro `instruct` opzionale per controllare emozione/stile (es. "Parla con entusiasmo", "Whisper softly").
Se `instruct` è vuoto usa il default del profilo.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "Qwen3TTSCustomVoice", "inputs": {"text": "TESTO_DA_SINTETIZZARE", "voice": "sofia", "language": "Italian", "instruct": "", "speaker_override": ""}},
    "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "TTS"}}
  }
}'
```

### 10. Voice Cloning (Qwen3-TTS Base)

Clona una voce da sample salvato. Voci disponibili: jarvis (IT maschile), eric (EN maschile).

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "Qwen3TTSVoiceClone", "inputs": {"text": "TESTO_DA_SINTETIZZARE", "speaker_name": "jarvis", "audio_path": "", "transcript": "", "language": "Italian"}},
    "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "Clone"}}
  }
}'
```

### 11. Speech-to-Text (Parakeet STT)

Trascrive audio in testo. Due modalità: da AUDIO tensor o da file path.

**Da file path:**
```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "ParakeetSTTFromFile", "inputs": {"audio_path": "/path/to/audio.wav", "response_format": "json"}}
  }
}'
```

**Da AUDIO tensor (es. output di LoadAudio):**
```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "LoadAudio", "inputs": {"audio": "UPLOADED_AUDIO"}},
    "2": {"class_type": "ParakeetSTT", "inputs": {"audio": ["1", 0], "response_format": "json"}}
  }
}'
```

**Alternativa senza ComfyUI — API diretta (più veloce):**
```bash
curl -s -X POST http://100.98.187.12:9000/v1/audio/transcriptions \
  -F 'file=@/path/to/audio.wav' \
  -F 'response_format=json'
```

### 12. Music Generation (ACE-Step 1.5)

Genera musica da prompt testuale. Output: WAV 48kHz stereo.

```bash
curl -s -X POST $COMFY/prompt -H "Content-Type: application/json" -d '{
  "prompt": {
    "1": {"class_type": "ACEStepGenerate", "inputs": {"prompt": "cinematic orchestral, epic, dramatic, strings, brass", "duration": 30.0, "lyrics": "", "seed": -1, "steps": 30, "guidance_scale": 15.0}},
    "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "Music"}}
  }
}'
```

Con lyrics strutturate:
```
"lyrics": "[verse]\nParole della strofa\n[chorus]\nRitornello qui"
```

**Alternativa senza ComfyUI — API diretta:**
```bash
curl -s -X POST http://100.98.187.12:7865/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "STILE_MUSICALE", "lyrics": "", "duration": 30, "return_audio": true}' \
  -o musica.wav
```
