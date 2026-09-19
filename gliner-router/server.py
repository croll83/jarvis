"""Servizio di scoring GLiNER per il router dell'orchestrator.

Deliberatamente STUPIDO: non conosce la casa, non conosce gli intent, non
decide niente. Riceve le etichette e restituisce i punteggi. Tutta la politica
— quali intent esistono, quali azioni, come si risolve un bersaglio — vive in
`router_model.py` nell'orchestrator, che resta la fonte unica.

Gira come servizio a se' perche' il container dell'orchestrator ha torch CPU e
niente GPU, e su CPU il bersaglio costa 1370ms contro i 34ms in GPU.
"""
import logging, os, time
from functools import lru_cache
from typing import Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gliner-router")

MODELLO = os.getenv("GLINER_MODEL", "fastino/gliner2.5-multi-v1")
DEVICE = os.getenv("GLINER_DEVICE", "cuda")

app = FastAPI(title="GLiNER router", version="1")
_clf = None
_CFG_VINCOLI = None
_CFG_LARGO = None


@app.on_event("startup")
def carica():
    global _clf, _CFG_VINCOLI, _CFG_LARGO
    from gliner2.classification import Classifier, ClassificationConfig
    t0 = time.time()
    # fp16. MISURATO: il processo occupa ~1,65 GiB di VRAM — 574 MiB di pesi
    # (287M parametri) piu' il contesto CUDA e gli spazi di lavoro. `quantize=True`
    # non cambia nulla su questo percorso, l'ho verificato. Sulla 5070 da 8GB ci
    # sta insieme al router generativo (4,7 GiB): 6,4 su 7,7 disponibili.
    _clf = Classifier.from_pretrained(MODELLO, map_location=DEVICE, dtype="float16")
    # decodifica esatta per l'intento: lo spazio e' 6x2x2, si esplora tutto
    _CFG_VINCOLI = ClassificationConfig(decoder="exact", beam_size=32,
                                        on_infeasible="relax", include_confidence=True)
    # il bersaglio ha 100-150 etichette: serve alzare i tetti sui candidati,
    # altrimenti max_candidates_per_task=64 ne taglia via un terzo
    _CFG_LARGO = ClassificationConfig(decoder="beam", beam_size=16,
                                      max_candidates_per_task=256,
                                      candidate_threshold=0.02, include_confidence=True)
    logger.info(f"modello {MODELLO} su {DEVICE} in {time.time()-t0:.1f}s")


class Ancore(BaseModel):
    """Le etichette a cui si agganciano i vincoli, passate per nome: il
    servizio non sa cosa sia HOME_CONTROL, glielo si dice."""
    home: str
    simple: str
    agent: str
    comando: str
    domanda: str
    casa: str
    fuori: str


class Richiesta(BaseModel):
    text: str
    intent_labels: List[str]
    natura_labels: List[str]
    argomento_labels: List[str]
    ancore: Ancore
    bersaglio_labels: Optional[List[str]] = None
    azione_labels: Optional[Dict[str, str]] = None
    # Domande in piu', ognuna a scelta singola: {nome: {etichetta: descrizione}}.
    # Generiche di proposito — il servizio non sa cosa siano `api_call` o
    # `measure`, sa solo che sono insiemi chiusi da soppesare.
    extra: Optional[Dict[str, Dict[str, str]]] = None


@lru_cache(maxsize=32)
def _schema_intento(intent: tuple, nat: tuple, arg: tuple, anc: tuple):
    from gliner2.classification import ClassificationSchema
    from gliner2.classification import constraints as C
    home, simple, agent, comando, domanda, casa, fuori = anc
    return (ClassificationSchema()
            .single("intent", list(intent))
            .single("natura", list(nat))
            .single("argomento", list(arg))
            .constrain(
                # HOME_CONTROL se e solo se e' un comando E riguarda la casa:
                # bidirezionale, quindi vieta anche il contrario
                C.iff(("intent", home), C.all_of(("natura", comando), ("argomento", casa))),
                C.implies(("intent", simple), ("natura", domanda)),
                C.implies(("intent", agent), ("argomento", fuori)),
            ))


@lru_cache(maxsize=16)
def _schema_uno(nome: str, etichette: tuple):
    from gliner2.classification import ClassificationSchema
    return ClassificationSchema().single(nome, list(etichette))


@lru_cache(maxsize=16)
def _schema_descritto(nome: str, coppie: tuple):
    from gliner2.classification import ClassificationSchema
    return ClassificationSchema().single(nome, dict(coppie))


@app.post("/route")
def route(r: Richiesta):
    out, t0 = {}, time.perf_counter()
    anc = (r.ancore.home, r.ancore.simple, r.ancore.agent, r.ancore.comando,
           r.ancore.domanda, r.ancore.casa, r.ancore.fuori)
    ri = _clf.classify(r.text, _schema_intento(tuple(r.intent_labels), tuple(r.natura_labels),
                                              tuple(r.argomento_labels), anc),
                       config=_CFG_VINCOLI).to_dict()
    out["intent"] = {"value": ri["intent"]["value"], "confidence": ri["intent"]["confidence"]}
    out["natura"] = {"value": ri["natura"]["value"], "confidence": ri["natura"]["confidence"]}
    t_int = (time.perf_counter() - t0) * 1000

    t = time.perf_counter()
    if r.bersaglio_labels:
        res = _clf.classify(r.text, _schema_uno("bersaglio", tuple(r.bersaglio_labels)),
                            config=_CFG_LARGO)
        d = res.to_dict()["bersaglio"]
        # le probabilita' servono all'orchestrator per ripescare un candidato
        # quando il bersaglio scelto non sta nella stanza nominata nel testo
        out["bersaglio"] = {"value": d["value"], "confidence": d["confidence"],
                            "probabilities": res.probabilities("bersaglio")}
    t_ber = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    if r.azione_labels:
        # ATTENZIONE: NON ordinare. L'ordine delle etichette nel prompt cambia
        # il risultato (encoder singolo: finiscono nello stesso prompt del
        # testo). Con `sorted()` come chiave di cache l'azione e' scesa da 86,8%
        # a 81,9% sul banco — l'ordine arriva dal chiamante ed e' quello
        # misurato, va conservato.
        d = _clf.classify(r.text, _schema_descritto("action", tuple(r.azione_labels.items())),
                          config=_CFG_LARGO).to_dict()["action"]
        out["azione"] = {"value": d["value"], "confidence": d["confidence"]}
    t_az = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    for nome, etichette in (r.extra or {}).items():
        if not etichette:
            continue
        # NON ordinare le etichette: l'ordine nel prompt cambia il risultato
        d = _clf.classify(r.text, _schema_descritto(nome, tuple(etichette.items())),
                          config=_CFG_LARGO).to_dict()[nome]
        out[nome] = {"value": d["value"], "confidence": d["confidence"]}
    t_ex = (time.perf_counter() - t) * 1000

    out["ms"] = {"intento": round(t_int), "bersaglio": round(t_ber), "azione": round(t_az),
                 "extra": round(t_ex), "totale": round((time.perf_counter() - t0) * 1000)}
    return out


class Generica(BaseModel):
    """Classificazione generica: task arbitrari, nessun vincolo cablato.

    `/route` esiste per il router di Jarvis e ha i suoi vincoli dentro
    (`intent ⇔ natura ∧ argomento`): un consumer che passa etichette proprie se
    li ritroverebbe applicati alla propria semantica, e otterrebbe sempre la
    prima etichetta. Questo endpoint non assume niente.
    """
    text: str
    # {nome_task: {etichetta: descrizione}} — la descrizione puo' essere vuota
    tasks: Dict[str, Dict[str, str]]
    # vincoli opzionali, espressi in forma dichiarativa:
    #   {"tipo": "implies"|"iff"|"excludes", "a": [task, etichetta], "b": [task, etichetta]}
    vincoli: Optional[List[dict]] = None
    entita: Optional[Dict[str, str]] = None     # {tipo: descrizione} per la NER
    probabilita: bool = False


@app.post("/classify")
def classify(r: Generica):
    """Classificazione zero-shot su task arbitrari. Vedi [[gliner-service]]."""
    from gliner2.classification import ClassificationSchema
    from gliner2.classification import constraints as C

    schema = ClassificationSchema()
    for nome, etichette in r.tasks.items():
        # NON ordinare le etichette: l'ordine nel prompt cambia il risultato
        schema = schema.single(nome, dict(etichette))
    _OPS = {"implies": C.implies, "iff": C.iff, "excludes": C.excludes}
    if r.vincoli:
        espressioni = []
        for v in r.vincoli:
            op = _OPS.get(v.get("tipo"))
            if not op:
                continue
            espressioni.append(op(tuple(v["a"]), tuple(v["b"])))
        if espressioni:
            schema = schema.constrain(*espressioni)

    t0 = time.perf_counter()
    res = _clf.classify(r.text, schema, config=_CFG_LARGO)
    d = res.to_dict()
    out = {}
    for nome in r.tasks:
        voce = {"value": d[nome]["value"], "confidence": d[nome]["confidence"]}
        if r.probabilita:
            voce["probabilities"] = res.probabilities(nome)
        out[nome] = voce
    if r.entita:
        out["entita"] = _estrattore().extract_entities(r.text, dict(r.entita),
                                                       include_confidence=True)["entities"]
    out["ms"] = round((time.perf_counter() - t0) * 1000)
    return out


_ext = None

def _estrattore():
    """L'estrattore si carica solo se qualcuno chiede entita': e' un secondo
    modello in VRAM e la maggior parte dei consumer non ne ha bisogno."""
    global _ext
    if _ext is None:
        from gliner2 import AutoExtractor
        logger.info("carico l'estrattore per la NER")
        _ext = AutoExtractor.from_pretrained(MODELLO, map_location=DEVICE, quantize=True)
    return _ext


@app.get("/health")
def health():
    return {"ok": _clf is not None, "model": MODELLO, "device": DEVICE}
