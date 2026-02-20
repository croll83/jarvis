# Task: Multi-turn voice e follow-up automatico nell'orchestrator JARVIS

Lavora nella directory `jarvis-orchestrator/`. Leggi `main.py`, `config.py`, `database.py`, e `device_api.py` prima di fare modifiche.

---

## Modifica 1: Sessione multi-turn con OpenClaw (previous_response_id)

**Problema:** Ogni chiamata a `forward_to_openclaw()` è stateless — manda il payload a `/v1/responses` senza nessun identificatore di conversazione. OpenClaw tratta ogni richiesta come un utente nuovo, quindi se l'utente dice "e poi?" o "quale?" dallo speaker, Jarvis non sa a cosa si riferisce.

**Come funziona OpenClaw multi-turn:**
L'API `/v1/responses` supporta il parametro `previous_response_id`. Quando incluso, OpenClaw collega la nuova richiesta alla sessione precedente e mantiene tutta la chat history. Il campo `response.id` nella risposta SSE (evento `response.completed`) contiene l'ID da salvare.

**Fix:**

### 1a. Salvare il response_id dalla risposta SSE

In `forward_to_openclaw()`, nel parsing degli eventi SSE, quando ricevi `response.completed`, estrai e salva `response.id`:

```python
elif event_type == "response.completed":
    resp_data = event.get("response", {})
    final_status = resp_data.get("status")
    response_id = resp_data.get("id")  # ← NUOVO: salva l'ID
    if not accumulated_text:
        accumulated_text = _extract_openclaw_response(resp_data)
```

Cambia il return type di `forward_to_openclaw()` da `str` a `tuple[str, str | None]` — ritorna `(text, response_id)`.

### 1b. Storage del response_id per sessione voice

Crea un dizionario in-memory per tracciare le conversazioni attive per device/speaker:

```python
# In-memory multi-turn session tracking
# Key: device_id or speaker_id, Value: {"response_id": str, "timestamp": float}
_openclaw_sessions: dict[str, dict] = {}
OPENCLAW_SESSION_TTL = 300  # 5 minuti — dopo questo tempo la sessione scade
```

### 1c. Passare previous_response_id nelle richieste successive

In `forward_to_openclaw()`, accetta un nuovo parametro opzionale `previous_response_id: str = None`. Se presente, aggiungilo al payload:

```python
payload = {
    "input": message_text,
    "model": "openclaw:main",
    "stream": True,
}
if previous_response_id:
    payload["previous_response_id"] = previous_response_id
```

### 1d. Collegare il flusso in _handle_openclaw_voice()

In `_handle_openclaw_voice()`, prima di chiamare `forward_to_openclaw()`:

```python
# Recupera sessione multi-turn attiva per questo device
session_key = context.get("device_id") or context.get("speaker_id") or "default"
prev_session = _openclaw_sessions.get(session_key)
prev_response_id = None

if prev_session and (time.time() - prev_session["timestamp"]) < OPENCLAW_SESSION_TTL:
    prev_response_id = prev_session["response_id"]
    logger.info(f"Multi-turn: continuing session for {session_key} (prev_id={prev_response_id[:20]}...)")
else:
    logger.info(f"Multi-turn: new session for {session_key}")

# Passa a forward_to_openclaw
response, response_id = await forward_to_openclaw(
    text, context, hint=hint,
    stream_tts_callback=_stream_tts_chunk,
    previous_response_id=prev_response_id
)

# Salva il nuovo response_id
if response_id:
    _openclaw_sessions[session_key] = {
        "response_id": response_id,
        "timestamp": time.time()
    }
```

### 1e. Aggiorna TUTTI i caller di forward_to_openclaw()

Cerca tutti i punti in `main.py` dove viene chiamato `forward_to_openclaw()` e aggiorna per gestire il return tuple `(text, response_id)`. Per i caller non-voice (es. Telegram via `process_jarvis_logic`), puoi semplicemente ignorare il response_id:

```python
response, _ = await forward_to_openclaw(text, context, hint=hint)
```

---

## Modifica 2: Follow-up automatico — OpenClaw riapre il microfono

**Concetto:** Quando OpenClaw fa una domanda di follow-up (es. "Quale luce vuoi accendere?"), l'orchestrator deve automaticamente riaprire il listening sull'AtomS3R per ricevere la risposta, senza che l'utente debba ridire la wake word.

**Implementazione:**

### 2a. Rilevamento domande di follow-up

Dopo aver ricevuto la risposta da OpenClaw, analizza se è una domanda che richiede input dall'utente. Due approcci combinati:

**Approccio 1 — Euristica semplice:** Se la risposta finisce con `?` è probabilmente una domanda.

**Approccio 2 — Marker strutturato da OpenClaw (migliore):** OpenClaw può includere un marker JSON nel testo per segnalare che aspetta input. Ma siccome non possiamo modificare OpenClaw, usiamo l'euristica.

```python
def _needs_followup(response_text: str) -> bool:
    """Determina se la risposta richiede follow-up dall'utente."""
    if not response_text:
        return False
    
    text = response_text.strip()
    
    # Se finisce con domanda diretta
    if text.endswith("?"):
        return True
    
    # Pattern comuni di richiesta input
    followup_patterns = [
        "quale prefer",
        "quale vuoi",
        "quale luce",
        "dimmi",
        "scegli",
        "confermi",
        "vuoi che",
        "ti serve altro",
        "cosa intendi",
        "puoi specificare",
        "quale stanza",
        "a quale ti riferisci",
    ]
    text_lower = text.lower()
    return any(p in text_lower for p in followup_patterns)
```

### 2b. Trigger del listening sull'AtomS3R

L'AtomS3R attualmente ascolta solo dopo la wake word. Serve un endpoint HTTP che l'orchestrator può chiamare per triggerare il recording da remoto.

**Lato firmware (AtomS3R) — serve una modifica al firmware ESP32:**

Aggiungi un endpoint HTTP `POST /trigger_listen` sul webserver dell'AtomS3R che:
1. Attiva il microfono per la durata standard (es. 5-8 secondi)
2. Invia l'audio registrato a `/voice_stream` come fa normalmente dopo la wake word
3. Non richiede la wake word

**Lato orchestrator — dopo la risposta TTS:**

```python
# In _handle_openclaw_voice(), dopo il TTS della risposta:
if _needs_followup(response):
    # Aspetta che il TTS finisca di parlare prima di riaprire il mic
    tts_duration = len(response) / config.TTS_CHARS_PER_SECOND + 1.0
    await asyncio.sleep(tts_duration)
    
    # Triggera il listening sull'AtomS3R
    device_id = context.get("device_id")
    if device_id:
        device_ip = get_device_ip(device_id)  # Serve una lookup device_id → IP
        if device_ip:
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        f"http://{device_ip}/trigger_listen",
                        timeout=aiohttp.ClientTimeout(total=3)
                    )
                    logger.info(f"Follow-up: triggered listening on {device_id} ({device_ip})")
            except Exception as e:
                logger.warning(f"Follow-up: failed to trigger {device_id}: {e}")
```

### 2c. IP lookup per device

L'AtomS3R manda heartbeat con il suo IP. Salva l'IP nel database dei device:

```python
def get_device_ip(device_id: str) -> str | None:
    """Recupera l'ultimo IP noto di un device dal database."""
    # L'heartbeat endpoint già riceve ip_address — assicurati che venga salvato
    # nella tabella devices e recuperalo qui
    device = database.get_device(device_id)
    return device.get("ip_address") if device else None
```

In `device_api.py`, nell'endpoint `/heartbeat`, salva l'IP:

```python
@router.post("/heartbeat")
async def device_heartbeat(data: HeartbeatRequest, request: Request):
    # Salva IP dal request se non fornito nel body
    client_ip = data.ip_address or request.client.host
    database.update_device_ip(data.device_id, client_ip)
    # ... resto della logica
```

### 2d. Timeout del follow-up

Se l'utente non risponde entro il timeout di recording dell'AtomS3R (5-8 sec), semplicemente non succede nulla. La sessione multi-turn resta attiva per 5 minuti (TTL dalla Modifica 1), quindi se l'utente parla dopo dicendo la wake word, il contesto è comunque mantenuto.

---

## Schema del flusso completo

```
Utente: "Jarvis, accendi la luce"
    ↓
AtomS3R → /voice_stream → pre_route (DOMOTICA_INCERTA) → forward_to_openclaw(prev_id=None)
    ↓
OpenClaw: "Quale luce vuoi accendere? Soggiorno, camera o cucina?"
    ↓
Orchestrator: salva response_id, TTS streaming la risposta
    ↓
_needs_followup("...cucina?") → True → aspetta fine TTS → POST /trigger_listen
    ↓
AtomS3R riapre il mic → Utente: "Soggiorno"
    ↓
AtomS3R → /voice_stream → pre_route → forward_to_openclaw(prev_id=<saved_id>)
    ↓
OpenClaw (con contesto della conversazione): "Fatto! Luce del soggiorno accesa."
    ↓
_needs_followup("...accesa.") → False → fine conversazione
```

---

## Note implementative

- **Pulizia sessioni scadute:** Aggiungi un task periodico che pulisce `_openclaw_sessions` ogni 10 minuti (rimuovi entry con timestamp > TTL)
- **Gestione errori:** Se `forward_to_openclaw` fallisce e fa fallback a Qwen locale, non salvare nessun response_id
- **Telegram path:** Per le richieste che arrivano da Telegram (`process_jarvis_logic`), il multi-turn è già gestito da OpenClaw via la sessione Telegram nativa. Applica previous_response_id solo per il path voice (AtomS3R/VirtualMic)
- **Log:** Logga sempre quando si usa una sessione esistente vs nuova, per debug

## Priorità

La **Modifica 1** (multi-turn) è indipendente e può essere implementata subito.
La **Modifica 2** (follow-up automatico) richiede una modifica al firmware AtomS3R (endpoint `/trigger_listen`). Implementa prima il lato orchestrator, poi il firmware separatamente.
