# Intelligent Router — Integration Guide

## Come Funziona il Routing Ora

### Flusso di Decisione

```
Task arriva da utente
        │
        ▼
┌──────────────────────┐
│ Regole Esplicite     │  ← PRIORITÀ 1: match su keyword/pattern noti
│ (SOUL.md)            │     "ragiona" → Opus, smart home → Haiku, ecc.
└──────────┬───────────┘
           │ no match chiaro
           ▼
┌──────────────────────┐
│ Classificatore       │  ← PRIORITÀ 2: 15-dimension weighted scoring
│ (router.py)          │     Restituisce tier + confidence
└──────────┬───────────┘
           │ confidence bassa o errore
           ▼
┌──────────────────────┐
│ Default: MEDIUM      │  ← FALLBACK: Sonnet (safe default)
│ (Sonnet)             │
└──────────────────────┘
```

### I 5 Tier

| Tier | Quando | Modello | Costo stimato/task |
|------|--------|---------|-------------------|
| 🟢 **SIMPLE** | Chat, status, smart home, FAQ | Haiku (diretto) | ~$0.001 |
| 🟡 **MEDIUM** | Analisi, ricerche, scrittura | Sonnet | ~$0.02 |
| 🟠 **COMPLEX** | Code multi-file, debug cross-file | Sonnet + thinking | ~$0.05 |
| 🧮 **REASONING** | "ragiona", prove, strategia | Opus + thinking=high | ~$0.10 |
| 🔴 **CRITICAL** | Security, deploy prod, finanza | Opus + thinking=high | ~$0.15 |

### Fallback Chains (da config.json)

- **SIMPLE**: Qwen Local → Haiku → Gemini Flash
- **MEDIUM**: Sonnet → Gemini 3 Flash → Grok
- **COMPLEX**: Sonnet → Gemini 3 Pro → Opus
- **REASONING**: Opus → Sonnet → Gemini 3 Pro
- **CRITICAL**: Opus → Gemini 3 Pro

Max 3 tentativi per request.

## Come Usare il Classificatore

### Da CLI (test/debug)
```bash
cd ~/.openclaw/workspace/skills/intelligent-router
python3 scripts/router.py classify "build auth system with JWT"
python3 scripts/router.py score "prove sqrt(2) is irrational"
python3 scripts/router.py models
python3 scripts/router.py health
```

### Da Haiku (nel flusso di routing)

Haiku NON chiama il classificatore Python direttamente. Il routing è **decision-based nelle regole SOUL.md**. Il classificatore serve come:

1. **Tool di validazione** — per verificare se il tuo istinto di routing è corretto
2. **Suggerimento per task ambigui** — quando le regole esplicite non matchano
3. **Debug** — per capire perché un task è stato classificato in un certo modo

### Esempio di Flusso Mentale per Haiku

```
Utente: "Analizza il codice di auth.py e trova vulnerabilità"

Haiku pensa:
1. Parola chiave "analizza" + "vulnerabilità" → sembra security
2. Regola esplicita: "Review di sicurezza" → CRITICAL/REASONING → Opus
3. Decisione: sessions_spawn(task="...", model="opus", thinking="high")
```

```
Utente: "Che tempo fa domani?"

Haiku pensa:
1. Domanda semplice, info fattuale
2. Regola esplicita: "Meteo, orari, info fattuali" → SIMPLE
3. Decisione: gestisco io direttamente
```

## Limitazioni Note

- **Italiano**: Il classificatore è ottimizzato per inglese. "ragiona" non triggera REASONING nel classifier (ma lo fa nelle regole esplicite SOUL.md)
- **Task corti**: Prompt brevi hanno pochi segnali → classificati come SIMPLE
- **Contesto mancante**: Il classificatore vede solo il testo del task, non la storia della conversazione

## Config

- **Modelli e costi**: `skills/intelligent-router/config.json`
- **Regole routing**: `SOUL.md` sezione "Smart Model Routing"
- **Script classificatore**: `skills/intelligent-router/scripts/router.py`
