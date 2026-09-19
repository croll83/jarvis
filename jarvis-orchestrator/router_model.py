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
    # IMAGE_GENERATION non è dichiarato: main.py:5966 lo inoltra ad AI Agent
    # senza fare nient'altro, quindi come decisione di routing non esiste — è
    # AI_AGENT con un nome diverso. Offrirlo costa a ogni classificatore
    # un'opzione in più da soppesare, e le opzioni inutili spostano le
    # decisioni: aggiungere etichette a una classe la fa vincere. Restano
    # attivi il ramo di dispatch e la scorciatoia a parole chiave in
    # ai_engines (che risparmia la chiamata al modello), entrambi scorciatoie
    # verso AI_AGENT, non un intent da far scegliere.
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

# Nomi che HA usa quando un'entita' non ha un'area: non sono luoghi.
_SCOPE_FASULLI = ("sconosciuto", "non classificato", "others")

def carica_bersagli(location_id: str, righe: Optional[List[dict]] = None) -> Bersagli:
    """Bersagli reali di una casa. `righe` = entity_maps (se None, legge dal DB)."""
    if righe is None:
        from database import _get_conn
        conn = _get_conn(); c = conn.cursor()
        # Il filtro sulla stanza NON va nella query: scarterebbe la RIGA, e con
        # essa un dispositivo perfettamente comandabile che semplicemente non ha
        # un'area assegnata in HA. Misurato col diff dei due vocabolari: 10
        # azionabili esclusi su albani20 e 4 su wagmi — script come "Privacy
        # Telecamere" e "Sicurezza Telecamere", il clima "Rehom Soggiorno", i
        # media player "Casa" e "Tutta Casa". Il classificatore non poteva
        # sceglierli nemmeno volendo: non erano fra le etichette.
        # (Le entita' di servizio tipo "AirPlay TV Cucina" restano fuori lo
        # stesso, e giustamente: hanno visible=0.)
        # I nomi di stanza fasulli vengono gia' scartati piu' sotto, dove si
        # costruiscono gli SCOPE: e' li' che servono, perche' uno scope e' un
        # luogo mentre un device no.
        c.execute("""SELECT zone, area, room, entity_type, entity_name
                     FROM entity_maps
                     WHERE location_id = ? AND entity_id IS NOT NULL
                       AND COALESCE(visible,1) = 1""", (location_id,))
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
                (r.get(k) or "").strip() for k in ("room", "area", "zone")
                if (r.get(k) or "").strip()
                and (r.get(k) or "").strip().lower() not in _SCOPE_FASULLI)
        for chiave, liv in (("room", "stanza"), ("area", "zona"), ("zone", "piano")):
            s = (r.get(chiave) or "").strip()
            if s and s.lower() not in _SCOPE_FASULLI:
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


# ──────────────────────────────────────────── regole di lingua italiana ──
# Tre cose che un classificatore a similarita' di embedding NON puo' vedere, e
# che in italiano sono marcate in modo regolare. Misurate sul banco dei 426:
# portano l'intento da 74,2 a 86,6 e il bersaglio da 77,7 a 83,4.

# 1. COMANDO o DOMANDA. E' il confine fra HOME_CONTROL e SIMPLE_CHAT, e non e'
# semantico: "accendi la luce della cucina" e "quali luci sono accese in
# cucina?" nominano le stesse cose con le stesse parole, cambia il modo del
# verbo. Per un modello che segna similarita' fra embedding sono quasi
# identiche. In regole: 90,4%.
_INTERROGATIVO = re.compile(
    r"^\s*(qual[ei]?|quant[oaie]|com[e']|dove|quando|perch[eé]|chi\b|che\b|cosa|"
    r"c'[eè]\b|ci sono|mi dici|dimmi|sai\b|puoi dirmi|vorrei sapere)", re.I)
_IMPERATIVO = re.compile(
    r"\b(accend|spegn|speng|apr|chiud|alz|abbass|met|avvi|ferm|attiv|disattiv|"
    r"imposta|porta|fai|manda|riproduc|suona|regola|stacc|azion|aument|diminu)", re.I)
_STATO = re.compile(r"\b(acces[ao]|spent[ao]|apert[ao]|chius[ao]|attiv[ao]|stato|temperatura|"
                    r"umidit|consumo|batteria|gradi)\b", re.I)
_VERBI_INTERI = ("accendi", "spegni", "apri", "chiudi", "alza", "abbassa", "metti", "avvia",
                 "ferma", "attiva", "disattiva", "imposta", "porta", "fai", "manda",
                 "riproduci", "suona", "regola", "stacca", "aumenta")

def _verbo_storpiato(testo: str) -> bool:
    """"spinni", "spaini", "pegni" SONO verbi: lo STT li rovina, non li cancella."""
    from difflib import SequenceMatcher
    for parola in re.findall(r"\b\w{4,}\b", testo.lower())[:4]:
        if any(SequenceMatcher(None, parola, v).ratio() >= 0.72 for v in _VERBI_INTERI):
            return True
    return False

# Comando di CORTESIA: ha la forma di una domanda ma chiede di agire. Il
# criterio di HOME_CONTROL lo dice a chiare lettere — "vale anche se e' formulato
# per cortesia ('puoi accendere la tv?')" — e ignorarlo costa caro: 9 delle 12
# regressioni di intento contro la baseline Jev erano "puoi spegnere X?" finite
# su SIMPLE_CHAT. Serve un modale di richiesta seguito dall'INFINITO di un verbo
# di comando, oppure un clitico dativo ("mi accendi...").
_CORTESIA = re.compile(
    r"\b(puoi|potresti|riesci a|ti va di|mi fai|per favore|per piacere)\b[^?]{0,30}?"
    r"\b(accend\w*|spegn\w*|apr\w*|chiud\w*|alz\w*|abbass\w*|mett\w*|avvi\w*|"
    # "port\w*" NON c'e': catturerebbe il sostantivo "porta" — "puoi controllare
    # se la porta e' aperta?" e' una domanda, non un comando. In italiano
    # portare/la porta sono omografi, e qui il sostantivo e' molto piu' frequente.
    r"ferm\w*|attiv\w*|disattiv\w*|impost\w*|mand\w*|riproduc\w*|"
    r"suon\w*|regol\w*|stacc\w*|aument\w*|diminu\w*)"
    r"|^\s*mi\s+(accend|spegn|apr|chiud|alz|abbass|met|avvi|ferm|attiv|disattiv)", re.I)

def natura(testo: str) -> str:
    """'comando' | 'domanda' | 'incerto' — dalla forma della frase, non dal senso."""
    if not testo:
        return "incerto"
    # la cortesia ha la precedenza sulla forma interrogativa: chiede di agire
    if _CORTESIA.search(testo):
        return "comando"
    interroga = bool(_INTERROGATIVO.search(testo)) or testo.strip().endswith("?")
    imperativo = bool(_IMPERATIVO.search(testo))
    if interroga and not (imperativo and not testo.strip().endswith("?")):
        return "domanda"
    if imperativo:
        return "comando"
    if _STATO.search(testo):
        return "domanda"
    return "comando" if _verbo_storpiato(testo) else "incerto"

# 2. AI_AGENT. Il criterio dichiarato sopra ("serve uno strumento esterno o piu'
# di un passo") ha una firma lessicale, non semantica. Misurato: 21/21 sul
# banco con 0 falsi positivi su 405, contro il 10% del classificatore.
_STRUMENTO_ESTERNO = re.compile(
    r"\b(mail|email|posta|agenda|calendario|appuntament\w+|impegn\w+|riunion\w+|"
    r"trading|portfolio|portafoglio|crypto|borsa|libreria|discografia)\b", re.I)
_ELABORAZIONE = re.compile(
    r"\b(analizz\w+|confront\w+|riassum\w+|organizz\w+|pianific\w+|"
    r"consigli\w+|suggeris\w+|gener\w+|disegn\w+|scriv\w+)\b", re.I)
_PRENOTAZIONE = re.compile(
    r"\b(prenot\w+)\b|\b(cerc\w+|trov\w+)\b.{0,20}\b(volo|voli|albergo|hotel|tavolo)\b", re.I)
_PLAYLIST = re.compile(r"\bplaylist\b", re.I)
# lettura AGGREGATA: una grandezza di casa insieme a un periodo. Il criterio
# manda ad AI_AGENT le domande che richiedono di ELABORARE i dati, non leggerli.
_GRANDEZZA = re.compile(r"\b(consum\w+|spes[ao]|speso|energia|produzion\w+|kwh|bolletta)\b", re.I)
_PERIODO = re.compile(r"\b(settimana|mese|mesi|anno|ieri|stanotte|scors\w+|gennaio|febbraio|"
                      r"marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|"
                      r"novembre|dicembre)\b", re.I)

def serve_strumento_esterno(testo: str, scopes: Optional[List[str]] = None) -> bool:
    """La frase richiede uno strumento esterno o piu' di un passo: AI_AGENT."""
    if not testo:
        return False
    if _STRUMENTO_ESTERNO.search(testo) or _ELABORAZIONE.search(testo) or _PRENOTAZIONE.search(testo):
        return True
    if _PLAYLIST.search(testo):
        # "metti una playlist in salotto" e' un comando di casa; senza un luogo
        # e' musica dalla libreria personale, che il criterio manda ad AI_AGENT
        t = testo.lower()
        if not any(re.search(rf"\b{re.escape(s.lower())}\b", t) for s in (scopes or [])):
            return True
    return bool(_GRANDEZZA.search(testo) and _PERIODO.search(testo))

# 3. LA STANZA NEL TESTO. E' una stringa letterale, quindi non serve un modello:
# si cerca con tolleranza alle storpiature. Serve a rifiutare i bersagli che
# stanno altrove — "spegni luci garage" non puo' dare "Luce Box", che il
# classificatore scegliera' sette volte su sette perche' l'etichetta e' corta e
# contiene "luce". Vale +5,7 punti sul bersaglio.
def stanza_nel_testo(testo: str, scopes: List[str], soglia: float = 0.84) -> Optional[str]:
    """Il nome di stanza, zona o piano nominato nel testo, anche storpiato."""
    from difflib import SequenceMatcher
    if not testo or not scopes:
        return None
    pulito = " " + re.sub(r"[^\w\s]", " ", testo.lower()) + " "
    parole = pulito.split()
    migliore, punteggio = None, 0.0
    for s in scopes:
        sl = s.lower()
        if f" {sl} " in pulito:
            return s                                  # esatta: non serve altro
        n = len(sl.split())
        for i in range(len(parole) - n + 1):
            r = SequenceMatcher(None, " ".join(parole[i:i + n]), sl).ratio()
            if r > punteggio:
                migliore, punteggio = s, r
    return migliore if punteggio >= soglia else None


# ─────────────────────────────────── letture di casa: fonte e grandezza ──
# Due vocabolari chiusi che servono a SIMPLE_CHAT. Senza di loro il payload
# resta vuoto e le letture dei sensori non partono: "che temperatura c'e' in
# soggiorno?" arrivava a main.py senza `api_call` e finiva nel ramo small-talk.
FONTI_RISPOSTA: Dict[str, str] = {
    "entity_discover": "Dati di CASA: sensori, stato dei dispositivi, cosa c'è in una stanza",
    "web_search": "Conoscenza esterna: meteo, notizie, fatti generali",
    "none": "Nessuna fonte: calcolo, data e ora, saluto",
}

# Grandezze misurate come vocabolario CHIUSO. `entity_discover` ignora
# device_class nel percorso strutturato e lo onora solo in quello semantico, che
# vuole `search`: facendo scegliere i termini da questa lista invece di generarli,
# le letture restano su un decisore a vocabolario chiuso.
TUTTE_LE_GRANDEZZE = "__tutto__"
GRANDEZZE: Dict[str, str] = {
    "temperatura": "Temperatura",
    "umidita": "Umidità",
    "consumo": "Consumo elettrico, potenza istantanea, watt",
    "energia": "Energia consumata o prodotta, kWh",
    "batteria": "Livello di carica di una batteria",
    "movimento": "Rilevazione di movimento o presenza",
    "luminosita": "Luminosità o illuminamento",
    "pressione": "Pressione",
    "porta finestra": "Stato di apertura di porte o finestre",
    "acqua": "Perdite d'acqua, livello o portata",
    "pompa di calore": "Pompa di calore, caldaia, riscaldamento",
    "fotovoltaico": "Produzione solare, inverter, fotovoltaico",
    TUTTE_LE_GRANDEZZE: "Nessuna grandezza specifica: si chiede cosa c'è o lo stato generale",
}

def parametro_luogo(nome: str, livelli: Dict[str, str]) -> str:
    """Il nome del parametro giusto per un luogo: room, zone o floor.

    `entity_discover` ha tre parametri distinti che filtrano su tre colonne
    diverse. Mandare una zona nel parametro `room` non trova niente.
    """
    return {"zona": "zone", "piano": "floor"}.get(livelli.get(nome, "stanza"), "room")


# La FONTE non si chiede al modello: misurato 42,5% contro il 90,8% di questa
# regola. Il motivo e' lo stesso di sempre — non e' una somiglianza semantica.
# "Dimmi la temperatura del soggiorno" e "che ore sono" sono entrambe domande
# brevi, e l'etichetta "nessuna fonte: calcolo, data e ora" vinceva su 38 casi
# che volevano i dati di casa. Il segnale vero e' lessicale e strutturale: una
# grandezza misurata, oppure un nome di casa nominato, oppure parole da ricerca.
_DA_WEB = re.compile(
    r"\b(meteo|che tempo|piover|previsioni|notiz|giornal|chi (?:è|e)\b|cos'?(?:è|e)\b|"
    r"quanto costa|borsa|bitcoin|cambio|dollaro|euro\b|significa|capitale|"
    r"popolazione|traduc)", re.I)
# parole che indicano lo stato o una grandezza di casa, anche senza nominare un
# dispositivo preciso ("è tutto spento?")
_DA_CASA = re.compile(
    r"\b(acces[ao]|spent[ao]|apert[ao]|chius[ao]|attiv[ao]|stato|gradi|temperatur|"
    r"umidit|consum|batteri|dispositiv|sensor|tapparell|luc[ei]|lampad|tv|televisio|"
    r"clima|termostat|porta|finestr|robot|aspirapolvere|serratur)", re.I)

def fonte_risposta(testo: str, scopes: List[str], devices: List[str],
                   grandezza: Optional[str] = None) -> str:
    """Da dove prendere la risposta a una domanda: 'entity_discover', 'web_search'
    oppure 'none' quando risponde il generatore da solo."""
    if not testo:
        return "none"
    if _DA_WEB.search(testo):
        return "web_search"
    if grandezza and grandezza != TUTTE_LE_GRANDEZZE:
        return "entity_discover"
    t = testo.lower()
    nomina = (stanza_nel_testo(testo, scopes) is not None
              or any(re.search(rf"\b{re.escape(n.lower())}\b", t) for n in devices))
    if nomina or _DA_CASA.search(testo):
        return "entity_discover"
    return "none"


# 4. IL PLURALE DICE "TUTTI QUELLI DELLA STANZA". "spegni le luci del garage"
# vuole lo scope Garage, non una luce singola — ma il classificatore sceglieva
# "Luce Box", e il vincolo di stanza non lo rifiutava perche' Box sta DENTRO
# Garage, quindi formalmente il device e' "nella stanza giusta".
# E' la domanda `is_collective` che Jev pone e che qui mancava.
_PLURALE_COLLETTIVO = re.compile(
    r"\b(tutt[eiao]\s+)?(le|i|gli|dei|delle|degli)\s+\w*"
    r"(luci|lampade|tapparelle|tende|prese|faretti|termosifoni|finestre|serrande)\b"
    r"|\bluci\b|\btapparelle\b", re.I)

def comando_collettivo(testo: str) -> bool:
    """Il comando vale per tutti gli apparecchi di un luogo, non per uno solo."""
    return bool(testo) and bool(_PLURALE_COLLETTIVO.search(testo))


# 5. IL VERBO DISTRUTTO DALLO STT. "Spini", "pegni", "spendio", "spennie",
# "pagnire" sono tutti "spegni", e il classificatore li legge come turn_on:
# ACCENDE invece di spegnere, che e' il peggior errore possibile per un comando
# vocale. La firma e' netta — una parola che somiglia foneticamente a "spegni",
# e nessuna che somigli ad "accendi" — e la confidenza dell'azione in quei casi
# sta fra 0,32 e 0,53, contro 0,86-0,99 quando il modello ha ragione.
# Misurato: corregge 6 casi e ne peggiora 0.
_SPEGNERE = ("spegni", "spegnere", "spegnila", "spegnile", "spegnilo")
_ACCENDERE = ("accendi", "accendere", "accendila", "accendile", "accendilo")

def _somiglia_a(testo: str, verbi: tuple, soglia: float) -> bool:
    from difflib import SequenceMatcher
    for parola in re.findall(r"\b[\w']{4,}\b", testo.lower())[:4]:
        if any(SequenceMatcher(None, parola, v).ratio() >= soglia for v in verbi):
            return True
    return False

def chiede_di_spegnere(testo: str) -> bool:
    """Il testo chiede di SPEGNERE, anche se lo STT ha distrutto il verbo."""
    if not testo:
        return False
    return _somiglia_a(testo, _SPEGNERE, 0.65) and not _somiglia_a(testo, _ACCENDERE, 0.75)
