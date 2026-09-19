# Dove si agisce sul routing

Tre decisori si passano la palla: **GLiNER** (locale, ~70 ms), **Jev** (cloud,
~470 ms), **Qwen** (locale generativo, ~1200 ms). L'ordine è calcolato dalla
configurazione — vedi `.env.example`, sezione *CATENA DI ROUTING* — e viene
loggato all'avvio (`🧭 Catena di routing: ...`).

Qwen è sempre l'ultimo anello e non si disattiva: è il solo che sa **generare**
testo libero (una query di ricerca, il titolo di un brano, il testo di una
mail). Gli altri due scelgono da elenchi chiusi, e quando non se la sentono
restituiscono `None` e passano al prossimo.

## La regola generale

> Se una cosa è **enumerabile** — intenti, azioni, domini, grandezze, fonti —
> si dichiara in **`router_model.py`**, una volta, e tutti e tre i decisori la
> leggono da lì.

`router_model.py` è la fonte unica. Prima queste cose stavano scritte due volte,
nel prompt di Qwen e in `jev_engine`, con parole diverse — e si contraddicevano:
il prompt mandava il meteo a `SIMPLE_CHAT`, Jev lo mandava ad `AI_AGENT`.

## Tabella: cosa cambiare, dove

| Vuoi cambiare | File | Cosa |
|---|---|---|
| un **intento** (aggiungere, togliere, spostare il confine) | `router_model.py` | `INTENT_CRITERI` — `breve` per la prosa, `criterio` per i classificatori |
| un'**azione** (nuovo servizio HA, o i domini su cui è valida) | `router_model.py` | `ACTIONS` — nome, descrizione, frase parlata, `domini` |
| come un'azione si **adatta** a un dominio | `router_model.py` | `normalizza_azione()` e `SINONIMI_AZIONE` |
| la descrizione di un **dominio** | `router_model.py` | `DOMINI_DESCRIZIONE` |
| quali domini sono **comandabili** | `router_model.py` | `AZIONABILI`, `VOCABOLARIO_BERSAGLI` |
| le **grandezze** misurate (temperatura, consumo…) | `router_model.py` | `GRANDEZZE` |
| da dove prendere una **risposta** (casa / web / nessuna fonte) | `router_model.py` | `FONTI_RISPOSTA` e la regola `fonte_risposta()` |
| cosa richiede **testo libero** (e quindi Qwen) | `router_model.py` | `SLOT_TESTO_LIBERO` |
| **stanze e dispositivi** | *nessun file* | vengono dalla entity map di Home Assistant, a runtime (`carica_bersagli`) |

### Le regole di lingua italiana

Stanno tutte in `router_model.py`, in fondo. Sono in codice e non nel modello
perché sono **sintattiche**, e un classificatore che misura similarità fra
embedding non le vede: `"accendi la luce della cucina"` e `"quali luci sono
accese in cucina?"` hanno le stesse parole e cambiano solo nel modo del verbo.

| Regola | Funzione | Cosa decide |
|---|---|---|
| comando o domanda | `natura()` | il confine `HOME_CONTROL` / `SIMPLE_CHAT` (90,4% in regole) |
| cortesia | `_CORTESIA` dentro `natura()` | `"puoi spegnere X?"` è un comando, non una domanda |
| serve uno strumento esterno | `serve_strumento_esterno()` | `AI_AGENT` (21/21 sul banco, 0 falsi positivi su 405) |
| la stanza nominata | `stanza_nel_testo()` | rifiuta i bersagli che stanno altrove (+5,7 sul bersaglio) |
| riferimento al turno prima | `riferimento_a_turno_precedente()` | `"ora spegnila"` → ultima azione **eseguita** |
| verbo → azione | `azione_dal_verbo()` | solo per la coreferenza. **Non** usarla per l'azione in generale: misurata 77,0% contro 87,9% del modello |

## Dove agisce ogni decisore

### GLiNER — `gliner_engine.py`
Non contiene politica: prende gli elenchi da `router_model` e li **codifica**
per il modello. Quello che si tocca qui è la codifica, e va **rimisurata**:

- `_INTENT_ETICHETTE` — le etichette brevi e **il loro ordine**. Le etichette
  finiscono nello stesso prompt del testo (encoder singolo), quindi l'ordine
  cambia il risultato: sul bersaglio va da 63,1% a 75,8% secondo la
  permutazione. Qui l'ordine è *i frequenti per primi*.
- l'ordine del bersaglio — `sorted(et, key=lambda k: (len(k), k))`, *i nomi
  corti per primi*.
- `_NATURA`, `_ARGOMENTO`, `_ANCORE` — le due domande ausiliarie e i vincoli
  DSL che legano l'intento a quelle.
- `_WILDCARD` — i comandi su tutta la casa, che passano a Qwen.

Le etichette **non possono contenere parentesi** (`_RESERVED` in
`gliner2/classification/schema.py`): corromperebbero l'allineamento fra logit
ed etichetta. Per questo c'è `_pulisci()`.

Il servizio di scoring (`../gliner-router/server.py`) è deliberatamente stupido:
riceve etichette, restituisce punteggi. Non sa cosa sia `HOME_CONTROL`.

### Jev — `jev_engine.py`
`_build_questions()` costruisce le 12 domande, prendendo intenti, azioni e
domini da `router_model`. Ha in più `SECURITY_ALERT` e `_MEASURES` dichiarati
localmente — un residuo: andrebbero spostati in `router_model`.

### Qwen — il prompt
`config/router_system_prompt.txt`, caricato in `ai_engines.SYSTEM_RULES`.

⚠️ Esiste un flag `ROUTER_PROMPT_GENERATO` che dovrebbe generare il prompt da
`router_model` tramite `render_flat.py` — **ma quel file non è nel repo**. Il
flag è spento per default e c'è un `try/except` che ricade sul file statico,
quindi non fa danni, ma non può funzionare: o si porta `render_flat.py`, o si
toglie il flag.

## Cose provate e **scartate con misura**

Non riproporle senza rimisurarle: sembrano tutte ovvie.

- usare il **tipo di dispositivo nominato** (`"tapparella"` → cover) per
  scegliere il bersaglio: 83,0% → **68,3%**. In queste case `"le luci della
  cucina"` vuole lo *scope* Cucina, non un apparecchio.
- **rinunciare** quando l'azione non ha domini compatibili nel bersaglio: 25
  ricadute su 265, e 15 erano **giuste** grazie a `normalizza_azione`
  (`"apri il cancello pedonale"` → `press` su un pulsante).
- esentare dal vincolo di stanza i bersagli il cui **nome** contiene la stanza
  (`"Filtraggio Piscina"` sta in Impianti): ne aggiusta 3 e ne perde 5.
- chiedere la **fonte** al modello invece di ricavarla in codice: 42,5% contro
  90,8%.
- ricavare l'**azione dal verbo**: 77,0% contro 87,9%.
- **spezzare** il vocabolario del bersaglio in gruppi ≤30 etichette: peggiora in
  modo monotòno (20 → 62,7%, 30 → 66,2%, 50 → 70,4%, tutte insieme → 77,7%).
  Il degrado oltre 30 tipi vale per GLiNER v1, non per questa architettura.

## Due trappole che è facile ripetere

**La zona morta della confidenza.** `main.py` esegue un comando domotico solo
con `conf >= conf_high` (0,85). Sotto, il flusso cade nel ramo small-talk e
pronuncia `router_data["response"]` — che per `HOME_CONTROL` è la frase
dell'azione: **dice "Chiudo" senza chiudere**. La confidenza restituita da un
decisore deve dichiarare la *decisione*, non l'incertezza del modello.

**Il reasoning va spento in tutti i punti.** Un modello con reasoning (Qwen3.x)
spende tutti i token in catena di pensiero e restituisce `content` vuoto. Il
flag sta in `config.ROUTER_CHAT_TEMPLATE_KWARGS` e va usato in **tutti e
quattro** i punti che chiamano il router: `_llm_chat`, la chiamata di routing,
il preprocess del TTS, il tool calling. Su prompt corti il contenuto
sopravvive, quindi il difetto si vede solo sulle risposte lunghe.
**Quando si cambia il modello del router va riprovata l'intera catena
generativa, non solo il routing.**

## Misurare

Banco da 426 casi in `/home/jarvis/router-eval/` (fuori dal repo: contiene
comandi di casa reali e nomi di persona). Errore di etichettatura ~7%, quindi il
tetto misurabile è ~93% e differenze sotto il rumore di fondo (0,4-1,1 punti)
non sono interpretabili.

**Attenzione ai denominatori**: il payload di Jev fa 70,2% su tutti i 426 (i
non-`HOME_CONTROL` contano come corretti perché non c'è payload da sbagliare) ma
**66,4% sui 265 `HOME_CONTROL`**, che è il denominatore giusto per un confronto.
