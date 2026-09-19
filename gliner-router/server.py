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
    _clf = Classifier.from_pretrained(MODELLO, map_location=DEVICE)
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
        d = _clf.classify(r.text, _schema_descritto("action", tuple(sorted(r.azione_labels.items()))),
                          config=_CFG_LARGO).to_dict()["action"]
        out["azione"] = {"value": d["value"], "confidence": d["confidence"]}
    t_az = (time.perf_counter() - t) * 1000

    out["ms"] = {"intento": round(t_int), "bersaglio": round(t_ber),
                 "azione": round(t_az), "totale": round((time.perf_counter() - t0) * 1000)}
    return out


@app.get("/health")
def health():
    return {"ok": _clf is not None, "model": MODELLO, "device": DEVICE}
