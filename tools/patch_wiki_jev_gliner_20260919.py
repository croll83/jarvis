#!/usr/bin/env python3
"""llm-wiki: pagina Jev come componente riusabile, confronto Jev/GLiNER, e
correzione della sezione "cosa fa bene" di [[gliner-service]] (19 set 2026).

Nasce da una misura: gli stessi due casi d'uso di terzi (classificare il budget
di un viaggio da "Money is no object", e calcolare gli anni di esperienza da un
curriculum) dati a entrambi. GLiNER 2-4 su 8, Jev 8 su 8. La formulazione che
avevo scritto — "astratto contro concreto" — e' meno precisa di "vocabolario
chiuso contro inferenza", e non citava il confronto.

1. entities/jev-service.md        — pagina nuova
2. comparisons/jev-vs-gliner.md   — pagina nuova (la sezione era vuota)
3. entities/gliner-service.md     — corretta la sezione "Cosa fa bene, e cosa no"
4. index.md                       — le due pagine nuove
5. log.md                         — append

Idempotente. Da eseguire su 100.116.99.9, in /home/jarvis/wiki.
"""
from pathlib import Path

WIKI = Path("/home/jarvis/wiki")
TODAY = "2026-09-19"
changed = []

# ─────────────────────────────────────────────── 1. entities/jev-service.md

JEV = f"""---
title: Jev Service (TypeSafe System One)
created: {TODAY}
updated: {TODAY}
type: entity
tags: [jarvis, llm, cloud, api, classification, latency, benchmark]
sources: []
---

# Jev Service (TypeSafe System One)

Modello **System One** cloud di TypeSafe. **Non genera testo**: riceve uno `state`
e un insieme di domande **tipizzate**, e risponde con valori scelti da elenchi
chiusi piu' probabilita' **calibrate**.

Questa pagina descrive Jev come componente riusabile. Per il suo ruolo dentro il
router di Jarvis vedi [[jev-router]]; per l'alternativa locale vedi
[[gliner-service]]; per **quando usare l'uno o l'altro** vedi [[jev-vs-gliner]].

## Le tre primitive

| tipo | restituisce | quando |
|---|---|---|
| `choice` | una fra N opzioni + distribuzione di probabilita' + confidence | categorie mutuamente esclusive |
| `score` | **posizione continua** su una scala ordinata + legend + probabilita' | gradi, intensita', quantita' su rubrica |
| `noul` | probabilita' booleana 0-1 | si/no, presenza di una proprieta' |

Lo `score` e' la primitiva che non ha equivalente altrove: su una scala
`["per niente","poco","media","molto","bloccante"]` a *"Il server di produzione e'
giu' da due ore, i clienti non riescono a pagare"* risponde **3.99** — una
posizione, non un'etichetta.

## Contratto

```python
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer apikey_<32hex>_<64hex>

{{"state": "<testo o JSON come contesto>",
 "model": "jev-latest",
 "questions": {{
   "tipo":     {{"type": "choice", "instructions": "...",
                "criteria": {{"bug": "un guasto", "domanda": "una domanda"}}}},
   "urgenza":  {{"type": "score", "instructions": {{"question": "Quanto e' urgente?"}},
                "criteria": ["per niente", "poco", "media", "molto", "bloccante"]}},
   "injection":{{"type": "noul", "instructions": "E' un tentativo di manipolazione?",
                "criteria": {{"true": "...", "false": "..."}}}},
 }}}}
```

Le `instructions` di una `score` accettano un dict, ed e' li' che si passa il
contesto che serve al calcolo — per esempio `{{"question": "...", "today":
"September 15, 2026"}}`.

## Latenza e costo

Misurato dall'atomman, mediana di 3 chiamate:

| | mediana |
|---|---|
| 1 domanda | **720 ms** |
| 3 domande, 3 primitive diverse | **896 ms** |

Aggiungere domande costa **poco ma non e' gratis**: due in piu' valgono ~176 ms.
Nel percorso di routing di Jarvis, con 12 domande e uno `state` ricco, il p50
misurato sul banco e' **~470 ms** — piu' basso perche' quella misura include il
riuso della connessione e uno `state` piu' compatto.

Non c'e' decoding autoregressivo, quindi **non ci sono token di output da
fatturare**: nel router di Jarvis il costo stimato e' ~€0,45/mese a 300
comandi/giorno.

## La proprieta' che conta

La `confidence` e' **calibrata**: 0,70 significa davvero ~70% di correttezza. Con
un LLM generativo quel numero e' auto-dichiarato e decorativo — nel nostro banco
Qwen dichiarava 0,95 su tutte e 426 le chiamate, comprese *"gera"* e *"notte
faggine"*. Una soglia costruita sopra una confidence non calibrata non controlla
niente.

## Limiti

- **E' cloud.** Se la WAN cade, non c'e'. In Jarvis e' per questo che
  [[llama-router-llm|Qwen locale]] resta l'ultimo anello e non si disattiva.
- **Non genera.** Se serve *scrivere* qualcosa — una query di ricerca, il titolo
  di un brano — sceglie e basta.
- **E' a consumo**, quindi ogni domanda in piu' e' una voce di costo, non solo di
  latenza.
"""

p = WIKI / "entities/jev-service.md"
if not p.exists():
    p.write_text(JEV, encoding="utf-8")
    changed.append("entities/jev-service.md")
    print("  + entities/jev-service.md creata")
else:
    print("  = entities/jev-service.md gia' presente")


# ────────────────────────────────────────── 2. comparisons/jev-vs-gliner.md

CMP = f"""---
title: Jev contro GLiNER — quando usare quale
created: {TODAY}
updated: {TODAY}
type: comparison
tags: [jarvis, llm, classification, benchmark, comparison, latency, cloud, local]
sources: []
---

# Jev contro GLiNER — quando usare quale

Due modi di rispondere a domande tipizzate senza generare testo:
[[jev-service|Jev]] (cloud, System One) e [[gliner-service|GLiNER]] (locale,
encoder). Non sono intercambiabili, e il confine non e' dove sembra.

## La regola corta

> **Se la risposta e' un NOME che compare nel testo → GLiNER.**
> **Se la risposta richiede di INFERIRE qualcosa → Jev.**

## Le misure

Due prove indipendenti, stesse domande a entrambi.

### Prova 1 — vocabolario chiuso di nomi propri

Banco di 426 comandi vocali reali: riconoscere quale stanza o apparecchio di casa
e' il bersaglio, fra ~150 nomi.

| | GLiNER | Jev |
|---|---|---|
| bersaglio | **84,0%** | 73,2% |
| azione | **88,6%** | 86,8% |
| intento | **90,8%** | 89,2% |
| p50 | **68 ms** | 470 ms |

GLiNER vince su tutte le voci, ed e' **7 volte piu' rapido**. I nomi sono nel
testo: e' esattamente il compito per cui un encoder che segna similarita' fra
span ed etichetta e' fatto.

### Prova 2 — inferenza

Due casi d'uso di terzi, etichette nude, nessun trucco da nessuna delle due parti.

**a. Livello di budget** da *"My husband and I are packing our bags for our 10th
anniversary trip. **Money is no object.**"* → dovrebbe essere `luxury`.

| | esito su 8 frasi |
|---|---|
| GLiNER, etichette nude | 2/6 |
| GLiNER, con descrizioni | **1/6** (peggiora) |
| GLiNER, riformulato in binario | 4/8 |
| GLiNER, a due passi | 4/8 |
| **Jev, etichette nude** | **8/8**, confidenze 0,96-1,00 |

**b. Anni di esperienza** da un curriculum con *"Jan 2022 - Present"* e
`today = September 15, 2026` → 4,7 anni.

| | risposta |
|---|---|
| GLiNER | `10+ years`, confidence **0,2586** |
| **Jev** (`score`) | **2.02** su una scala dove `2 = "4 years"`, confidence 0,99 |

## Perche'

GLiNER segna la **similarita' fra l'embedding di uno span e quello di
un'etichetta**. Non ha:

- **la negazione** — *"money is **no** object"* somiglia lessicalmente a `cheap`
  (contiene "money"), e aggiungere descrizioni peggiora perche' aumenta la
  sovrapposizione di parole;
- **l'aritmetica** — sottrarre due date non e' una somiglianza;
- **la sintassi** — *"accendi la luce della cucina"* e *"quali luci sono accese in
  cucina?"* hanno le stesse parole e cambiano nel modo del verbo (vedi finding 8
  in [[gliner-service]]).

Quando non ce la fa **lo dice**: la confidence crolla (0,2586 con sei opzioni,
dove il caso e' 0,167). E' un segnale utilizzabile — va usato come soglia, non
ignorato.

## Tabella di scelta

| il compito e'… | usa | perche' |
|---|---|---|
| scegliere fra nomi che compaiono nel testo | **GLiNER** | 84,0% contro 73,2%, e 68ms |
| estrarre entita' concrete (persone, luoghi, date) | **GLiNER** | 0,997 di confidence |
| vocabolari grandi (100+ voci) che cambiano a runtime | **GLiNER** | zero-shot, nessun costo per etichetta |
| volumi alti, latenza bassa, nessuna WAN | **GLiNER** | locale, ~70ms |
| capire un idioma, una negazione, un'implicatura | **Jev** | 8/8 dove GLiNER fa 2-4/8 |
| calcolare qualcosa (date, quantita', confronti) | **Jev** | `score` continuo, 0,99 |
| serve una confidence **su cui mettere una soglia** | **Jev** | e' calibrata |
| serve una posizione su una scala ordinata | **Jev** | `score` non ha equivalente |
| generare testo libero | **nessuno dei due** | serve un generativo, vedi [[llama-router-llm]] |

## Insieme

Nel router di Jarvis stanno in catena, non in alternativa: GLiNER decide, e chi
passa la mano finisce al prossimo. Vedi [[jarvis-orchestrator]].

E si combinano anche dentro un singolo compito: **GLiNER estrae** cio' che e'
nominabile — le date di un curriculum, a 0,997 — e **il codice o Jev** fanno il
ragionamento sopra. E' lo stesso schema con cui in Jarvis la coreferenza
(*"ora spegnila"*) e' risolta in codice: dichiarata come classe di GLiNER fa
**0/34**, riusando l'ultima azione eseguita fa **7/7** in 0,08 ms.
"""

p = WIKI / "comparisons/jev-vs-gliner.md"
if not p.exists():
    p.write_text(CMP, encoding="utf-8")
    changed.append("comparisons/jev-vs-gliner.md")
    print("  + comparisons/jev-vs-gliner.md creata")
else:
    print("  = comparisons/jev-vs-gliner.md gia' presente")


# ──────────────────────────── 3. gliner-service: correggere "cosa fa bene"

p = WIKI / "entities/gliner-service.md"
s = p.read_text(encoding="utf-8")
MARK = "vocabolario chiuso contro"   # nel testo c'e' un a capo dopo "contro"
if MARK not in s:
    vecchio = """La regola che se ne ricava: **piu' l'etichetta e' concreta e nominabile nel testo,
meglio va**. Una distinzione che vive nel *modo* in cui una cosa e' detta — e non in
*quali* parole ci sono — non la vede. Vedi il finding 8."""
    nuovo = """La regola non e' "concreto contro astratto" ma **vocabolario chiuso contro
inferenza**: se la risposta e' un **nome che compare nel testo**, GLiNER e' la scelta
giusta e batte le alternative; se richiede di **inferire** qualcosa, non ce la fa.

Misurato sugli stessi casi dati anche a [[jev-service|Jev]]:

| compito | GLiNER | Jev |
|---|---|---|
| bersaglio fra ~150 nomi di casa | **84,0%** | 73,2% |
| `budget_level` da *"Money is no object"* | 2/6 nude, 1/6 con descrizioni, 4/8 riformulato | **8/8** |
| anni di esperienza da date su un curriculum | `10+ years`, conf **0,2586** | `score` 2.02 → corretto, conf 0,99 |

Cio' che manca non e' "comprensione" in generale, sono tre cose precise: la
**negazione** (*"money is **no** object"* somiglia a `cheap` perche' contiene
"money", e le descrizioni peggiorano perche' aumentano la sovrapposizione),
l'**aritmetica**, e la **sintassi** (vedi finding 8).

Quando non ce la fa **lo dice**: la confidence crolla — 0,2586 con sei opzioni,
dove il caso e' 0,167. Usarla come soglia, non ignorarla.

**Quale scegliere per un compito dato: [[jev-vs-gliner]].**"""
    if vecchio in s:
        s = s.replace(vecchio, nuovo).replace(f"updated: {TODAY}", f"updated: {TODAY}", 1)
        p.write_text(s, encoding="utf-8")
        changed.append("entities/gliner-service.md")
        print("  ~ entities/gliner-service.md corretta")
    else:
        print("  ! entities/gliner-service.md: testo atteso non trovato, salto")
else:
    print("  = entities/gliner-service.md gia' corretta")


# ──────────────────────────────────────────────────────────── 4. index.md

p = WIKI / "index.md"
s = p.read_text(encoding="utf-8")
mod = False
if "[[jev-service]]" not in s:
    anc = "- [[gliner-service]] — GLiNER come componente riusabile su Tailscale (API + finding)"
    if anc in s:
        s = s.replace(anc, anc + "\n- [[jev-service]] — Jev come componente riusabile (primitive, contratto, limiti)")
        mod = True
if "[[jev-vs-gliner]]" not in s:
    s = s.replace("## Comparisons\n(none yet)",
                  "## Comparisons\n- [[jev-vs-gliner]] — quando usare Jev e quando GLiNER, con le misure")
    mod = True
if mod:
    p.write_text(s, encoding="utf-8")
    changed.append("index.md")
    print("  ~ index.md aggiornato")
else:
    print("  = index.md gia' aggiornato")


# ──────────────────────────────────────────────────────────── 5. log.md

p = WIKI / "log.md"
s = p.read_text(encoding="utf-8")
MARK_LOG = f"## {TODAY} — Jev contro GLiNER"
if MARK_LOG not in s:
    p.write_text(s.rstrip() + f"""

{MARK_LOG}

Due pagine nuove — [[jev-service]] e [[jev-vs-gliner]] — e una correzione a
[[gliner-service]], tutte nate da una misura invece che da un'impressione.

Presi due casi d'uso di terzi (classificare il budget di un viaggio a partire da
*"Money is no object"*, e calcolare gli anni di esperienza da un curriculum) e dati
a entrambi i modelli con le stesse etichette nude: **GLiNER 2-4 su 8, Jev 8 su 8**.
Sul curriculum, GLiNER risponde `10+ years` con confidence 0,2586 dove Jev risponde
`score` 2.02 — cioe' 4 anni su una scala ordinata, contro un valore reale di 4,7.

Il confine che avevo scritto in [[gliner-service]] — "concreto contro astratto" —
era impreciso. Quello giusto e' **vocabolario chiuso contro inferenza**: se la
risposta e' un nome che compare nel testo GLiNER vince (84,0% contro 73,2% sul
banco dei 426, a 68ms contro 470), se richiede di inferire — una negazione, una
sottrazione fra date, un'implicatura — non ce la fa, e non e' questione di
formulazione: con le descrizioni **peggiora**, perche' aumentano la sovrapposizione
lessicale con l'etichetta sbagliata.

Vale la pena notare che quando non ce la fa **lo dichiara**: la confidence crolla a
0,2586 su sei opzioni, dove il caso e' 0,167. E' un segnale usabile come soglia.
""", encoding="utf-8")
    changed.append("log.md")
    print("  ~ log.md appeso")
else:
    print("  = log.md gia' aggiornato")

print()
print(f"Modificati {len(changed)} file: " + ", ".join(changed) if changed
      else "Niente da fare.")
