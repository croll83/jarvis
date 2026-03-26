# Audio Tools Reference

## Parakeet-TDT — Speech-to-Text (STT)

Server always-on su GB10 (100.98.187.12:9000). Modello: Parakeet-TDT-0.6B-v3 (NeMo CUDA).
Performance: ~90x realtime. 25 lingue con auto-detection. VRAM: ~4.9 GB.

### API OpenAI-compatible (Whisper drop-in)

```bash
curl -s -X POST http://100.98.187.12:9000/v1/audio/transcriptions \
  -F 'file=@audio.wav' \
  -F 'response_format=json'
```

### Parametri
- \`file\`: file audio (WAV, MP3, OGG, FLAC — qualsiasi formato supportato da ffmpeg)
- \`response_format\`: \`json\` (default), \`text\`, \`verbose_json\` (con segments e timestamps)

---

## Qwen3-TTS — Text-to-Speech (v2: CustomVoice + Voice Clone)

Server always-on su GB10 (100.98.187.12:9880). Dual-model:
- **CustomVoice** (always loaded): voci preset con controllo espressività via instruct. VRAM: ~4.3 GB.
- **Base** (on-demand): voice cloning da sample audio. VRAM: +4.3 GB quando caricato.

Performance: ~0.9x RTF su CUDA bfloat16.

### Voci disponibili (CustomVoice profiles)

| Nome | Genere | Lingua | Speaker | Descrizione |
|---|---|---|---|---|
| **sofia** | Donna | Italiano | serena | Calda, espressiva, amichevole, rassicurante |
| **marco** | Uomo | Italiano | ryan | Caldo, sicuro, naturale, espressivo |
| **emma** | Donna | Inglese | serena | Warm, expressive, friendly, reassuring |
| **james** | Uomo | Inglese | ryan | Warm, confident, natural, expressive |

### Voci clone (Base model, on-demand)
- \`jarvis\`: voce clonata maschile italiana
- \`eric\`: voce maschile calda americana

### API OpenAI-compatible

```bash
curl -s -X POST http://100.98.187.12:9880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input": "Testo da narrare", "voice": "sofia"}' \
  -o narrazione.wav
```

Parametri JSON:
- \`input\`: testo da sintetizzare
- \`voice\`: nome profilo (\`sofia\`, \`marco\`, \`emma\`, \`james\`) o voce clone (\`jarvis\`, \`eric\`)
- \`language\`: override lingua (default: dal profilo)
- \`instruct\`: override istruzioni espressive (default: dal profilo)

### API Nativa CustomVoice (form-data)

```bash
curl -s -X POST http://100.98.187.12:9880/tts \
  -F 'text=Testo da narrare' \
  -F 'voice=sofia' \
  -o narrazione.wav
```

Parametri form:
- \`text\`: testo
- \`voice\`: nome profilo (sofia, marco, emma, james)
- \`speaker\`: nome speaker diretto (serena, ryan, aiden, vivian, sohee, etc.)
- \`language\`: lingua
- \`instruct\`: istruzioni per espressività/emozione (es. "Parla con entusiasmo", "Speak sadly")

### API Voice Clone (form-data)

```bash
curl -s -X POST http://100.98.187.12:9880/tts/clone \
  -F 'text=Testo da narrare' \
  -F 'speaker_name=jarvis' \
  -F 'language=Italian' \
  -o narrazione.wav
```

Upload mode (sample custom):
```bash
curl -s -X POST http://100.98.187.12:9880/tts/clone \
  -F 'text=Testo da narrare' \
  -F 'speaker_wav=@sample.wav' \
  -F 'speaker_transcript=Trascrizione del sample audio' \
  -F 'language=Italian' \
  -o narrazione.wav
```

### Speaker preset disponibili
aiden, dylan, eric, ono_anna, ryan, serena, sohee, uncle_fu, vivian

### Nodi ComfyUI
- **Qwen3-TTS CustomVoice Generate** (audio/tts): text + voice profile + instruct → AUDIO
- **Qwen3-TTS Voice Clone** (audio/tts): text + speaker_name o audio_path → AUDIO

---

## ACE-Step 1.5 — Generazione musica

Server always-on su GB10 (100.98.187.12:7865). Modello: ACE-Step-v1-3.5B.
Performance: ~4s per 30s di musica stereo 48kHz. VRAM: 7.8 GB.

### API

```bash
curl -s -X POST http://100.98.187.12:7865/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "cinematic orchestral, epic, dramatic, strings, brass",
    "lyrics": "",
    "duration": 30,
    "return_audio": true
  }' -o musica.wav
```

### Parametri principali
- \`prompt\`: tag di stile/genere (1000+ stili e strumenti, 50+ lingue)
- \`lyrics\`: testo canzone con \`[verse]\`, \`[chorus]\`, \`[bridge]\` (opzionale)
- \`duration\`: durata in secondi (10-600, default 30)
- \`seed\`: seed per riprodurre risultati (-1 = random)
- \`steps\`: passi di diffusione (default 30)
- \`guidance_scale\`: aderenza al prompt (default 15.0)
- \`return_audio\`: true = WAV diretto, false = path file
- Output: WAV 48kHz stereo

### Nodo ComfyUI
**ACE-Step Music Generate** (audio/music): prompt + duration + lyrics → AUDIO tensor.

---

## Formati audio supportati
- WAV (lossless, preferito per editing)
- MP3 (compresso, per output finale)
- AAC (compresso, per video MP4)
- OGG/Vorbis (compresso, alternativo)
