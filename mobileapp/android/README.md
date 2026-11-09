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
- `live/` + `wear/WatchLiveAudio.kt`: sessione live full-duplex sul Voice Live Hub (`live_session_start` con `backend:"hub"`): `HubClient` TCP diretto all'hub, AEC on-device (telefono) o on-watch (tunnel via telefono). Attiva solo in sessione.

## Componenti chiave (`:wear`)

- `PhoneLink.kt` — apre il canale audio bidirezionale col telefono (`/jarvis/audio2`), manda i tap, riceve `HeadState`, gestisce mic/playback in base allo stato. In sessione live hub (`/jarvis/live`) mic comms + AEC locale sempre aperto; il parlato live lo suona `LivePlayout` (buffer anti-jitter + silenzio continuo, il track non si spegne tra una risposta e l'altra).

**Canale audio watch ↔ telefono.** Dalla 0.3.0 il verso telefono→watch è a frame (`shared/wear/WatchStream.kt`): TTS di oggi, parlato live (rate 16/24 kHz e stream media/comms nel frame) e clear del barge-in in ordine con l'audio. Il telefono 0.3.0 accetta anche il canale vecchio `/jarvis/audio` (PCM grezzo + `/jarvis/clear`), ma un watch 0.3.0 richiede il telefono 0.3.0: aggiornare sempre entrambe le APK. Le impostazioni del telefono ("Watch in sessione live") scelgono stream media/comms e 16/24 kHz senza reinstallare il watch.
**Live dal tap (0.4.0, Fase 3 del Voice Live Hub, design 19).** Se l'orchestrator abilita il device (`live_on_wake` nel `welcome` o nel push `{"type":"live_on_wake"}`, tenuto in cache per device_id) e l'interruttore "Il tap apre Gemini Live" è acceso, il tap in IDLE di telefono o watch manda `live_start{req_id,trigger:"tap"}` invece di `audio_start`. Il mic della modalità live parte subito (watch: `/jarvis/live` start, mai il mic x8) e riempie il buffer del comando (`shared/hub/CommandBuffer.kt`, tetto 8 s, VAD a energia → `offer_extra.command`), che va all'hub per primo dopo l'accept tardivo (7 s); all'accept parte `live_session_active`. `live_declined` (salvo `duplicate`/`voice_gate`), nessuna risposta in 2,5 s (`live_cancel`) o un hub che fallisce prima dell'accept portano subito al turno classico di oggi. Gli `announce` dell'orchestrator vanno all'hub come frame di controllo. Flag o interruttore spenti: il tap è quello della 0.3.0. Telefono e watch 0.4.0 vanno installati insieme.
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

- [x] Concentus verificato: `io.github.jaredmdobson:concentus:1.0.2` su Maven Central, package `io.github.jaredmdobson.concentus` (NON `org.concentus`). Fallback se mai servisse: `com.github.theeasiestway:android-opus-codec` via JitPack (JNI).
- [ ] Contesa del `controller` singleton tra UI telefono e relay watch: definire ownership (chi ha toccato per ultimo).
- [ ] `WatchBridgeService`: gestione robusta ciclo di vita canale (riapertura, timeout, nodo multiplo).
- [ ] Wear: avvio FGS/wake-lock durante la sessione per stabilità mic in background.
- [ ] Icone/animazioni: sostituire i placeholder Canvas con un asset "testa robot" dedicato (Lottie).
- [ ] Gestione permessi RECORD_AUDIO negata (UX di fallback).
- [ ] Test end-to-end contro l'orchestrator dopo lo spike Python.
