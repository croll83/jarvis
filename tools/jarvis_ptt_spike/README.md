# JARVIS PTT Spike Client

Client Python che valida il protocollo `/ws/audio` dell'orchestrator **simulando il
futuro client Galaxy Watch / telefono** (modello "tap sulla testa del robot", no wakeword).

Serve a provare l'intera catena end-to-end **prima** di aprire Android Studio:

```
TAP → connessione on-demand → audio_start(pcm) → stream PCM 16k →
   (silenzio VAD | secondo tap = audio_flush) → speech_end →
   STT → AI → TTS → frame Opus → decode → speaker
```

## Setup

```bash
cd tools/jarvis_ptt_spike
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # PortAudio: sudo apt install libportaudio2
```

**macOS** (opuslib è un wrapper su libopus nativa, va installata a parte, altrimenti
"Could not find Opus library"):

```bash
brew install portaudio opus
export DYLD_LIBRARY_PATH="$(brew --prefix opus)/lib:$DYLD_LIBRARY_PATH"
```

Se libopus non c'è, lo spike **non si blocca**: disattiva solo il playback della TTS e
logga i frame TTS in arrivo (mic e pipeline restano testabili).

## Uso

```bash
export JARVIS_WS_URL=ws://<orchestrator-tailscale-ip>:5000      # porta orchestrator, senza /ws/audio
export JARVIS_DEVICE_TOKEN=<DEVICE_API_TOKEN>                    # se configurato

# microfono live (default)
python jarvis_ptt_client.py

# oppure invia un WAV 16kHz mono (utile per test ripetibili in CI)
python jarvis_ptt_client.py --wav comando_16k_mono.wav

# TTL connessione calda a 2 minuti (default 600s = 10 min)
python jarvis_ptt_client.py --ttl 120
```

Runtime: **ENTER = TAP** (avvia turno / secondo tap = flush / barge-in durante la risposta), **`q` + ENTER = esci**.

## Prerequisito lato server

1. Applica la patch `audio_flush` in `jarvis-orchestrator/ws_audio_handler.py` (già inclusa nel repo).
2. Il device (`--device-id`) viene **auto-registrato** al primo `hello`; poi in dashboard admin
   imposta almeno: `location_id`, `friendly_name`, e **`use_internal_speaker = true`**
   (altrimenti la TTS va su uno speaker HA e qui non arriva audio).

## Cosa dimostra

- codec **PCM in uplink** (nessun Opus da encodare lato client)
- decode **Opus in downlink** (TTS)
- **connessione on-demand** con TTL: prima chiamata più lenta, successive sprint
- fine turno via **VAD server** oppure **audio_flush** (secondo tap)
- **multiturn** e **live session** (riattivazione mic automatica su `trigger_listen`)

Questo file è la specifica eseguibile del protocollo: la state machine di `on_tap()` /
`_on_text()` è ciò che riscriverai in Kotlin (relay telefono) e Swift (iPhone).
