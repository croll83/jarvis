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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# ───────────────────────────────────────────────────────────── intent ──
INTENTS: Dict[str, str] = {
    "HOME_CONTROL":  "comando su dispositivi di casa",
    "SIMPLE_CHAT":   "risposta diretta, stato dispositivi, o 1 ricerca web",
    "AI_AGENT":      "serve uno strumento esterno, più passi o dati storici",
    "SET_LOCATION":  "l'utente dice dove si trova",
    "IMAGE_GENERATION": "generare un'immagine",
    "RETRY":         "manca il contesto necessario per decidere",
    "VERIFY_WITH_AI_AGENT": "risposta data, ma va verificata con l'agent",
}

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

@dataclass
class Bersagli:
    scopes: List[str]                    # stanze, aree, zone, "ovunque" → bulk
    devices: List[str]                   # entità univoche → singolo
    device_dominio: Dict[str, str]
    scope_domini: Dict[str, List[str]]   # quali domini esistono in ogni scope
    device_in_scope: Dict[str, set] = field(default_factory=dict)  # device → scope che lo contengono

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
    righe = [r for r in righe if r.get("entity_type") in AZIONABILI]
    scopes, devices, dom, scope_dom = [], [], {}, {}
    dev_scope: Dict[str, set] = {}
    for r in righe:
        nome = (r.get("entity_name") or "").strip()
        if nome and nome not in dom:
            devices.append(nome); dom[nome] = r["entity_type"]
        if nome:
            dev_scope.setdefault(nome, set()).update(
                (r.get(k) or "").strip() for k in ("room", "area", "zone") if (r.get(k) or "").strip())
        for chiave in ("room", "area", "zone"):
            s = (r.get(chiave) or "").strip()
            if s and s.lower() not in ("sconosciuto", "non classificato", "others"):
                if s not in scopes:
                    scopes.append(s)
                scope_dom.setdefault(s, [])
                if r["entity_type"] not in scope_dom[s]:
                    scope_dom[s].append(r["entity_type"])
    scopes.append("ovunque"); scope_dom["ovunque"] = sorted(AZIONABILI)
    return Bersagli(sorted(scopes), sorted(devices), dom, scope_dom, dev_scope)
