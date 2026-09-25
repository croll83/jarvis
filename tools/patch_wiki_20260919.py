#!/usr/bin/env python3
"""Aggiorna la llm-wiki con la catena di routing GLiNER -> Jev -> Qwen 4B
(19 settembre 2026).

1. entities/gliner-router.md       — pagina nuova (entity)
2. entities/jev-router.md          — da primario a stadio OPZIONALE fra GLiNER e Qwen
3. entities/llama-router-llm.md    — Qwen 3.5 4B al posto del 2.5 7B, ultimo anello
4. concepts/jarvis-orchestrator.md — diagramma, sezione Routing, novita', updated
5. index.md                        — gliner-router sotto l'orchestrator
6. log.md                          — append dell'azione (richiesto da SCHEMA.md)

Idempotente: ogni blocco ha un marker e viene saltato se gia' presente.
Va eseguito sulla workstation (100.116.99.9), in /home/jarvis/wiki.
"""
import sys
from pathlib import Path

WIKI = Path("/home/jarvis/wiki")
TODAY = "2026-09-19"
changed = []


def leggi(rel: str) -> str:
    p = WIKI / rel
    if not p.exists():
        print(f"  ! manca {rel}, salto")
        return ""
    return p.read_text(encoding="utf-8")


def scrivi(rel: str, testo: str):
    (WIKI / rel).write_text(testo, encoding="utf-8")
    changed.append(rel)


def bump(testo: str) -> str:
    """Aggiorna la data `updated` nel frontmatter (convenzione SCHEMA.md)."""
    import re
    return re.sub(r"^updated: \d{4}-\d{2}-\d{2}$", f"updated: {TODAY}", testo,
                  count=1, flags=re.M)


# ────────────────────────────────────────────── 1. entities/gliner-router.md

GLINER_PAGE = f"""---
title: GLiNER Router (classificatore locale)
created: {TODAY}
updated: {TODAY}
type: entity
tags: [jarvis, orchestrator, routing, llm, local, latency, gpu, benchmark]
sources: []
---

# GLiNER Router (classificatore locale)

Decisore **primario** dell'[[fastapi-orchestrator|orchestrator]] dal 19 settembre 2026.
Prende il posto di [[jev-router|Jev]] in testa alla catena; Jev resta come stadio
**opzionale** e [[llama-router-llm|Qwen]] resta l'ultimo anello.

## Cos'e'

`fastino/gliner2.5-multi-v1`: encoder **mDeBERTa-v3**, 287M parametri. Non genera testo:
segna la **similarita' fra l'embedding di uno span e quello di un'etichetta**, e sceglie
da elenchi chiusi.

Gira come servizio a se' — `gliner-router.service`, porta **11436**, systemd sull'host e
**non** in Docker. Il motivo e' misurato: il container ha `torch` **CPU**, e su CPU il
bersaglio costa **1370ms** contro **34ms** in GPU. Occupa ~1,65 GiB di VRAM (574 MiB di
pesi piu' contesto CUDA), che sulla 5070 da 8 GB stanno insieme al router generativo.

Il servizio e' **deliberatamente stupido**: riceve etichette, restituisce punteggi. Non sa
cosa sia `HOME_CONTROL`. Tutta la politica vive in `router_model.py`.

## Le misure

Banco condiviso di **426 casi reali** (fuori dal repo pubblico: contiene comandi di casa e
nomi di persona). Rumore di fondo **0,4-1,1 punti**, errore di etichettatura ~7%.

| | GLiNER | [[jev-router|Jev]] | [[llama-router-llm|Qwen 4B]] |
|---|---|---|---|
| intento | **90,8%** | 89,2% | 88,7% |
| bersaglio | **84,0%** | 73,2% | 71,7% |
| azione | **88,6%** | 86,8% | 87,2% |
| payload | **74,5%** | 66,4% | 71,4% |
| p50 | **68 ms** | 470 ms | 1171 ms |

> **Attenzione ai denominatori.** Il payload di Jev fa 70,2% su tutti i 426 — i non-domotici
> contano come corretti perche' non c'e' payload da sbagliare — ma **66,4% sui 265 che un
> payload ce l'hanno**. Confrontare i due numeri fa sembrare vantaggi che non esistono.

## Cosa ha insegnato

**I vocabolari chiusi battono l'estrazione.** Risolvere il bersaglio classificando sul
vocabolario della casa fa **84,0%**; estrarre uno span e agganciarlo col fuzzy ne fa 49,2%.

**L'ORDINE delle etichette conta.** Encoder singolo: le etichette finiscono nello stesso
prompt del testo, quindi il loro ordine condiziona il risultato. Sul bersaglio si va da
**63,1% a 75,8%** per sola permutazione. Gli ordini in uso — *frequenti per primi*
sull'intento, *nomi corti per primi* sul bersaglio — sono i migliori misurati, e toccarli
senza rimisurare e' un errore. Un bug in produzione lo ha confermato: una chiave di cache
con `sorted()` riordinava le etichette e l'azione scendeva da 86,8% a 81,9%.

**Alcune cose un classificatore non le puo' vedere.** Il confine fra comando e domanda non
e' semantico: *"accendi la luce della cucina"* e *"quali luci sono accese in cucina?"*
nominano le stesse cose con le stesse parole e cambiano solo nel **modo del verbo**. Per una
similarita' fra embedding sono quasi identiche. Quelle regole stanno in codice dentro
`router_model.py`: `natura()` le riconosce al **90,4%**, e usata come correzione chirurgica
porta l'intento da 74,2% a 82,2%.

Stessa storia per `AI_AGENT` (firma **lessicale**: 21/21 con 0 falsi positivi su 405, contro
il 10% del modello) e per la coreferenza — *"ora spegnila"* — che dichiarata come classe fa
**0/34** e in codice fa **7/7** in 0,08ms, risolta dall'ultima azione **eseguita**.

## Dove si agisce

`jarvis-orchestrator/ROUTING.md` e' la mappa: quale file per cambiare un intento, un'azione,
un dominio, una regola di lingua o un'etichetta. La regola generale e' che tutto cio' che e'
**enumerabile** si dichiara in `router_model.py`, una volta, e lo leggono tutti e tre gli
stadi; stanze e dispositivi non si scrivono affatto, vengono dalla entity map di
[[home-assistant-domotica|Home Assistant]] a runtime.

Vedi anche [[jarvis-orchestrator]] per la catena completa.
"""

if not (WIKI / "entities/gliner-router.md").exists():
    scrivi("entities/gliner-router.md", GLINER_PAGE)
    print("  + entities/gliner-router.md creata")
else:
    print("  = entities/gliner-router.md gia' presente")


# ───────────────────────────────────────── 2. jev-router: da primario a opzionale

MARK_JEV = "<!-- patch-20260919-catena -->"
jev = leggi("entities/jev-router.md")
if jev and MARK_JEV not in jev:
    vecchio = """Router **primario** dell'[[fastapi-orchestrator|orchestrator]] da settembre 2026. Sostituisce
[[llama-router-llm|Qwen locale]] come primo stadio; Qwen resta come **fallback su ogni percorso
di errore** e non e' stato rimosso."""
    nuovo = f"""{MARK_JEV}
Stadio **opzionale** dell'[[fastapi-orchestrator|orchestrator]]. Dal 17 settembre 2026 era il
router primario; dal **19 settembre** il primo stadio e' [[gliner-router|GLiNER]], locale e
~7 volte piu' rapido, e Jev sta **fra GLiNER e [[llama-router-llm|Qwen]]**.

Entra in catena solo se `JEV_API_KEY` e' valorizzata. Sul banco dei 426 casi fa **66,4%** di
payload contro il **74,5%** di GLiNER, a 470ms contro 68ms — ed e' cloud, quindi a consumo.
Resta documentato e supportato: la scelta e' configurazione, non codice."""
    if vecchio in jev:
        scrivi("entities/jev-router.md", bump(jev.replace(vecchio, nuovo)))
        print("  ~ entities/jev-router.md aggiornata")
    else:
        print("  ! entities/jev-router.md: testo atteso non trovato, salto")
else:
    print("  = entities/jev-router.md gia' aggiornata")


# ──────────────────────────────── 3. llama-router-llm: 4B, ultimo anello

MARK_LLAMA = "<!-- patch-20260919-4b -->"
llama = leggi("entities/llama-router-llm.md")
if llama and MARK_LLAMA not in llama:
    vecchio = """> **Da settembre 2026 non e' piu' il primo stadio.** Il router primario e' [[jev-router|Jev]], e
> questo server subentra quando Jev e' irraggiungibile, poco sicuro, o quando serve **generare**
> testo libero — cosa che Jev per costruzione non fa. Resta installato di proposito: Jev e'
> cloud-only, e `HOME_CONTROL` deve funzionare anche senza WAN."""
    nuovo = f"""{MARK_LLAMA}
> **E' l'ULTIMO anello della catena, e non si disattiva.** Dal 19 settembre 2026 davanti a lui
> ci sono [[gliner-router|GLiNER]] (locale, ~68ms) e opzionalmente [[jev-router|Jev]] (cloud).
> Subentra quando quelli passano la mano — serve uno slot di **testo libero**, il comando vale
> per tutta la casa, la confidenza e' bassa — o quando non rispondono. Non si toglie per
> costruzione: e' il solo stadio che sa **generare**, e `HOME_CONTROL` deve funzionare anche
> senza WAN.

> **Il modello e' cambiato: Qwen 3.5 4B Q6_K** (unsloth) al posto del 2.5 7B. Misurato meglio
> sullo stesso banco di 426 casi — payload **71,4%** contro 68,3% — e libera **2,2 GB** di VRAM,
> che e' cio' che ha fatto spazio a GLiNER sulla stessa scheda.
>
> ⚠️ **E' un modello con reasoning**, e questo ha una conseguenza operativa che e' costata due
> giri di debug: spende TUTTI i token consentiti in catena di pensiero e restituisce `content`
> **vuoto**. Misurato sul prompt di `_phrase_ha_data` con `max_tokens=200`: senza il flag 200
> token e `finish_reason=length` con contenuto vuoto, con il flag 12 token e la risposta giusta.
> Su prompt **corti** il contenuto sopravvive, quindi il difetto si vede solo sulle risposte
> lunghe. Il flag vive in `config.ROUTER_CHAT_TEMPLATE_KWARGS` e va passato in **tutti e quattro**
> i punti che chiamano il router: `_llm_chat`, la chiamata di routing, il preprocess del TTS e il
> tool calling. Averlo messo solo nel routing ha lasciato rotte tutte le risposte parlate lunghe.
> **Quando si cambia il modello del router va riprovata l'intera catena generativa.**"""
    if vecchio in llama:
        t = llama.replace(vecchio, nuovo)
        t = t.replace("Qwen 2.5 7B Q6_K", "Qwen 3.5 4B Q6_K").replace("qwen2.5_7b-q6_K", "Qwen3.5-4B-Q6_K")
        scrivi("entities/llama-router-llm.md", bump(t))
        print("  ~ entities/llama-router-llm.md aggiornata")
    else:
        print("  ! entities/llama-router-llm.md: testo atteso non trovato, salto")
else:
    print("  = entities/llama-router-llm.md gia' aggiornata")


# ────────────────────────────────────── 4. concepts/jarvis-orchestrator.md

MARK_ORCH = "<!-- patch-20260919-routing -->"
orch = leggi("concepts/jarvis-orchestrator.md")
if orch and MARK_ORCH not in orch:
    t = orch
    # diagramma dei componenti
    t = t.replace(
        'ROUTE["Router 2 stadi<br/>1. [[jev-router|Jev]] (cloud, ~280ms)<br/>2. [[llama-router-llm|Qwen 2.5 7B Q6_K]] (fallback)"]',
        'ROUTE["Catena di routing<br/>1. [[gliner-router|GLiNER]] (locale, ~68ms)<br/>2. [[jev-router|Jev]] (cloud, opzionale)<br/>3. [[llama-router-llm|Qwen 3.5 4B]] (ultimo anello)"]')
    # sezione Routing
    vecchia_sezione = """### Routing (2 stadi, da settembre 2026)"""
    nuova_sezione = f"""{MARK_ORCH}
### Routing (catena configurabile, da settembre 2026)

**La catena e' configurazione, non codice.** Un decisore entra in catena se e' **configurato**:
`GLINER_URL` valorizzato lo attiva, `JEV_API_KEY` valorizzata attiva Jev, e Qwen chiude sempre.
L'ordine vive in `config.ROUTING_CHAIN`, viene loggato all'avvio e `get_routing()` lo **percorre**
invece di ricostruirlo con una scala di `if`.

| configurazione | catena |
|---|---|
| `GLINER_URL` + `JEV_API_KEY` | GLiNER → Jev → Qwen |
| solo `GLINER_URL` | GLiNER → Qwen |
| solo `JEV_API_KEY` | Jev → Qwen |
| nessuno dei due | Qwen |

- **[[gliner-router|GLiNER]] — stadio primario** (dal 19 settembre 2026). Classificatore locale
  su vocabolari chiusi, p50 **68ms**. Sul banco dei 426 casi: intento 90,8%, bersaglio 84,0%,
  payload 74,5%.
- **[[jev-router|Jev]] — stadio opzionale**, cloud a consumo. Payload 66,4% a 470ms.
- **[[llama-router-llm|Qwen 3.5 4B Q6_K]] — ultimo anello**, mai disattivabile: e' il solo che
  sa **generare** testo libero (query di ricerca, titolo di un brano, testo di una mail).

**`router_model.py` e' la fonte unica.** Intenti, azioni, domini, grandezze e fonti si dichiarano
una volta e li leggono tutti e tre gli stadi. Prima stavano in due posti — il prompt di Qwen e
`jev_engine` — con parole diverse, e si contraddicevano: il prompt mandava il meteo a
`SIMPLE_CHAT`, Jev lo mandava ad `AI_AGENT`. Stanze e dispositivi non si scrivono affatto:
vengono dalla entity map di [[home-assistant-domotica|Home Assistant]] a runtime.

**Alcune regole stanno in codice, non nel modello**, perche' sono **sintattiche** e un
classificatore a similarita' di embedding non le vede. La mappa completa di dove si agisce e' in
`jarvis-orchestrator/ROUTING.md`.

### Il vecchio schema a 2 stadi"""
    if vecchia_sezione in t:
        t = t.replace(vecchia_sezione, nuova_sezione, 1)
    # diagramma di sequenza
    t = t.replace('CMD["Comando vocale<br/>(testo STT grezzo)"] --> JEV{{"[[jev-router|Jev]]<br/>1 call, domande in parallelo<br/>~280ms"}}',
                  'CMD["Comando vocale<br/>(testo STT grezzo)"] --> GLI{{"[[gliner-router|GLiNER]]<br/>vocabolari chiusi + regole<br/>~68ms"}}')
    t = t.replace('NORM --> QWEN["[[llama-router-llm|Qwen 2.5 7B Q6_K]]<br/>p50 ~1044ms"]',
                  'NORM --> QWEN["[[llama-router-llm|Qwen 3.5 4B Q6_K]]<br/>p50 ~1171ms"]')
    t = t.replace("| `SIMPLE_CHAT` | Router LLM (Qwen 7B), 1 tool call |",
                  "| `SIMPLE_CHAT` | Router LLM (Qwen 4B), 1 tool call |")
    # novita'
    t = t.replace("## Novità settembre 2026\n", f"""## Novità settembre 2026

- **Catena di routing configurabile — [[gliner-router|GLiNER]] primario** (19 settembre). Un
  decisore entra in catena se e' configurato; Qwen chiude sempre perche' e' il solo che genera
  testo libero. GLiNER fa **74,5%** di payload contro il 66,4% di Jev, a **68ms** contro 470ms,
  e senza cloud. Il bersaglio passa da 73,2% a **84,0%**.
- **`router_model.py` come fonte unica, e le regole di lingua in codice**. Cio' che e'
  enumerabile si dichiara una volta; cio' che e' **sintattico** — comando contro domanda,
  cortesia, coreferenza — sta in regole, perche' un classificatore a similarita' di embedding
  non lo vede. Misurato: la regola comando/domanda fa 90,4% e porta l'intento da 74,2 a 82,2%.
- **Qwen 3.5 4B al posto del 2.5 7B**: payload 71,4% contro 68,3% e 2,2 GB di VRAM liberati,
  che e' cio' che ha fatto spazio a GLiNER sulla stessa scheda. E' un modello **con reasoning**:
  vedi l'avvertenza in [[llama-router-llm]], perche' ha lasciato rotte per giorni tutte le
  risposte parlate lunghe.
""", 1)
    scrivi("concepts/jarvis-orchestrator.md", bump(t))
    print("  ~ concepts/jarvis-orchestrator.md aggiornata")
else:
    print("  = concepts/jarvis-orchestrator.md gia' aggiornata")


# ───────────────────────────────────────────────────────── 5. index.md

idx = leggi("index.md")
if idx and "[[gliner-router]]" not in idx:
    vecchio = "- [[jev-router]] — Router primario (Jev / TypeSafe System One, cloud)\n- [[llama-router-llm]] — Router di fallback locale (Qwen 2.5 7B Q6_K)"
    nuovo = ("- [[gliner-router]] — Decisore primario (GLiNER 2.5 multi-v1, locale, ~68ms)\n"
             "- [[jev-router]] — Decisore opzionale (Jev / TypeSafe System One, cloud)\n"
             "- [[llama-router-llm]] — Ultimo anello, mai disattivabile (Qwen 3.5 4B Q6_K)")
    if vecchio in idx:
        scrivi("index.md", idx.replace(vecchio, nuovo))
        print("  ~ index.md aggiornato")
    else:
        print("  ! index.md: testo atteso non trovato, salto")
else:
    print("  = index.md gia' aggiornato")


# ───────────────────────────────────────────────────────────── 6. log.md

LOG = f"""

## {TODAY} — Catena di routing: GLiNER primario, Jev opzionale, Qwen 4B ultimo anello

Il router dell'orchestrator passa da due stadi a una **catena configurabile**. Un decisore entra
in catena se e' **configurato**: `GLINER_URL` attiva [[gliner-router|GLiNER]], `JEV_API_KEY`
attiva [[jev-router|Jev]], e [[llama-router-llm|Qwen]] chiude sempre perche' e' il solo che sa
generare testo libero. L'ordine vive in `config.ROUTING_CHAIN` e viene loggato all'avvio.

Misurato su un banco condiviso di **426 casi reali** (rumore di fondo 0,4-1,1 punti, errore di
etichettatura ~7%), sugli stessi denominatori:

| | GLiNER | Jev | Qwen 4B |
|---|---|---|---|
| intento | 90,8% | 89,2% | 88,7% |
| bersaglio | 84,0% | 73,2% | 71,7% |
| payload | 74,5% | 66,4% | 71,4% |
| p50 | 68 ms | 470 ms | 1171 ms |

Tre cose imparate e degne di nota:

**I vocabolari chiusi battono l'estrazione** — 84,0% contro 49,2% sul bersaglio — e **l'ordine
delle etichette conta**: su un encoder singolo finiscono nello stesso prompt del testo, e la resa
va da 63,1% a 75,8% per sola permutazione. Un bug in produzione l'ha confermato: una chiave di
cache con `sorted()` faceva scendere l'azione da 86,8% a 81,9%.

**Alcune regole non appartengono a un modello.** Il confine fra comando e domanda e' sintattico,
non semantico: *"accendi la luce della cucina"* e *"quali luci sono accese in cucina?"* hanno le
stesse parole e cambiano nel modo del verbo. In regole si riconosce al 90,4%; la coreferenza
(*"ora spegnila"*) fa 0/34 come classe del modello e 7/7 in codice, in 0,08ms.

**Il modello del router e' passato a Qwen 3.5 4B** (payload 71,4% contro 68,3% del 7B, e 2,2 GB
di VRAM liberati). E' un modello **con reasoning**: spende tutti i token in catena di pensiero e
restituisce `content` vuoto. Il flag `enable_thinking=False` era stato messo solo nella chiamata
di routing, e per giorni sono rimaste rotte tutte le risposte parlate lunghe — le letture dei
sensori rispondevano "c'e' stato un problema". Ora sta in `config.ROUTER_CHAT_TEMPLATE_KWARGS` e
vale per tutti e quattro i punti che chiamano il router.

Nota di metodo: il banco misurava `payload_ok = intent_ok and entity_ok and action_ok`, e per
`SIMPLE_CHAT` entity e action sono nulli — quindi gli **87 payload di lettura etichettati non
venivano mai confrontati**. Un difetto che impediva ogni lettura dei sensori e' passato
inosservato a un banco da 426 casi.
"""

log = leggi("log.md")
if log and f"## {TODAY} — Catena di routing" not in log:
    scrivi("log.md", log.rstrip() + "\n" + LOG)
    print("  ~ log.md appeso")
else:
    print("  = log.md gia' aggiornato")


print()
if changed:
    print(f"Modificati {len(changed)} file: " + ", ".join(changed))
else:
    print("Niente da fare: la wiki era gia' aggiornata.")
