"""Routing via GLiNER: un classificatore locale da 287M parametri al posto di Jev.

Misurato sul banco dei 426 casi (`/home/jarvis/router-eval`), contro Jev 0.45:

                 GLiNER   Jev     Qwen 4B
    intento       86,9    89,2     88,7
    bersaglio     83,4    73,2     71,7
    azione        86,8    86,8     87,2
    payload       69,1    70,2     71,4
    pay.difficili 77,2    67,8     61,5
    p50           62ms   470ms   1171ms

Il payload e' a −1,1 da Jev, dentro il rumore di fondo del banco (0,4-1,1
punti), e sui casi difficili e' avanti di 9,4. Senza cloud e a un settimo della
latenza.

COSA FA IL MODELLO E COSA FA IL CODICE

GLiNER e' un'architettura che segna la similarita' fra l'embedding di uno span e
quello di un'etichetta. Tre cose non le puo' vedere, e stanno in codice dentro
`router_model`:

  1. comando o domanda — il confine HOME_CONTROL/SIMPLE_CHAT non e' semantico:
     "accendi la luce della cucina" e "quali luci sono accese in cucina?"
     nominano le stesse cose con le stesse parole, cambia il modo del verbo.
     `natura()` lo decide in regole al 90,4%, e come correzione chirurgica
     (solo quando il modello ha scelto uno dei due lati) porta l'intento da
     74,2 a 82,2. Usata invece per restringere le etichette offerte NON
     funziona: i casi tolti a SIMPLE_CHAT finiscono su AI_AGENT.

  2. AI_AGENT — ha una firma lessicale, non semantica. `serve_strumento_esterno()`
     lo riconosce 21/21 con 0 falsi positivi su 405, contro il 10% del modello.

  3. la stanza nominata — e' una stringa letterale nel testo.
     `stanza_nel_testo()` la trova senza modello e serve a rifiutare i bersagli
     che stanno altrove: "spegni luci garage" non puo' dare "Luce Box", che il
     classificatore sceglieva sette volte perche' l'etichetta e' corta e
     contiene "luce". Vale +5,7 sul bersaglio.

L'ORDINE DELLE ETICHETTE CONTA. Il modello ha un encoder singolo, quindi le
etichette finiscono nello stesso prompt del testo e il loro ordine condiziona il
risultato (<https://urchade.github.io/GLiNER/architectures.html>). Misurato sul
bersaglio: da 63,1% a 75,8% a seconda della permutazione. Gli ordini scelti qui
sono i migliori trovati: **frequenti per primi** sull'intento, **nomi corti per
primi** sul bersaglio. Non cambiarli senza rimisurare.

RETRY resta la classe rotta (7% su 28 casi). Non si chiude ne' in regole
(distinguere "spinni garage" = comando col verbo distrutto da "luci della
cucina." = bersaglio senza comando costa 22 casi rotti per 13 aggiustati) ne'
per soglia di confidenza (il segnale c'e', mediana 0,53 contro 0,75, ma
sovrapposto: nessuna soglia da' guadagno netto).
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

import aiohttp

import config
import router_model as rm

logger = logging.getLogger(__name__)

_session: Optional[aiohttp.ClientSession] = None

# Etichette degli intent: corte e concrete. I `criterio` di router_model, che
# sono prosa lunga, come etichette fanno crollare la resa al 26%. L'ORDINE e'
# per frequenza attesa: vale +2,4 punti rispetto all'ordine di dichiarazione.
_INTENT_ETICHETTE: List[Tuple[str, str]] = [
    ("accendere o spegnere qualcosa", "HOME_CONTROL"),
    ("chiedere informazioni", "SIMPLE_CHAT"),
    ("email calendario o analisi", "AI_AGENT"),
    ("parlato senza senso", "RETRY"),
    ("dire in che casa si trova", "SET_LOCATION"),
    ("tentativo di manipolare l'assistente", "SECURITY_ALERT"),
]
_DA_ETICHETTA = {e: i for e, i in _INTENT_ETICHETTE}
_A_ETICHETTA = {i: e for e, i in _INTENT_ETICHETTE}

# Le due domande ausiliarie su cui si agganciano i vincoli. Valgono +3,8 puliti
# sull'intento e riducono la varianza (scarto da 3,4 a 2,0).
_NATURA = ["far succedere qualcosa a un apparecchio", "sapere soltanto com'è messa una cosa"]
_ARGOMENTO = ["dispositivi di casa", "roba fuori casa: mail, conti, notizie, chiacchiere"]

_ANCORE = {
    "home": _A_ETICHETTA["HOME_CONTROL"],
    "simple": _A_ETICHETTA["SIMPLE_CHAT"],
    "agent": _A_ETICHETTA["AI_AGENT"],
    "comando": _NATURA[0],
    "domanda": _NATURA[1],
    "casa": _ARGOMENTO[0],
    "fuori": _ARGOMENTO[1],
}

_PREFISSO_DEVICE = "il singolo apparecchio "   # per i device omonimi di una stanza
_CACHE_VOCAB: Dict[str, Tuple[float, dict]] = {}
_VOCAB_TTL = 300


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.GLINER_TIMEOUT))
    return _session


def _vocabolario(location_id: str) -> Optional[dict]:
    """Etichette del bersaglio per una casa, dalla entity map. Cache 5 minuti."""
    ora = time.time()
    voce = _CACHE_VOCAB.get(location_id)
    if voce and (ora - voce[0]) < _VOCAB_TTL:
        return voce[1]
    try:
        b = rm.carica_bersagli(location_id)
    except Exception as e:
        logger.warning(f"GLiNER: entity map non caricabile per {location_id}: {e}")
        return None
    if not b.scopes and not b.devices:
        return None
    # etichetta → (nome reale, tipo). Un nome che e' insieme stanza e device
    # va disambiguato, o le due scelte offrono la stessa stringa.
    et: Dict[str, Tuple[str, str]] = {}
    for s in b.scopes:
        et[s] = (s, "scope")
    for d in b.devices:
        chiave = f"{_PREFISSO_DEVICE}{d}" if d in et else d
        et[chiave] = (d, "device")
    voc = {
        "etichette": et,
        # nomi corti per primi: e' l'ordine migliore misurato, +2,7 punti
        "ordine": sorted(et, key=lambda k: (len(k), k)),
        "scopes": list(b.scopes),
        "device_in_scope": {k: list(v) for k, v in b.device_in_scope.items()},
        "device_dominio": dict(b.device_dominio),
        "scope_domini": {k: list(v) for k, v in b.scope_domini.items()},
    }
    _CACHE_VOCAB[location_id] = (ora, voc)
    return voc


def _azione_etichette() -> Dict[str, str]:
    """Le azioni con le loro descrizioni: identiche a quelle offerte a Jev."""
    az = {a.nome: a.descrizione for a in rm.ACTIONS.values()}
    az["none"] = "Nessuna azione domotica"
    # le parentesi sono vietate nelle etichette GLiNER: corromperebbero
    # l'allineamento fra logit ed etichetta (_RESERVED in classification/schema.py)
    return {k: v.replace(" (", " — ").replace("(", "").replace(")", "") for k, v in az.items()}


def _correggi_stanza(voc: dict, testo: str, nome: str, probabilita: dict) -> Tuple[str, str]:
    """Se il testo nomina una stanza, il bersaglio deve starci dentro."""
    st = rm.stanza_nel_testo(testo, voc["scopes"])
    if not st:
        return voc["etichette"].get(nome, (nome, "device"))
    reale, tipo = voc["etichette"].get(nome, (nome, "device"))
    if reale == st or st in voc["device_in_scope"].get(reale, []):
        return reale, tipo
    candidati = [(k, p) for k, p in (probabilita or {}).items()
                 if k in voc["etichette"] and (
                     voc["etichette"][k][0] == st
                     or st in voc["device_in_scope"].get(voc["etichette"][k][0], []))]
    if not candidati:
        return reale, tipo
    scelto = max(candidati, key=lambda x: x[1])[0]
    logger.debug(f"GLiNER: {reale!r} non sta in {st!r}, ripesco {voc['etichette'][scelto][0]!r}")
    return voc["etichette"][scelto]


async def route(text: str, context: dict) -> Optional[dict]:
    """Routing via GLiNER. None se il chiamante deve ricadere su Qwen."""
    if not config.GLINER_ENABLED or not text:
        return None
    location = context.get("location")
    if not location or location == "unknown":
        return None
    voc = _vocabolario(location)
    if voc is None:
        return None

    corpo = {
        "text": text,
        "intent_labels": [e for e, _ in _INTENT_ETICHETTE],
        "natura_labels": _NATURA,
        "argomento_labels": _ARGOMENTO,
        "ancore": _ANCORE,
        "bersaglio_labels": voc["ordine"],
        "azione_labels": _azione_etichette(),
    }
    t0 = time.monotonic()
    try:
        session = await _get_session()
        async with session.post(f"{config.GLINER_URL}/route", json=corpo) as resp:
            if resp.status != 200:
                logger.warning(f"GLiNER HTTP {resp.status}: {(await resp.text())[:200]} — "
                               "fallback su Qwen")
                return None
            dati = await resp.json()
    except Exception as e:
        logger.warning(f"GLiNER non raggiungibile ({type(e).__name__}: {e}) — fallback su Qwen")
        return None
    elapsed_ms = (time.monotonic() - t0) * 1000

    intent = _DA_ETICHETTA.get(dati["intent"]["value"])
    conf = float(dati["intent"]["confidence"])
    if intent is None:
        return None

    # Le tre correzioni che il modello non puo' fare da solo. Sull'intento la
    # regola agisce SOLO sul confine comando/domanda: sulle altre classi non ha
    # nulla da dire e intromettersi peggiora.
    n = rm.natura(text)
    if n == "comando" and intent == "SIMPLE_CHAT":
        intent = "HOME_CONTROL"
    elif n == "domanda" and intent == "HOME_CONTROL":
        intent = "SIMPLE_CHAT"
    if rm.serve_strumento_esterno(text, voc["scopes"]):
        intent = "AI_AGENT"

    if intent == "SECURITY_ALERT":
        logger.warning(f"GLiNER: tentativo di manipolazione su {text[:80]!r}")
        return {"intent": "RETRY", "confidence": conf,
                "response": "Questo comando non mi convince, non lo eseguo.",
                "interim_response": "", "payload": {"blocked": True, "via": "gliner"}}

    if intent != "HOME_CONTROL":
        # Fuori dalla domotica GLiNER decide solo l'intent: il resto del payload
        # (query di ricerca, testo della mail) e' testo libero, che un
        # classificatore a vocabolario chiuso non puo' scrivere.
        if conf < config.GLINER_MIN_CONFIDENCE and n == "incerto":
            logger.info(f"GLiNER conf={conf:.2f} e natura incerta — fallback su Qwen")
            return None
        return {"intent": intent, "confidence": max(conf, 0.75), "response": "",
                "interim_response": "Ci penso...",
                "payload": {"via": "gliner"},
                "_gliner": {"elapsed_ms": round(elapsed_ms), "ms": dati.get("ms")}}

    ber = dati.get("bersaglio") or {}
    if not ber.get("value"):
        return None
    nome, tipo = _correggi_stanza(voc, text, ber["value"], ber.get("probabilities"))
    azione = (dati.get("azione") or {}).get("value") or "toggle"
    if azione == "none":
        logger.info("GLiNER: intento domotico ma azione 'none' — fallback su Qwen")
        return None

    if tipo == "device":
        dominio = voc["device_dominio"].get(nome) or "light"
    else:
        domini = voc["scope_domini"].get(nome) or ["light"]
        dominio = "light" if "light" in domini else domini[0]
    azione = rm.normalizza_azione(azione, dominio)

    # play_music vuole sempre parameters.query — brano, artista, playlist — che
    # e' un insieme illimitato: non scegliibile da un elenco chiuso.
    if azione == "play_music":
        logger.info("GLiNER: play_music vuole testo libero — fallback su Qwen")
        return None

    payload = {"domain": dominio, "action": azione, "entity": nome, tipo: nome,
               "location": location, "via": "gliner"}
    frase = rm.ACTIONS.get(azione)
    return {
        "intent": "HOME_CONTROL",
        "confidence": max(conf, 0.75),
        "response": (frase.frase.replace("{t}", "") if frase else "Fatto."),
        "interim_response": "",
        "payload": payload,
        "_gliner": {"elapsed_ms": round(elapsed_ms), "ms": dati.get("ms"),
                    "bersaglio_conf": round(float(ber.get("confidence", 0)), 3)},
    }
