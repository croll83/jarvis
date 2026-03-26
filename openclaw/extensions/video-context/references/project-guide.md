# Project Guide — Video Producer

## Struttura progetto

Ogni progetto vive in `projects/<slug>/` nel workspace dell'agent.

```
projects/<slug>/
├── project.yaml          # metadata + storyboard + stato
├── input/                # file forniti dall'utente
├── frames/               # keyframe/immagini generati
├── clips/                # video clip generati
├── audio/                # musica, SFX, narrazione
└── output/               # video finali assemblati
```

## project.yaml

```yaml
title: "Titolo del progetto"
description: "Descrizione breve"
created: "2026-03-21"
status: in_progress  # draft | in_progress | review | completed

resolution:
  width: 640
  height: 640

storyboard:
  - scene: 1
    description: "Descrizione scena"
    type: t2v  # t2i | t2v | i2v | faceswap | lipsync
    prompt: "Prompt per la generazione"
    status: pending  # pending | generating | done | rejected
    output: null  # filename quando generato
    notes: ""

  - scene: 2
    description: "Seconda scena"
    type: i2v
    prompt: "Prompt per animazione"
    input_image: "input/reference.png"
    status: pending
    output: null

audio:
  narration: null     # filename narrazione
  music: null         # filename musica
  sfx: []             # lista SFX con timestamp

output:
  final: null         # filename video finale
  format: mp4
  fps: 16
```

## Naming convention

- Frames: `frames/scene_01_v1.png`, `frames/scene_01_v2.png` (versioni)
- Clips: `clips/scene_01.webp`, `clips/scene_01.mp4`
- Audio: `audio/narration.wav`, `audio/music.mp3`, `audio/sfx_explosion.wav`
- Output: `output/final_v1.mp4`, `output/final_v2.mp4`

## Flusso operativo

### 1. Setup progetto
- Creare cartella e sottocartelle
- Creare project.yaml con metadata e storyboard iniziale
- Se l'utente fornisce file, salvarli in `input/`

### 2. Approvazione storyboard
- Mostrare storyboard all'utente scena per scena
- Attendere approvazione o modifiche
- Aggiornare project.yaml

### 3. Generazione asset (una scena alla volta)
- Generare l'asset (immagine o video)
- Scaricare output da ComfyUI e salvare nel progetto
- Mostrare risultato all'utente
- Se approvato: aggiornare status a "done" e salvare filename in output
- Se rifiutato: proporre variazione (seed diverso, prompt modificato) e rigenerare

### 4. Audio
- Generare narrazione con XTTS se necessario
- Aggiungere musica/SFX
- Salvare in `audio/`

### 5. Assemblaggio
- Concatenare clip con ffmpeg
- Aggiungere audio
- Salvare in `output/`
- Mostrare risultato finale

### 6. Iterazione
- L'utente può chiedere modifiche a singole scene
- Rigenerare solo le scene modificate
- Riassemblare il video finale

## Regole importanti

- MAI procedere alla scena successiva senza approvazione
- MAI sovrascrivere asset approvati — creare nuove versioni (v1, v2, ecc.)
- Aggiornare SEMPRE project.yaml dopo ogni operazione
- Mostrare SEMPRE il risultato all'utente prima di procedere
- Per video lunghi: ogni clip è ~5 secondi (81 frame a 16fps), pianificare di conseguenza
