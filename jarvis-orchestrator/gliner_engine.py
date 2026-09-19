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
import re
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

def _soglia_esecuzione() -> float:
    """La soglia oltre la quale main.py ESEGUE un comando domotico.

    Va letta, non indovinata: sotto quella soglia il dispatch non esegue e il
    comando finisce nel ramo small-talk, dove viene pronunciata la frase
    dell'azione ("Chiudo.") senza che nulla accada. E' successo davvero con
    "chiudi la porta del box": confidenza 0,75 contro una soglia di 0,85.

    Il contratto di questo engine e' netto: `None` significa "non me la sento,
    passa a Qwen", qualunque altra cosa significa "ho deciso". Una decisione
    presa non deve poter essere scartata in silenzio a valle.
    """
    try:
        from database import get_global_preference
        v = get_global_preference("confidence_threshold_high")
        if v:
            return float(v)
    except Exception:
        pass
    return float(getattr(config, "CONFIDENCE_THRESHOLD_HIGH", 0.85))


_PREFISSO_DEVICE = "il singolo apparecchio "   # per i device omonimi di una stanza
# "spegni tutto" punta a tutta la casa: e' un bersaglio legittimo e ha il suo
# nome nel contratto del router, ma non sta nella entity map, quindi va aggiunto
# a mano come fa Jev con _WHOLE_HOUSE.
# "spegni tutto" agisce su TUTTA la casa. Non lo gestiamo: il resolver di
# main.py non conosce "ovunque" come nome di bersaglio — avvisa "no match" e
# inventa un entity_id sintetico tipo `fan.ovunque` — e la logica giusta vive
# nel ramo A della cascata, con una guardia elaborata contro il caso "tutte le
# luci della <zona storpiata>" che non deve diventare "tutta la casa".
# Duplicarla qui significherebbe riscrivere a mano il codice piu' delicato del
# resolver per due casi su 426, sulla classe di errore piu' costosa che esiste
# (un'azione di massa su tutti i dispositivi). Si passa la mano a Qwen, che e'
# il comportamento gia' in produzione e gia' provato.
_WILDCARD = re.compile(r"\b(tutta la casa|in tutta casa|ovunque|dappertutto|"
                       r"tutto\s*$|tutti\s*$|tutte\s*$)", re.I)
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
        "scope_livello": dict(b.scope_livello),
    }
    _CACHE_VOCAB[location_id] = (ora, voc)
    return voc


def _pulisci(etichette: Dict[str, str]) -> Dict[str, str]:
    """Le parentesi sono vietate nelle etichette GLiNER: corromperebbero
    l'allineamento fra logit ed etichetta (`_RESERVED` in classification/schema.py)."""
    return {k: v.replace(" (", " — ").replace("(", "").replace(")", "")
            for k, v in etichette.items()}


def _azione_etichette() -> Dict[str, str]:
    """Le azioni con le loro descrizioni: identiche a quelle offerte a Jev."""
    az = {a.nome: a.descrizione for a in rm.ACTIONS.values()}
    az["none"] = "Nessuna azione domotica"
    return _pulisci(az)


def _correggi_bersaglio(voc: dict, testo: str, nome: str, probabilita: dict) -> Tuple[str, str]:
    """Se il testo nomina una stanza, il bersaglio deve starci dentro.

    "spegni luci garage" dava "Luce Box": l'etichetta e' corta e contiene
    "luce", quindi la parola comune vinceva sulla stanza. Succedeva sette volte.
    Vale +5,7 punti sul bersaglio.

    PROVATO E SCARTATO: usare anche il TIPO di dispositivo nominato
    ("tapparella" → cover) per preferire un apparecchio di quel dominio. Sembra
    ovvio e invece porta il bersaglio da 83,0% a 68,3%: in queste case, quando
    l'utente dice "le luci della cucina" il bersaglio atteso e' lo SCOPE Cucina,
    non un singolo apparecchio, e forzare un device di quel dominio rompe tutto.
    """
    reale, tipo = voc["etichette"].get(nome, (nome, "device"))
    st = rm.stanza_nel_testo(testo, voc["scopes"])
    if not st or reale == st or st in voc["device_in_scope"].get(reale, []):
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


def _payload_lettura(voc: dict, testo: str, context: dict, dati: dict) -> Optional[dict]:
    """Payload di SIMPLE_CHAT: da dove prendere la risposta e con quali parametri.

    Senza questo il payload restava vuoto e main.py finiva nel ramo small-talk:
    "che temperatura c'e' in soggiorno?" non eseguiva nessuna lettura. La logica
    e' quella di jev_engine, che era gia' stata tarata sui dati.
    """
    grandezza = (dati.get("grandezza") or {}).get("value") or rm.TUTTE_LE_GRANDEZZE
    fonte = rm.fonte_risposta(testo, voc["scopes"], list(voc["device_dominio"]), grandezza)
    if fonte == "web_search":
        # la query di ricerca e' testo libero: un vocabolario chiuso non la scrive
        logger.info("GLiNER: web_search vuole una query libera — fallback su Qwen")
        return None
    if fonte == "none":
        # calcolo, ora, saluto: nessuna fonte da interrogare, risponde il generatore
        return {"via": "gliner"}

    luogo = rm.stanza_nel_testo(testo, voc["scopes"])
    if not luogo:
        # la stanza del microfono, ma solo se la frase non nomina un dispositivo
        # preciso: altrimenti lo si cercherebbe nel posto sbagliato
        stanza_ctx = context.get("room")
        if stanza_ctx and stanza_ctx != "unknown":
            luogo = rm.stanza_valida(context.get("location"), stanza_ctx)

    params: dict = {}
    if grandezza != rm.TUTTE_LE_GRANDEZZE:
        # La stanza va DENTRO la stringa di ricerca, non nel filtro: molti sensori
        # in HA non hanno un'area assegnata — "Rehom Soggiorno Temperatura" ha
        # room=Sconosciuto — e il filtro room li ESCLUDE lasciando passare solo
        # rumore. Misurato in jev_engine: 0,580 su spazzatura col filtro, 0,684
        # sul sensore giusto con la stanza nella query.
        params["search"] = f"{grandezza} {luogo}".strip() if luogo else grandezza
    elif luogo:
        # nessuna grandezza ("cosa c'e' in cucina"): qui il filtro strutturale e'
        # proprio quello che serve, e room/zone/floor sono colonne distinte
        params[rm.parametro_luogo(luogo, voc["scope_livello"])] = luogo

    if not params:
        logger.info("GLiNER: lettura senza luogo ne' grandezza — fallback su Qwen")
        return None
    return {"api_call": "entity_discover", "params": params, "via": "gliner"}


async def close() -> None:
    """Chiude la sessione HTTP allo spegnimento."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


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
        # Servono a SIMPLE_CHAT: senza, il payload resta vuoto e le letture dei
        # sensori non partono. Sono due vocabolari chiusi dichiarati in
        # router_model, non testo libero.
        # Solo la grandezza: la FONTE si ricava in codice, perche' chiesta al
        # modello fa 42,5% contro il 90,8% della regola (misurato sugli 87 casi
        # del banco che hanno un payload di lettura atteso).
        "extra": {"grandezza": _pulisci(rm.GRANDEZZE)},
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
    elif intent == "AI_AGENT":
        # La regola lessicale e' piu' affidabile del modello su questa classe:
        # 21/21 con 0 falsi positivi su 405, contro il 10% del classificatore. Se
        # non scatta, AI_AGENT e' quasi sempre sbagliato — "che ore sono?" usciva
        # AI_AGENT con confidenza 0,49.
        intent = "SIMPLE_CHAT"
    if intent == "SET_LOCATION" and n == "domanda":
        # non si dichiara dove si e' facendo una domanda: "che tempo fa domani a
        # Milano?" usciva SET_LOCATION perche' nomina una citta'
        intent = "SIMPLE_CHAT"

    if intent == "SECURITY_ALERT":
        logger.warning(f"GLiNER: tentativo di manipolazione su {text[:80]!r}")
        return {"intent": "RETRY", "confidence": conf,
                "response": "Questo comando non mi convince, non lo eseguo.",
                "interim_response": "", "payload": {"blocked": True, "via": "gliner"}}

    if intent != "HOME_CONTROL":
        if conf < config.GLINER_MIN_CONFIDENCE and n == "incerto":
            logger.info(f"GLiNER conf={conf:.2f} e natura incerta — fallback su Qwen")
            return None
        payload = {"via": "gliner"}
        if intent == "SIMPLE_CHAT":
            payload = _payload_lettura(voc, text, context, dati)
            if payload is None:
                return None
        return {"intent": intent, "confidence": max(conf, _soglia_esecuzione()),
                "response": "", "interim_response": "Ci penso...",
                "payload": payload,
                "_gliner": {"elapsed_ms": round(elapsed_ms), "ms": dati.get("ms")}}

    if _WILDCARD.search(text) and not rm.stanza_nel_testo(text, voc["scopes"]):
        logger.info(f"GLiNER: comando su tutta la casa ({text[:50]!r}) — fallback su Qwen")
        return None

    ber = dati.get("bersaglio") or {}
    if not ber.get("value"):
        return None
    nome, tipo = _correggi_bersaglio(voc, text, ber["value"], ber.get("probabilities"))
    azione = (dati.get("azione") or {}).get("value") or "toggle"
    if azione == "none":
        logger.info("GLiNER: intento domotico ma azione 'none' — fallback su Qwen")
        return None

    # Il dominio non va scelto prima dell'azione, o la stravolge: su uno scope
    # scegliendo "light" per default, `open_cover` diventava `turn_on` e
    # "apri la tapparella della camera" accendeva le luci. Le azioni portano
    # con se' i domini su cui sono valide (router_model.ACTIONS[].domini):
    # fra i domini presenti nel bersaglio si prende uno COMPATIBILE con l'azione.
    validi = set(rm.ACTIONS[azione].domini) if azione in rm.ACTIONS else set()
    if tipo == "device":
        dominio = voc["device_dominio"].get(nome) or "light"
    else:
        domini = voc["scope_domini"].get(nome) or ["light"]
        compatibili = [d for d in domini if d in validi]
        dominio = (compatibili[0] if compatibili
                   else ("light" if "light" in domini else domini[0]))
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
        # la confidenza dichiara la DECISIONE, non l'incertezza del modello: quella
        # resta in `_gliner.intento_conf` per la diagnostica
        "confidence": max(conf, _soglia_esecuzione()),
        "response": (frase.frase.replace("{t}", "") if frase else "Fatto."),
        "interim_response": "",
        "payload": payload,
        "_gliner": {"elapsed_ms": round(elapsed_ms), "ms": dati.get("ms"),
                    "intento_conf": round(conf, 3),
                    "bersaglio_conf": round(float(ber.get("confidence", 0)), 3)},
    }
