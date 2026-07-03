# JARVIS Android (mobile + wear)

Progetto Gradle multi-modulo. Apri `mobileapp/android/` in Android Studio (Ladybug+).

## Moduli

| Modulo    | Ruolo |
|-----------|-------|
| `:shared` | Costanti protocollo, `AudioFormat`, `HeadState`, path Data Layer, `MicRecorder`/`TtsPlayer` (riusati da mobile e wear). |
| `:mobile` | Telefono: client standalone **+** relay per lo watch **+** widget home. Possiede la WS, il token, Opus decode, il TTL. |
| `:wear`   | Galaxy Watch: testa robot animata + mic, stateless, parla col telefono via Data Layer. Include la Tile. |

## Componenti chiave (`:mobile`)

- `ws/JarvisController.kt` — **il cervello**: state machine `/ws/audio`, on-demand + TTL, tap→audio_start→stream→flush/VAD→TTS. Equivalente Kotlin dello spike Python.
- `core/JarvisRuntime.kt` — holder singolo di config + controller (condiviso tra UI/service/relay).
- `service/JarvisForegroundService.kt` — tiene calda la WS + mic locale (FGS `microphone`).
- `wear/WatchBridgeService.kt` — `WearableListenerService`: ponte Data Layer ↔ controller.
- `audio/OpusDecoderWrapper.kt` — decode TTS (Concentus).
- `ui/` — testa robot Compose + tap + impostazioni orchestrator.
- `widget/` — widget Glance (tap → MainActivity con auto-tap).

## Componenti chiave (`:wear`)

- `PhoneLink.kt` — apre il canale audio bidirezionale col telefono, manda i tap, riceve `HeadState`, gestisce mic/playback in base allo stato.
- `RobotHeadScreen.kt` — testa robot (Canvas) cliccabile.
- `tile/JarvisTileService.kt` — Tile "widget"; tap → apre MainActivity.

## Build

Android Studio genera il Gradle wrapper all'import (o `gradle wrapper`). Poi:

```bash
./gradlew :mobile:assembleDebug     # APK telefono
./gradlew :wear:assembleDebug       # APK watch
```

## Setup runtime

1. Applica la patch `audio_flush` all'orchestrator (già nel repo).
2. Installa `:mobile` sul telefono (con Tailscale attivo), apri → Impostazioni → URL `ws://<tailscale-ip>:5000` + token.
3. Il `device_id` (pseudo-MAC generato) viene auto-registrato al primo `hello`; in dashboard imposta `location_id`, `friendly_name` e **`use_internal_speaker = true`**.
4. Installa `:wear` sul Galaxy Watch (companion, non standalone).

## Stato / TODO

Questo è uno **scaffold funzionale** da rifinire durante l'integrazione:

- [ ] Verificare la versione dell'artefatto Concentus (`io.github.jaredmdobson:concentus`) e il package `org.concentus`.
- [ ] Contesa del `controller` singleton tra UI telefono e relay watch: definire ownership (chi ha toccato per ultimo).
- [ ] `WatchBridgeService`: gestione robusta ciclo di vita canale (riapertura, timeout, nodo multiplo).
- [ ] Wear: avvio FGS/wake-lock durante la sessione per stabilità mic in background.
- [ ] Icone/animazioni: sostituire i placeholder Canvas con un asset "testa robot" dedicato (Lottie).
- [ ] Gestione permessi RECORD_AUDIO negata (UX di fallback).
- [ ] Test end-to-end contro l'orchestrator dopo lo spike Python.
