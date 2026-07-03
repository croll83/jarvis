# JARVIS Mobile

Client mobili che si comportano come un AtomS3R: **tap sulla testa robot → parli →
senti la risposta dell'orchestrator**, con multiturn e live session. Nessuna wakeword
(il tap la sostituisce).

## Architettura (vincolo Tailscale)

Il telefono è il **gateway di rete**: possiede la WS verso l'orchestrator, il token e
Tailscale. Lo smartwatch è **stateless** e parla col telefono via Wearable Data Layer
(BT/WiFi), senza mai toccare né Tailscale né il token.

```
Galaxy Watch ──ChannelClient/MessageClient──► Telefono (relay) ──WS + Tailscale──► Orchestrator /ws/audio
 (mic+testa)         PCM + controllo          (WS+Opus+token+TTL)   control+Opus
```

Lo stesso client telefono è anche il **client standalone Android** (widget in home) → il
"plus a costo zero".

## Protocollo

Specifica eseguibile di riferimento: [`tools/jarvis_ptt_spike/`](../tools/jarvis_ptt_spike/).
- Uplink **PCM** grezzo (`codec:"pcm"`), nessun encoding.
- Downlink **Opus** (TTS) → decodificato sul telefono (Concentus, pure Java).
- Fine turno: **VAD server** (silenzio) *oppure* **`audio_flush`** (secondo tap → invia subito).
- Connessione **on-demand** con TTL (default 10 min): prima chiamata più lenta, poi sprint.

Richiede la patch `audio_flush` in `jarvis-orchestrator/ws_audio_handler.py` (già nel repo)
e il device configurato in dashboard con **`use_internal_speaker = true`**.

## Fasi

- **Fase 1a — telefono Android** (`android/mobile`): client standalone + relay + widget. ← *scaffold presente*
- **Fase 1b — Galaxy Watch** (`android/wear`): testa robot + mic via Data Layer. ← *scaffold presente*
- **Fase 2 — iPhone + Apple Watch** (`ios/`, futura): Swift + URLSessionWebSocketTask + WatchConnectivity.

Vedi [`android/README.md`](android/README.md) per build, stato e TODO.
