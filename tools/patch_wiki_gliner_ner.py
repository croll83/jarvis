#!/usr/bin/env python3
"""Corregge [[gliner-service]]: la NER e' dietro un interruttore, spenta per
default, perche' richiede un SECONDO modello in VRAM. Idempotente."""
from pathlib import Path

P = Path("/home/jarvis/wiki/entities/gliner-service.md")
s = P.read_text(encoding="utf-8")
MARK = "GLINER_NER_ENABLED"
if MARK in s:
    print("  = gia' aggiornata")
    raise SystemExit(0)

v = """L'estrattore per la NER si carica **solo** se chiedi `entita`: e' un secondo modello
in VRAM e la maggior parte dei consumer non ne ha bisogno."""
n = """> ⚠️ **La NER e' SPENTA per default.** Richiede un **secondo modello** in VRAM: il
> `Classifier` non sa estrarre e l'`AutoExtractor` non sa applicare vincoli, quindi
> servono entrambi. Misurato: il processo passa da **1266 a 2838 MiB**, e insieme al
> router generativo (4572 MiB) restano ~740 MiB su 8151 — troppo pochi perche'
> `llama-server` allochi un contesto lungo.
>
> Chiedendo `entita` con la NER spenta la risposta contiene `"entita": null` e un
> campo `avviso`. Per accenderla: `GLINER_NER_ENABLED=true` nella unit, **dopo** aver
> verificato che ci sia spazio sulla GPU. `GET /health` riporta `"ner": true|false`."""
assert s.count(v) == 1
P.write_text(s.replace(v, n), encoding="utf-8")
print("  ~ entities/gliner-service.md: NER dietro interruttore")
