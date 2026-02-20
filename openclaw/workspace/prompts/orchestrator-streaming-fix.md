# Task: Ottimizzare la latenza delle risposte voice nell'orchestrator JARVIS — 2 modifiche

Lavora nella directory `jarvis-orchestrator/`. Leggi `main.py`, `config.py` e `integrations.py` prima di fare modifiche.

---

## Modifica 1: Streaming TTS — Inizia a parlare prima che OpenClaw finisca

**Problema:** `forward_to_openclaw()` (main.py) riceve già SSE streaming da OpenClaw (eventi `response.output_text.delta`), ma accumula TUTTO il testo e lo restituisce solo alla fine. Il TTS parte solo quando la risposta completa è pronta. Questo aggiunge 10-60 secondi di silenzio.

**Fix:** Implementa delivery TTS progressivo durante lo streaming SSE. Man mano che i delta di testo arrivano da OpenClaw:

1. Accumula i delta in un buffer di frasi
2. Quando viene rilevato un confine di frase (`. ` o `! ` o `? ` o `\n`), manda quel chunk al TTS immediatamente tramite lo speaker
3. Continua ad accumulare la frase successiva mentre la precedente viene riprodotta
4. L'evento finale `response.output_text.done` gestisce il testo residuo nel buffer

**Dettagli implementativi:**
- `forward_to_openclaw()` attualmente prende `text, context, hint` e ritorna una stringa. Cambia la signature per accettare un `stream_tts_callback` async opzionale
- Crea `_handle_openclaw_voice_streaming()` che sostituisce l'attuale `_handle_openclaw_voice()` per le sorgenti AtomS3R/VirtualMic
- Il callback riceve ogni chunk di frase + context e chiama `speak()` / `speak_with_sound()` da `integrations.py`
- Per sorgente Telegram, mantieni il comportamento attuale (accumula testo completo, invia una volta)
- Per sorgente VirtualMic, pusha i chunk via SSE `event_bus.publish("voice_response_chunk", ...)`
- Gestisci lo speaking state correttamente: setta `speaking_state` al primo chunk, puliscilo dopo l'ultimo chunk + durata TTS stimata
- La funzione deve comunque ritornare il testo completo accumulato per logging/chat history

**Logica di split frasi:**
```python
import re
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Splitta il buffer in frasi complete e testo incompleto residuo."""
    parts = _SENTENCE_END.split(buffer)
    if len(parts) <= 1:
        return [], buffer  # Nessuna frase completa ancora
    complete = parts[:-1]
    remainder = parts[-1]
    return complete, remainder
```

**Vincoli importanti:**
- Non rompere i caller esistenti non-streaming di `forward_to_openclaw()` — lo streaming TTS è opt-in tramite il callback
- Il `deliver_final_response()` alla fine di `_handle_openclaw_voice()` va saltato quando lo streaming TTS è stato usato (per non ripetere l'ultima parte)
- Dimensione minima chunk per TTS: 20 caratteri (evita frammenti microscopici)
- Il sound type (positive/neutral/negative) va suonato solo sul PRIMO chunk

---

## Modifica 2: Skip del routing Qwen completo per pre-route ALTRO

**Problema:** Quando `pre_route()` ritorna `ALTRO` (non-domotica), il codice attuale nel `/voice_stream` endpoint già chiama direttamente `_handle_openclaw_voice()` bypassando `process_jarvis_logic()`. Questo è corretto. Però `_handle_openclaw_voice()` NON manda nessun feedback TTS immediato — fa solo un beep neutrale e poi silenzio per 10-60 secondi.

**Fix:** In `_handle_openclaw_voice()`, prima di chiamare `forward_to_openclaw()`:
1. Manda un breve TTS di acknowledgment allo speaker: "Ci penso..." o "Un attimo..." (scegli random da un pool)
2. Usa `speak()` direttamente (non `speak_with_sound()`) per velocità
3. Fallo solo per sorgente AtomS3R (VirtualMic riceve un indicatore testuale via SSE)
4. Questo sostituisce l'attuale chiamata `play_feedback_sound("neutral")` — non servono entrambi

**Pool di frasi di acknowledgment (italiano):**
```python
THINKING_PHRASES = [
    "Ci penso...",
    "Un attimo...",
    "Fammi controllare...",
    "Hmm, vediamo...",
    "Dammi un secondo...",
]
```

Scegli random con `random.choice()`.

---

## Test

Dopo l'implementazione, traccia un comando vocale tipo "Ciao Jarvis, che tempo fa domani?" e verifica:
1. Pre-route classifica come ALTRO
2. Lo speaker dice immediatamente "Ci penso..." (nessun delay di routing)
3. Man mano che OpenClaw manda la risposta in streaming, il TTS parte alla prima frase completa
4. La risposta completa viene salvata nella chat history
5. Lo speaking state è gestito correttamente
