#!/usr/bin/env python3
"""Aggiunge a [[gliner-service]] l'endpoint generico /classify e l'avvertenza su
cosa il modello fa bene e cosa no. Idempotente. Da eseguire su 100.116.99.9.
"""
from pathlib import Path

P = Path("/home/jarvis/wiki/entities/gliner-service.md")
MARK = "### A-bis. Via `/classify`"
s = P.read_text(encoding="utf-8")

if MARK in s:
    print("  = gia' aggiornata")
    raise SystemExit(0)

ANC = "### B. Direttamente, con la libreria"
NUOVO = """### A-bis. Via `/classify` — task arbitrari, nessun vincolo cablato

**Questo e' l'endpoint da usare se non sei il router di Jarvis.** `/route` ha i
vincoli di Jarvis dentro (`intent <=> natura AND argomento`): passandogli etichette
proprie te li ritrovi applicati alla tua semantica, e ottieni sempre la prima.

```python
post("/classify", {
    "text": "E' la terza volta che il servizio cade",
    "tasks": {                                   # quanti ne vuoi, tutti a scelta singola
        "urgenza": {"alta": "serve subito, blocca qualcosa", "bassa": "puo' aspettare"},
        "tono":    {"neutro": "informativo", "scontento": "lamentela o frustrazione"},
    },
    # opzionali:
    "vincoli": [{"tipo": "implies", "a": ["tono", "scontento"],
                                    "b": ["urgenza", "alta"]}],   # implies|iff|excludes
    "entita": {"persona": "il nome di una persona",               # NER
               "luogo": "una citta' o un sito"},
    "probabilita": True,        # per ripescare candidati alternativi
})
# -> {"urgenza": {"value": ..., "confidence": ...}, "tono": {...},
#     "entita": {"persona": [{"text": "Marco", "confidence": 0.997}], ...}, "ms": 63}
```

L'estrattore per la NER si carica **solo** se chiedi `entita`: e' un secondo modello
in VRAM e la maggior parte dei consumer non ne ha bisogno.

## Cosa fa bene, e cosa no

Provato da un nodo Tailscale con etichette completamente estranee alla domotica:

| compito | esito |
|---|---|
| **NER su entita' concrete** (persone, luoghi, prodotti) | eccellente — "Marco" 0,997, "Milano" 0,997 |
| **classificazione su vocabolario chiuso** di nomi propri | forte — vedi i finding |
| **classificazione astratta o soggettiva** (urgenza, tono, sentiment) | **mediocre** — 2 su 3 in una prova a campione |

La regola che se ne ricava: **piu' l'etichetta e' concreta e nominabile nel testo,
meglio va**. Una distinzione che vive nel *modo* in cui una cosa e' detta — e non in
*quali* parole ci sono — non la vede. Vedi il finding 8.

""" + ANC

s = s.replace(ANC, NUOVO, 1)
P.write_text(s, encoding="utf-8")
print("  ~ entities/gliner-service.md: aggiunti /classify e i limiti")
