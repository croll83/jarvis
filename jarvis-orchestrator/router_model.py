"""Modello decisionale del router — dichiarato UNA volta sola.

Oggi lo stesso vocabolario vive in quattro posti che non concordano:
  jev_engine._ACTIONS (20)  main._valid_ha_actions (25)
  main._map_action_for_domain (11)  router_system_prompt.txt (19 righe di prosa)
Da qui si generano tutte le rese: prompt piatto per Qwen, schema tipizzato per
Jev, label per GLiNER. Il prompt smette di poter contraddire il codice.

Distinzione portante (scelta esplicita del proprietario):
  SCOPE  = stanza / area / zona / "ovunque"  → agisce su TUTTI i device dentro
  DEVICE = una entità precisa e univoca      → agisce su quella e basta
Sono due campi distinti, mai mescolati: "cucina" e "centro block cucina" non
sono lo stesso tipo di bersaglio.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# ───────────────────────────────────────────────────────────── intent ──
# Due forme per ogni intent: `breve` per i prompt in prosa, `criterio` per i
# classificatori a scelta chiusa, che hanno bisogno di sapere DOVE passa il
# confine. Stavano scritti in due posti — il prompt di Qwen e jev_engine — con
# parole diverse, e infatti si contraddicevano sulle ricerche web: il prompt
# mandava il meteo a SIMPLE_CHAT, Jev lo mandava ad AI_AGENT. Da qui in avanti
# il confine è dichiarato una volta.
INTENT_CRITERI: Dict[str, Dict[str, str]] = {
    "HOME_CONTROL": {
        "breve": "comando su dispositivi di casa",
        "criterio": ("Comando domotico su un dispositivo o un luogo della MAPPA ENTITÀ: "
                     "accendere, spegnere, aprire, chiudere, impostare, alzare, abbassare, "
                     "riprodurre musica. Vale anche se è formulato per cortesia "
                     "('puoi accendere la tv?')"),
    },
    "SIMPLE_CHAT": {
        "breve": "risposta diretta, stato dei dispositivi, o una ricerca web",
        "criterio": (
            "Si risolve in UN passo, senza strumenti esterni. Tre famiglie: "
            "(a) calcoli, data e ora, saluti e chiacchiere; "
            "(b) LETTURA dello stato di casa ADESSO — temperatura, umidità, consumi, "
            "batteria, porte aperte, quali dispositivi ci sono o sono accesi. Vale per "
            "un dispositivo o per cento: contare quante luci sono accese è ancora una "
            "lettura, purché il valore sia quello attuale e non vada calcolato; "
            "(c) UNA ricerca web su conoscenza esterna con risposta breve e verificabile: "
            "meteo, 'chi è / cos'è X', notizie del giorno, quotazioni e mercato azionario"),
    },
    "AI_AGENT": {
        "breve": "serve uno strumento esterno, più passi, o ragionamento sui dati",
        "criterio": (
            "Serve uno strumento esterno o più di un passo: email, calendario, "
            "prenotazioni, trading operativo, ricerche approfondite e confronti "
            "multi-fonte, musica dalla libreria personale. "
            "E le domande sulla casa che richiedono di ELABORARE invece di leggere: "
            "storico, andamenti, medie, confronti fra stanze o periodi, 'perché', "
            "'com'è andato', 'è tutto a posto'. "
            "NON le ricerche web semplici (meteo, chi è, notizie, quotazioni): "
            "quelle sono SIMPLE_CHAT con una sola ricerca"),
    },
    "SET_LOCATION": {
        "breve": "l'utente dice dove si trova",
        "criterio": "L'utente comunica in quale casa o luogo si trova",
    },
    "IMAGE_GENERATION": {
        "breve": "generare un'immagine",
        "criterio": "L'utente chiede di generare o disegnare un'immagine",
    },
    "RETRY": {
        "breve": "manca il contesto necessario per decidere",
        "criterio": ("Ambiguo, incomprensibile, oppure manca il contesto per agire in "
                     "sicurezza (es. comando su tutta la casa senza sapere quale casa)"),
    },
    "VERIFY_WITH_AI_AGENT": {
        "breve": "risposta data, ma va verificata con l'agent",
        "criterio": "La risposta è stata data ma va verificata con l'agent",
    },
}

# compatibilità: la forma breve, come era prima
INTENTS: Dict[str, str] = {k: v["breve"] for k, v in INTENT_CRITERI.items()}

# ──────────────────────────────────────────────────────────── azioni ──
@dataclass(frozen=True)
class Action:
    nome: str
    descrizione: str          # per lo schema tipizzato (Jev/GLiNER)
    frase: str                # risposta parlata, templatizzata (niente decode)
    domini: Set[str]
    parametri: Dict[str, str] = field(default_factory=dict)

ACTIONS: Dict[str, Action] = {a.nome: a for a in [
    Action("turn_on",  "Accendere", "Accendo{t}.",
           {"light","switch","media_player","fan","scene","script","input_boolean","automation","vacuum"}),
    Action("turn_off", "Spegnere", "Spengo{t}.",
           {"light","switch","media_player","fan","input_boolean","automation","vacuum"}),
    Action("toggle",   "Invertire lo stato", "Fatto.",
           {"light","switch","media_player","fan","input_boolean","cover","lock"}),
    Action("open_cover",  "Aprire tapparella, tenda o porta", "Apro{t}.", {"cover"}),
    Action("close_cover", "Chiudere tapparella, tenda o porta", "Chiudo{t}.", {"cover"}),
    Action("stop_cover",  "Fermare la tapparella a metà corsa", "Fermo{t}.", {"cover"}),
    Action("set_cover_position", "Portare la tapparella a una posizione parziale",
           "Fatto.", {"cover"}, {"position": "0-100"}),
    Action("set_temperature", "Impostare la temperatura", "Imposto la temperatura.",
           {"climate"}, {"temperature": "gradi, 5-35"}),
    Action("set_hvac_mode", "Cambiare modalità del clima", "Fatto.",
           {"climate"}, {"hvac_mode": "heat|cool|auto|off"}),
    Action("set_percentage", "Impostare la velocità", "Fatto.",
           {"fan"}, {"percentage": "0-100"}),
    Action("volume_set",  "Impostare il volume a un valore preciso", "Fatto.",
           {"media_player"}, {"volume_level": "0.0-1.0"}),
    Action("volume_up",   "Alzare il volume", "Alzo il volume.", {"media_player"}),
    Action("volume_down", "Abbassare il volume", "Abbasso il volume.", {"media_player"}),
    Action("volume_mute", "Silenziare o togliere il muto", "Fatto.",
           {"media_player"}, {"is_volume_muted": "true|false"}),
    Action("media_play",  "Riprendere la riproduzione", "Riprendo.", {"media_player"}),
    Action("media_pause", "Mettere in pausa", "Metto in pausa.", {"media_player"}),
    Action("media_stop",  "Fermare la riproduzione", "Fermo.", {"media_player"}),
    Action("media_next_track", "Saltare al brano successivo", "Salto.", {"media_player"}),
    Action("media_previous_track", "Tornare al brano precedente", "Torno indietro.", {"media_player"}),
    Action("select_source", "Cambiare sorgente o canale", "Fatto.",
           {"media_player"}, {"source": "nome sorgente"}),
    Action("play_music", "Riprodurre musica: artista, brano, album, playlist o radio",
           "Metto la musica.", {"media_player"},
           {"query": "TESTO LIBERO: cosa suonare", "media_type": "playlist|radio",
            "enqueue": "add"}),
    Action("transfer", "Spostare la musica in un'altra stanza", "Sposto la musica.", {"media_player"}),
    Action("now_playing", "Dire che cosa sta suonando", "Controllo.", {"media_player"}),
    Action("lock",   "Chiudere la serratura", "Chiudo la serratura.", {"lock"}),
    Action("unlock", "Aprire la serratura", "Apro la serratura.", {"lock"}),
    Action("press",  "Premere il pulsante (cancelli, aperture)", "Fatto.", {"button"}),
]}

# Descrizioni dei domini in italiano: servono a un CLASSIFICATORE per scegliere,
# quindi sono semantica, non elenchi generati. Erano cablate in jev_engine, dove
# erano state tarate sul campo; qui stanno insieme al vocabolario che descrivono.
DOMINI_DESCRIZIONE: Dict[str, str] = {
    "light":        "Luci, lampade, faretti, strip led",
    "cover":        "Tapparelle, tende, serrande, porte di garage",
    "climate":      "Clima, termostato, riscaldamento, condizionatore",
    "media_player": "TV, speaker, soundbar, musica",
    "fan":          "Ventilatori",
    "switch":       "Prese, interruttori, impianti (filtraggio, boiler, irrigazione)",
    "lock":         "Serrature",
    "button":       "Pulsanti: cancelli, aperture, comandi a impulso",
    "scene":        "Scene",
    "script":       "Script e scenari di casa (Buonanotte, Esco, Rientro)",
    "vacuum":       "Robot aspirapolvere",
    "input_boolean": "Interruttori virtuali di Home Assistant",
    "automation":   "Automazioni di Home Assistant",
}

DOMAINS: Dict[str, List[str]] = {}
for _a in ACTIONS.values():
    for _d in _a.domini:
        DOMAINS.setdefault(_d, []).append(_a.nome)

# forme corte che un LLM emette comunque: normalizzate qui, come fa il resolver
SINONIMI_AZIONE = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover",
                   "pause": "media_pause", "play": "media_play", "skip": "media_next_track",
                   "mute": "volume_mute", "resume": "media_play"}

def normalizza_azione(azione: str, dominio: str) -> str:
    """Azione generica → azione valida per QUEL dominio (allineata a
    main._map_action_for_domain: unica fonte, stesse regole)."""
    azione = SINONIMI_AZIONE.get(azione, azione)
    if dominio == "cover":
        return {"turn_on": "open_cover", "turn_off": "close_cover", "press": "open_cover",
                "unlock": "open_cover", "lock": "close_cover"}.get(azione, azione)
    if dominio == "lock":
        return {"turn_on": "unlock", "turn_off": "lock", "press": "unlock",
                "open_cover": "unlock", "close_cover": "lock"}.get(azione, azione)
    if dominio == "button":
        return "press"
    if dominio == "script":
        return "turn_on"        # gli script si eseguono, turn_off per HA è no-op
    return {"press": "turn_on", "open_cover": "turn_on", "close_cover": "turn_off",
            "unlock": "turn_on", "lock": "turn_off"}.get(azione, azione)

# ────────────────────────────────────────────── slot di testo libero ──
# Ciò che NON è enumerabile: qui un decisore a vocabolario chiuso deve
# rinunciare e passare la mano a un generatore. Dichiararlo rende la regola
# di fallback di Jev una proprietà del modello, non un'euristica.
SLOT_TESTO_LIBERO = {
    ("HOME_CONTROL", "play_music"): "parameters.query",
    ("SIMPLE_CHAT", "web_search"): "params.query",
    ("SIMPLE_CHAT", "entity_discover"): "params.search",   # opzionale
}

API_CALLS = {
    "entity_discover": {"room": "stanza/zona come detta dall'utente",
                        "domain": "light|cover|climate|sensor|switch|media_player|lock|camera",
                        "search": "TESTO LIBERO: dispositivo o grandezza",
                        "device_class": "temperature|humidity|power|energy|motion|battery"},
    "web_search": {"query": "TESTO LIBERO: cosa cercare"},
}

# ───────────────────────────────────────────────────────── bersagli ──
AZIONABILI = {"light", "switch", "cover", "climate", "media_player", "fan",
              "lock", "vacuum", "button", "script", "scene", "input_boolean"}

# Bersagli offribili a un decisore: le azionabili più le telecamere, che non si
# comandano ma sono bersagli legittimi di una domanda ("le telecamere del
# giardino registrano?"). È l'insieme che jev_engine già offriva di fatto.
VOCABOLARIO_BERSAGLI = AZIONABILI | {"camera"}

@dataclass
class Bersagli:
    scopes: List[str]                    # stanze, aree, zone, "ovunque" → bulk
    devices: List[str]                   # entità univoche → singolo
    device_dominio: Dict[str, str]
    scope_domini: Dict[str, List[str]]   # quali domini esistono in ogni scope
    device_in_scope: Dict[str, set] = field(default_factory=dict)  # device → scope che lo contengono
    scope_livello: Dict[str, str] = field(default_factory=dict)    # scope → 'piano' | 'zona' | 'stanza'

def carica_bersagli(location_id: str, righe: Optional[List[dict]] = None) -> Bersagli:
    """Bersagli reali di una casa. `righe` = entity_maps (se None, legge dal DB)."""
    if righe is None:
        from database import _get_conn
        conn = _get_conn(); c = conn.cursor()
        c.execute("""SELECT zone, area, room, entity_type, entity_name
                     FROM entity_maps
                     WHERE location_id = ? AND entity_id IS NOT NULL
                       AND COALESCE(visible,1) = 1
                       AND LOWER(COALESCE(room,'')) != 'sconosciuto'
                       AND LOWER(COALESCE(zone,'')) != 'non classificato'""", (location_id,))
        righe = [dict(r) for r in c.fetchall()]
        conn.close()
    righe = [r for r in righe if r.get("entity_type") in VOCABOLARIO_BERSAGLI]
    scopes, devices, dom, scope_dom = [], [], {}, {}
    dev_scope: Dict[str, set] = {}
    livello: Dict[str, str] = {}
    for r in righe:
        nome = (r.get("entity_name") or "").strip()
        if nome and nome not in dom:
            devices.append(nome); dom[nome] = r["entity_type"]
        if nome:
            dev_scope.setdefault(nome, set()).update(
                (r.get(k) or "").strip() for k in ("room", "area", "zone") if (r.get(k) or "").strip())
        for chiave, liv in (("room", "stanza"), ("area", "zona"), ("zone", "piano")):
            s = (r.get(chiave) or "").strip()
            if s and s.lower() not in ("sconosciuto", "non classificato", "others"):
                if s not in scopes:
                    scopes.append(s)
                    livello[s] = liv
                scope_dom.setdefault(s, [])
                if r["entity_type"] not in scope_dom[s]:
                    scope_dom[s].append(r["entity_type"])
    scopes.append("ovunque")
    scope_dom["ovunque"] = sorted(AZIONABILI)
    livello["ovunque"] = "casa"
    return Bersagli(sorted(scopes), sorted(devices), dom, scope_dom, dev_scope, livello)


def vocabolario_casa(location_id: str, righe: Optional[List[dict]] = None):
    """Bersagli + indice per la casa: l'unica lettura dell'entity map.

    Ritorna (bersagli, lookup) dove lookup mappa il nome di un dispositivo su
    (stanza, dominio, location): serve a chi deve costruire il payload dopo che
    il bersaglio è stato scelto. jev_engine aveva la propria lettura, con un
    filtro leggermente diverso (includeva `camera`, escludeva `input_boolean`):
    due vocabolari quasi uguali sono peggio di uno solo, perché la differenza
    non si vede finché non sbaglia.
    """
    if righe is None:
        from database import _get_conn
        conn = _get_conn(); c = conn.cursor()
        c.execute("""SELECT zone, area, room, entity_type, entity_name, location_id
                     FROM entity_maps
                     WHERE location_id = ? AND entity_id IS NOT NULL
                       AND COALESCE(visible,1) = 1
                       AND LOWER(COALESCE(room,'')) != 'sconosciuto'
                       AND LOWER(COALESCE(zone,'')) != 'non classificato'""", (location_id,))
        righe = [dict(r) for r in c.fetchall()]
        conn.close()
    b = carica_bersagli(location_id, righe)
    lookup = {}
    for r in righe:
        if r.get("entity_type") not in VOCABOLARIO_BERSAGLI:
            continue
        nome = (r.get("entity_name") or "").strip()
        if nome and nome not in lookup:
            lookup[nome] = (r.get("room"), r.get("entity_type"),
                            r.get("location_id") or location_id)
    return b, lookup


def stanza_valida(location_id: str, nome: Optional[str]) -> Optional[str]:
    """Il nome è una stanza/zona/piano VERO di quella casa? Altrimenti None.

    La "stanza" di un dispositivo vocale è il suo friendly_name (main.py), e
    funziona solo finché i dispositivi si chiamano come le stanze. Sui client
    mobili non può funzionare: un telefono si chiama "Fold 7 Marco" e si sposta.
    Passare quel nome a valle come stanza fa filtrare entity_discover e
    resolve_entity_id (dove il filtro è HARD) su una stanza inesistente, che
    non trova niente e scarta l'entità giusta. Meglio nessun indizio che uno falso.
    """
    if not nome:
        return None
    n = nome.strip()
    if not n or n.lower() in ("unknown", "sconosciuto", "none", "casa"):
        return None
    try:
        b = carica_bersagli(location_id)
    except Exception:
        return n          # senza mappa non si può giudicare: si lascia passare
    if n in b.scopes:
        return n
    esatto = next((s for s in b.scopes if s.lower() == n.lower()), None)
    return esatto


# ─────────────────────────────────────────────────────── coreferenza ──
# "ora spegnila", "rifallo", "e l'umidità?": frasi che NON nominano il
# bersaglio perché lo danno per detto. Non sono ambigue per chi parla, e non
# hanno bisogno di un modello: il bersaglio è quello dell'ultima azione
# ESEGUITA, che l'orchestrator già conosce.
#
# La sorgente è l'ultima azione andata a buon fine, non il turno precedente.
# Sono due cose diverse quando un turno fallisce:
#   1. "accendi la strip led del salotto"  → eseguito
#   2. "ora spegnila" → STT la storpia in "ora spargi qua" → nessuna azione
#   3. "ora spegnila" → il bersaglio è quello del turno 1, non del 2.
# Registrando solo gli esiti `ok`, il turno 2 semplicemente non esiste in
# memoria e il salto avviene da sé.

# Pronome clitico attaccato al verbo (accendi+la, spegni+le): in italiano è il
# segnale più netto che il bersaglio è già noto.
_CLITICO = re.compile(
    r"\b((?:ri)?(?:accend|spegn|speng|apr|chiud|alz|abbass|fall|met|avvi|ferm|stacc|attiv|disattiv)"
    r"\w*(?:la|lo|le|li|ne)\b|rifall[ao]|ancora una volta|di nuovo|un'altra volta)", re.I)
# "aprile" è anche il mese: "quanti giorni è 2 aprile" non è una coreferenza.
_MESE_APRILE = re.compile(r"(?:\d|\bil\b|\bdi\b|\bin\b)\s*aprile\b", re.I)
# Seguito ellittico: "e l'umidità?" eredita il luogo della domanda precedente.
_SEGUITO = re.compile(r"^\s*(?:e|ed|invece|ma)\b.{0,40}\?\s*$", re.I)

def riferimento_a_turno_precedente(testo: str, vocabolario: Optional[Set[str]] = None) -> bool:
    """La frase rimanda a un bersaglio già detto invece di nominarlo.

    `vocabolario` = nomi di stanze e dispositivi della casa: se la frase ne
    nomina uno, il bersaglio è lì e non va ereditato. "e in camera?" eredita
    la grandezza (la temperatura), non il luogo — e il luogo è ciò che qui
    stiamo risolvendo.
    """
    if not testo:
        return False
    if _CLITICO.search(testo) and not _MESE_APRILE.search(testo):
        return True
    if not _SEGUITO.search(testo):
        return False
    t = testo.lower()
    nomi = vocabolario if vocabolario is not None else set()
    return not any(re.search(rf"\b{re.escape(n.lower())}\b", t) for n in nomi)

# Verbo → azione generica. `normalizza_azione` la adatta poi al dominio reale
# del bersaglio ereditato, quindi qui bastano le forme neutre.
_VERBO_AZIONE = [
    (r"\b(?:ri)?(?:accend|attiv|avvi|riaccend)", "turn_on"),
    (r"\b(?:ri)?(?:spegn|speng|disattiv|stacc|ferm)", "turn_off"),
    (r"\b(?:ri)?apr", "open_cover"),
    (r"\b(?:ri)?chiud", "close_cover"),
    (r"\b(?:ri)?alz|\bsu\b", "volume_up"),
    (r"\b(?:ri)?abbass|\bgi[uù]\b", "volume_down"),
]

def azione_dal_verbo(testo: str, dominio: str, azione_precedente: Optional[str] = None) -> Optional[str]:
    """Azione espressa dal verbo della frase, adattata al dominio del bersaglio.

    "rifallo" / "di nuovo" non portano un verbo proprio: ripetono l'azione
    precedente.
    """
    t = (testo or "").lower()
    if re.search(r"\brifall[ao]\b|\bancora una volta\b|\bdi nuovo\b|\bun'altra volta\b", t):
        return normalizza_azione(azione_precedente, dominio) if azione_precedente else None
    for pattern, azione in _VERBO_AZIONE:
        if re.search(pattern, t):
            if azione in ("volume_up", "volume_down") and dominio != "media_player":
                azione = "open_cover" if azione == "volume_up" else "close_cover"
            return normalizza_azione(azione, dominio)
    return None
