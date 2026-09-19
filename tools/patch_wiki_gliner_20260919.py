#!/usr/bin/env python3
"""Aggiunge alla llm-wiki la voce dedicata a GLiNER come COMPONENTE RIUSABILE
(19 settembre 2026).

La pagina [[gliner-router]] gia' creata descrive il ruolo di GLiNER dentro il
router di Jarvis. Questa descrive GLiNER *in se'*: cosa e' installato, dove, con
che API si usa, e tutti i finding misurati in due giorni di lavoro — cosi' un
altro consumer (Hermes in primis) parte subito senza rifare gli stessi errori.

1. entities/gliner-service.md — pagina nuova
2. index.md                   — sotto l'orchestrator
3. log.md                     — append (richiesto da SCHEMA.md)

Idempotente. Va eseguito sulla workstation (100.116.99.9), in /home/jarvis/wiki.
"""
from pathlib import Path

WIKI = Path("/home/jarvis/wiki")
TODAY = "2026-09-19"
changed = []

PAGE = f"""---
title: GLiNER Service (classificatore zero-shot condiviso)
created: {TODAY}
updated: {TODAY}
type: entity
tags: [jarvis, llm, local, gpu, nlp, classification, ner, api, benchmark]
sources: []
---

# GLiNER Service (classificatore zero-shot condiviso)

Servizio **condiviso** sulla rete Tailscale: classifica testo ed estrae entita' su
**vocabolari definiti a runtime**, senza fine-tuning. Nato per il router di Jarvis
(vedi [[gliner-router]]), esposto a tutta la tailnet perche' serve anche ad altri
consumer — [[hermes-ai-agent|Hermes]] in primis.

Non genera testo. Segna la **similarita' fra l'embedding di uno span e quello di
un'etichetta**, e sceglie da elenchi chiusi. Se serve *scrivere* qualcosa, serve un
LLM generativo: vedi [[llama-router-llm]].

## Dove sta

| | |
|---|---|
| host | atomman — `100.88.84.81` (Tailscale) |
| endpoint | `http://100.88.84.81:11436` |
| unit | `gliner-router.service` (systemd sull'host, **non** in Docker) |
| codice | `/opt/jarvis/gliner-router/server.py` — nel repo sotto `gliner-router/` |
| venv | `/home/jarvis/gliner-eval` — Python 3.12.3 |
| modello | `fastino/gliner2.5-multi-v1` — mDeBERTa-v3, 287M parametri |
| VRAM | ~1,65 GiB (574 MiB di pesi + contesto CUDA) |
| latenza | intento ~11ms, azione ~22ms, bersaglio su 150 etichette ~65ms |

Pacchetti: `gliner2==2.0.0`, `gliner==0.2.29`, `torch==2.11.0+cu128`,
`transformers==4.57.6`, `sentencepiece`, `protobuf`, `fastapi`, `uvicorn`.

**Sta fuori dal container per una ragione misurata**: l'immagine dell'orchestrator
ha `torch` **CPU**, e su CPU il bersaglio costa **1370ms** contro **34ms** in GPU.

**Rete**: ascolta su `0.0.0.0`, ma `ufw` ha policy DROP e la 11436 e' aperta solo da
`100.64.0.0/10` — raggiungibile da ogni nodo Tailscale, **non** dalla LAN.

## Documentazione

- Model card: <https://huggingface.co/fastino/gliner2.5-multi-v1>
- Libreria GLiNER2: <https://github.com/fastino-ai/GLiNER2> — i tutorial in
  `tutorial/` sono la cosa piu' utile, in particolare `14-constrained_classification.md`
- **Architettura** (altra libreria, stesso principio): <https://urchade.github.io/GLiNER/architectures.html>
  — spiega perche' l'ordine delle etichette conta, vedi sotto

## Come si usa

### A. Via il servizio (consigliato per altri consumer)

`POST /route` — deliberatamente **stupido**: riceve etichette, restituisce punteggi.
Non sa cosa sia un intent. Le ancore dei vincoli si passano per nome.

```python
import json, urllib.request

corpo = {{
    "text": "accendi la luce della cucina",
    # ORDINE SIGNIFICATIVO: vedi i finding
    "intent_labels": ["accendere o spegnere qualcosa", "chiedere informazioni", ...],
    "natura_labels": ["far succedere qualcosa a un apparecchio",
                      "sapere soltanto com'e' messa una cosa"],
    "argomento_labels": ["dispositivi di casa", "roba fuori casa: mail, conti, notizie"],
    "ancore": {{"home": "accendere o spegnere qualcosa", "simple": "chiedere informazioni",
               "agent": "...", "comando": "...", "domanda": "...",
               "casa": "...", "fuori": "..."}},
    "bersaglio_labels": ["Cucina", "Luce Box", ...],        # vocabolario chiuso
    "azione_labels": {{"turn_on": "Accendere", "turn_off": "Spegnere", ...}},
    "extra": {{"grandezza": {{"temperatura": "Temperatura", ...}}}},  # domande in piu'
}}
req = urllib.request.Request("http://100.88.84.81:11436/route",
    data=json.dumps(corpo).encode(), headers={{"Content-Type": "application/json"}})
r = json.load(urllib.request.urlopen(req, timeout=20))
# r["intent"]["value"], r["bersaglio"]["value"], r["bersaglio"]["probabilities"],
# r["azione"]["value"], r["ms"]
```

`GET /health` → `{{"ok": true, "model": ..., "device": "cuda"}}`.

Il campo `extra` accetta domande arbitrarie a scelta singola:
`{{nome: {{etichetta: descrizione}}}}`.

### B. Direttamente, con la libreria

```python
from gliner2 import AutoExtractor
from gliner2.classification import (Classifier, ClassificationSchema,
                                    ClassificationConfig, constraints as C)

# ATTENZIONE: `map_location`, non `.to("cuda")` — il wrapper non e' un nn.Module
m   = AutoExtractor.from_pretrained("fastino/gliner2.5-multi-v1",
                                    map_location="cuda", quantize=True)
clf = Classifier.from_pretrained("fastino/gliner2.5-multi-v1",
                                 map_location="cuda", dtype="float16")

schema = (ClassificationSchema()
    .single("intento", ["comando", "domanda"])
    .single("tipo", ["luce", "tapparella"])
    .constrain(C.implies(("intento", "comando"), ("tipo", "luce"))))

cfg = ClassificationConfig(decoder="exact", beam_size=32, on_infeasible="relax",
                           include_confidence=True)
r = clf.classify("accendi la luce", schema, config=cfg).to_dict()
```

Utili: `clf.score(text, schema)` restituisce i **logit grezzi** per etichetta;
`result.probabilities("task")` serve a ripescare un candidato alternativo;
`m.extract_entities(text, {{label: descrizione}})` per la NER.

## Finding — leggere PRIMA di usarlo

Tutti misurati su un banco di 426 casi reali. Il rumore di fondo e' 0,4-1,1 punti:
sotto i ~2 punti non si conclude niente.

### 1. La classificazione su insieme chiuso e' la primitiva FORTE

| approccio | resa |
|---|---|
| `.single()` su vocabolario chiuso | **84,0%** (bersaglio), **88,6%** (azione) |
| estrazione entita' + aggancio fuzzy | 49,2% |
| campi `.structure().field(choices=)` | 68,7% su enum da 10, **7,3%** su enum da 100+ |

Se il valore e' enumerabile, **classifica**; non estrarre.

### 2. L'ORDINE delle etichette conta, e parecchio

Encoder singolo: le etichette finiscono **nello stesso prompt del testo**, quindi la
loro posizione condiziona il risultato. Misurato sul bersaglio: da **63,1% a 75,8%**
per sola permutazione.

Ordini migliori trovati: **nomi corti per primi** su un vocabolario di entita',
**i piu' frequenti per primi** su un insieme di intenti.

> Un bug in produzione lo ha confermato: una chiave di cache con `sorted()`
> riordinava le etichette alfabeticamente e l'azione scendeva da 86,8% a 81,9%.
> **Non ordinare mai le etichette per comodita' di caching.**

### 3. NON spezzare il vocabolario in gruppi

La doc di GLiNER v1 dice che oltre ~30 tipi la resa degrada. Su **questa**
architettura e' il contrario, in modo monotono: gruppi da 20 → 62,7%, da 30 → 66,2%,
da 50 → 70,4%, **tutte insieme → 77,7%**. Il modello guadagna dal vedere tutti i
candidati in competizione nello stesso prompt.

### 4. Etichette CORTE e concrete, mai prose

| forma | resa (intento) |
|---|---|
| etichette corte | **77%** |
| criteri in prosa lunga come etichette | **26%** |
| descrizioni ricche di esempi | peggiorano |

Le **parentesi sono vietate** in etichette e descrizioni (`_RESERVED` in
`gliner2/classification/schema.py`): corrompono l'allineamento fra logit ed etichetta.

### 5. Le leve che NON funzionano

- `instruction=` per task: fa **danno** (75,1% → 59,7%)
- `temperature=` su un task singolo: **inerte per costruzione** — dividere tutti i
  logit per T non cambia quale sia il massimo
- `examples=` few-shot: instabili, ±10 punti secondo quali esempi peschi
- bias per classe tarato sui logit: nessun guadagno fuori campione
- `quantize=True` su `Classifier`: non riduce la VRAM

### 6. I vincoli DSL aiutano l'intento, non il bersaglio

`constrain()` con decodifica unificata vale **+3,8 punti puliti** sull'intento e
riduce la varianza. Sul bersaglio invece peggiora (76,9% → 73,1%).

Il pattern che funziona: dichiarare due domande **ausiliarie e binarie** e legarle
all'intento con `iff`/`implies`. Le binarie sono il punto forte del modello.

### 7. Piu' task nello stesso schema si DISTURBANO

Condividono il prompt dell'encoder: mettendo insieme intento, bersaglio e azione si
perdono ~3 punti sul bersaglio e ~2,6 sull'azione. **Chiamate separate** sono piu'
lente ma piu' precise.

### 8. Cosa il modello NON puo' vedere

E' similarita' fra embedding, quindi le distinzioni **sintattiche** gli sfuggono:

- **comando contro domanda** — *"accendi la luce della cucina"* e *"quali luci sono
  accese in cucina?"* hanno le stesse parole e cambiano nel modo del verbo. In regole
  italiane: **90,4%**.
- **coreferenza** — *"ora spegnila"*: dichiarata come classe fa **0/34**; in codice,
  riusando l'ultima azione eseguita, fa **7/7** in 0,08ms.
- **firme lessicali** — "serve uno strumento esterno" (mail, calendario, trading):
  il modello lo prende al 10%, una regola lessicale **21/21 con 0 falsi positivi su 405**.

Gli **span attributes** non aiutano qui: sono *span-conditioned*, cioe' rispondono a
"cosa e' vero di **questo span**". Su *"accendi la luce della cucina"* l'attributo
sullo span "cucina" esce sempre uguale, perche' l'informazione sta nel verbo.

### 9. Trappole operative

- `GLiNER.from_pretrained` (legacy) **non** funziona: serve `gliner2` +
  `AutoExtractor` / `Classifier`
- `.to("cuda")` sul wrapper fallisce: usare `map_location="cuda"`
- `classify_text` vuole un **dict**, non una lista
- `du -sh` sulla cache HF dice **116K**: sono symlink nei blob. I pesi veri sono
  **1,15 GB** — verificare con `ls -lL` sul `.safetensors`
- il primo `classify` su uno schema nuovo costa ~400ms (compilazione), poi ~100ms

## Quando NON usarlo

Se il valore da produrre e' **testo libero** — una query di ricerca, il titolo di un
brano, il corpo di una mail — GLiNER non puo' scriverlo: sceglie e basta. Serve un
generativo. Nel router di Jarvis e' esattamente questo il criterio con cui passa la
mano a [[llama-router-llm|Qwen]].
"""

p = WIKI / "entities/gliner-service.md"
if not p.exists():
    p.write_text(PAGE, encoding="utf-8")
    changed.append("entities/gliner-service.md")
    print("  + entities/gliner-service.md creata")
else:
    print("  = entities/gliner-service.md gia' presente")

# ───────────────────────────────────────────────────────────── index.md
idx_p = WIKI / "index.md"
idx = idx_p.read_text(encoding="utf-8")
if "[[gliner-service]]" not in idx:
    anc = "- [[gliner-router]] — Decisore primario (GLiNER 2.5 multi-v1, locale, ~68ms)"
    if anc in idx:
        idx_p.write_text(idx.replace(anc, anc +
            "\n- [[gliner-service]] — GLiNER come componente riusabile su Tailscale (API + finding)"),
            encoding="utf-8")
        changed.append("index.md")
        print("  ~ index.md aggiornato")
    else:
        print("  ! index.md: ancora non trovata, salto")
else:
    print("  = index.md gia' aggiornato")

# ───────────────────────────────────────────────────────────── log.md
log_p = WIKI / "log.md"
log = log_p.read_text(encoding="utf-8")
MARK = f"## {TODAY} — GLiNER esposto su Tailscale"
if MARK not in log:
    log_p.write_text(log.rstrip() + f"""

{MARK}

Il servizio GLiNER (`gliner-router.service`, atomman :11436) passa da `127.0.0.1` a
`0.0.0.0`: e' ora raggiungibile da ogni nodo Tailscale, perche' serve anche a consumer
diversi dall'orchestrator — [[hermes-ai-agent|Hermes]] in primis. Non e' esposto sulla
LAN: `ufw` ha policy DROP e la porta e' aperta solo da `100.64.0.0/10`, con la stessa
convenzione degli altri servizi dell'host.

Nuova pagina [[gliner-service]]: cosa e' installato e dove, le due API (servizio e
libreria), i puntamenti a model card e tutorial, e **i finding misurati** — la
classificazione su insieme chiuso batte l'estrazione (84,0% contro 49,2%), l'ordine
delle etichette vale fino a 12 punti, spezzare il vocabolario peggiora, e le
distinzioni sintattiche (comando vs domanda, coreferenza) vanno in codice perche' un
classificatore a similarita' di embedding non le vede.

Nello stesso giro il router generativo e' tornato sotto systemd: girava come processo
avviato a mano per il bench, mentre `llama-router.service` era `enabled` ma `inactive`
e puntava ancora al 7B — al primo riavvio la produzione sarebbe tornata silenziosamente
al modello vecchio.
""", encoding="utf-8")
    changed.append("log.md")
    print("  ~ log.md appeso")
else:
    print("  = log.md gia' aggiornato")

print()
print(f"Modificati {len(changed)} file: " + ", ".join(changed) if changed
      else "Niente da fare: la wiki era gia' aggiornata.")
