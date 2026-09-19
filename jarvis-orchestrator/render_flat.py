"""Resa 'prompt piatto' del modello decisionale, per Qwen generativo.

Il prompt non e' scritto a mano: e' generato da `router_model` piu' la mappa
reale della casa. Due conseguenze volute:
  - un esempio non puo' citare un'entita' inesistente (era il caso di "Luci Frigo");
  - un esempio non puo' insegnare una costante da copiare, perche' i nomi vengono
    dalla casa su cui si sta decidendo;
  - e soprattutto, il prompt non puo' piu' CONTRADDIRE il codice: intenti,
    azioni e domini escono dalle stesse dichiarazioni che leggono Jev e GLiNER.

ATTENZIONE — E' SPENTO PER DEFAULT, E PER UNA RAGIONE MISURATA.
Sul banco dei 426 casi, con Qwen 2.5 7B come router primario:

    variante                        token   intent   payload   bersaglio   collettivi
    scritto a mano (baseline)        5315    88,5%     65,3%      64,8%       58,2%
    generato v3 (questo)             2121    89,0%     61,3%      59,6%       67,1%
    chirurgico C (DEPLOYATO)         ~5000   88,7%     68,3%      67,2%       79,5%

Il generato pareggia l'intent con META' dei token e guadagna 9 punti sui
comandi collettivi — il difetto piu' fastidioso della versione scritta a mano —
ma perde 7 punti di payload contro quella deployata: i 48 esempi scritti a mano
fanno un lavoro che la generazione non replica.

Quelle misure pero' sono state prese quando Qwen era il router PRIMARIO. Ora
Qwen e' l'ultimo anello e vede solo cio' che GLiNER passa — un sottoinsieme
diverso e piu' difficile. Su quel sottoinsieme il confronto NON e' stato fatto.
Prima di accendere `ROUTER_PROMPT_GENERATO`, rimisurare.
"""
import json
from typing import List, Optional
import router_model as M


SUFFISSI_TECNICI = ("dlna", "mass", "chromecast", "cast", "2 ", "bluetooth", "airplay",
                    "non disturbare", "dnd", "notifica", "echo dot", " app")

def _pulito(nome: str) -> bool:
    n = (nome or "").lower()
    return bool(n) and not any(s in n for s in SUFFISSI_TECNICI) and len(n.split()) <= 3

def _migliore(cands, preferiti=()):
    """Il candidato più adatto a fare da esempio: pulito, corto, e se possibile
    fra quelli preferiti (es. gli scenari veri invece di uno script di servizio)."""
    puliti = [c for c in cands if _pulito(c)]
    for p in preferiti:
        for c in puliti:
            if p in c.lower():
                return c
    return min(puliti, key=lambda s: (len(s.split()), len(s))) if puliti else (cands[0] if cands else None)


def _scelte(b: M.Bersagli, dominio: str, n: int = 1, multi: bool = True) -> List[str]:
    """n scope che contengono >1 device di quel dominio (multi=True) o device singoli."""
    if multi:
        conta = {}
        for s, domini in b.scope_domini.items():
            if dominio in domini and s != "ovunque":
                conta[s] = sum(1 for d, dd in b.device_dominio.items() if dd == dominio)
        return list(conta)[:n]
    return [d for d, dd in b.device_dominio.items() if dd == dominio][:n]


def _esempi(b: M.Bersagli, loc: str, mappa_righe) -> List[str]:
    """Esempi costruiti sulle entità vere di QUESTA casa."""
    per_stanza = {}
    for r in mappa_righe:
        if r.get("entity_type") == "light" and r.get("room"):
            per_stanza.setdefault(r["room"], []).append(r["entity_name"])
    stanza_multi = next((s for s, v in per_stanza.items() if len(v) > 1 and _pulito(s)), None)
    luce_singola = _migliore([v for vs in per_stanza.values() for v in vs])
    zona = next((s for s in b.scopes if s.lower().startswith("zona")), None) or \
           next((s for s in b.scopes if s.lower().startswith("piano")), None)
    dev = lambda dom: [d for d, dd in b.device_dominio.items() if dd == dom]
    cover = _migliore(dev("cover"), ("tapparella", "porta"))
    script = _migliore(dev("script"), ("buonanotte", "buongiorno", "rientro"))
    button = _migliore(dev("button"), ("pedonale", "carrabile", "cancell"))
    tv = _migliore([d for d in dev("media_player") if d.lower().startswith("tv")])
    stanza_tv = next((r["room"] for r in mappa_righe
                      if r.get("entity_name") == tv), None) if tv else None

    E = []
    def add(testo, obj):
        E.append(f'"{testo}" → {json.dumps(obj, ensure_ascii=False, separators=(",", ":"))}')

    # lo scope è il caso più frequente del parlato reale: più di un esempio, su
    # stanze diverse, e senza la parola "tutte" (che spesso l'utente non dice)
    altre_stanze = [s for s, v in per_stanza.items() if len(v) > 1 and _pulito(s)][:3]
    for i, st in enumerate(altre_stanze):
        art = 'della' if st[-1] in 'aà' else 'del'
        testo = (f"spegni le luci {art} {st.lower()}" if i == 0 else
                 (f"accendi luci {st.lower()}" if i == 1 else f"spegni {st.lower()}"))
        azione = "turn_off" if i != 1 else "turn_on"
        add(testo, {"intent": "HOME_CONTROL",
                    "payload": {"entity": st, "domain": "light", "action": azione}})
    if zona:
        add(f"accendi le luci in {zona.lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": zona, "domain": "light",
                                                   "action": "turn_on"}})
    add("spegni tutto",
        {"intent": "HOME_CONTROL", "payload": {"entity": "ovunque", "action": "turn_off"}})
    if altre_stanze:
        add(f"spegni tutte le luci {'della' if altre_stanze[0][-1] in 'aà' else 'del'} {altre_stanze[0].lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": altre_stanze[0],
                                                   "domain": "light", "action": "turn_off"}})
    if luce_singola:
        add(f"accendi {luce_singola.lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": luce_singola, "domain": "light",
                                                   "action": "turn_on"}})
    if tv:
        add(f"spegni {tv.lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": tv, "domain": "media_player",
                                                   "action": "turn_off"}})
    if cover:
        add(f"apri {cover.lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": cover, "domain": "cover",
                                                   "action": "open_cover"}})
    if button:
        add(f"apri il cancello {button.lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": button, "domain": "button",
                                                   "action": "press"}})
    if script:
        add(f"avvia {script.lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": script, "domain": "script",
                                                   "action": "turn_on"}})
    if stanza_tv:
        add(f"metti i pink floyd in {stanza_tv.lower()}",
            {"intent": "HOME_CONTROL", "payload": {"entity": stanza_tv, "domain": "media_player",
                                                   "action": "play_music",
                                                   "parameters": {"query": "pink floyd"}}})
    add("metti in pausa",
        {"intent": "HOME_CONTROL", "payload": {"entity": "unknown", "domain": "media_player",
                                               "action": "media_pause"}})
    if stanza_multi:
        add(f"che temperatura c'è in {stanza_multi.lower()}?",
            {"intent": "SIMPLE_CHAT", "payload": {"api_call": "entity_discover",
             "params": {"room": stanza_multi, "search": "temperatura",
                        "device_class": "temperature"}}})
    add("che tempo fa domani?",
        {"intent": "SIMPLE_CHAT", "payload": {"api_call": "web_search",
         "params": {"query": "meteo domani"}}})
    add("ciao, come stai?", {"intent": "SIMPLE_CHAT", "response": "Tutto sotto controllo."})
    add("leggi le mie email", {"intent": "AI_AGENT", "interim_response": "Leggo le email..."})
    add("ho appuntamenti domani?", {"intent": "AI_AGENT", "interim_response": "Controllo il calendario..."})
    add("com'è andato il consumo questa settimana?", {"intent": "AI_AGENT", "interim_response": "Guardo lo storico..."})
    if stanza_multi:
        add(f"quali luci sono accese in {stanza_multi.lower()}?",
            {"intent": "SIMPLE_CHAT", "payload": {"api_call": "entity_discover",
             "params": {"room": stanza_multi, "domain": "light"}}})
    return E


def _esempi_coreference(b: M.Bersagli, mappa_righe) -> List[str]:
    """Due esempi su domini DIVERSI, per insegnare il meccanismo e non un nome."""
    tv = _migliore([d for d, dd in b.device_dominio.items()
                    if dd == "media_player" and d.lower().startswith("tv")])
    conta = {}
    for r in mappa_righe:
        if r.get("entity_type") == "light" and r.get("room"):
            conta[r["room"]] = conta.get(r["room"], 0) + 1
    interne = {s: n for s, n in conta.items()
               if not any(w in s.lower() for w in ("giardino", "esterno", "balcone", "piscina"))}
    stanza = max(interne or conta, key=(interne or conta).get) if conta else None
    out = []
    if tv:
        out.append(f'[RECENTE: "spegni {tv.lower()}" → "Fatto"]  "ora riaccendila" → '
                   + json.dumps({"intent": "HOME_CONTROL",
                                 "payload": {"entity": tv, "domain": "media_player",
                                             "action": "turn_on"}}, ensure_ascii=False,
                                separators=(",", ":")))
    if stanza:
        out.append(f'[RECENTE: "che temperatura c\'è in {stanza.lower()}?" → "21 gradi"]  '
                   f'"e l\'umidità?" → '
                   + json.dumps({"intent": "SIMPLE_CHAT",
                                 "payload": {"api_call": "entity_discover",
                                             "params": {"room": stanza, "search": "umidità",
                                                        "device_class": "humidity"}}},
                                ensure_ascii=False, separators=(",", ":")))
    return out


def render(location_id: str, mappa_righe: Optional[list] = None) -> str:
    if mappa_righe is None:
        from database import _get_conn
        conn = _get_conn(); c = conn.cursor()
        c.execute("""SELECT zone, area, room, entity_type, entity_name FROM entity_maps
                     WHERE location_id=? AND entity_id IS NOT NULL AND COALESCE(visible,1)=1
                       AND LOWER(COALESCE(room,''))!='sconosciuto'
                       AND LOWER(COALESCE(zone,''))!='non classificato'""", (location_id,))
        mappa_righe = [dict(r) for r in c.fetchall()]
        conn.close()
    mappa_righe = [r for r in mappa_righe if r.get("entity_type") in M.AZIONABILI]
    b = M.carica_bersagli(location_id, mappa_righe)

    # tabella "verbo parlato → azione, per dominio": è la vera difficoltà
    # ("apri" vale press sui button, turn_on sugli script, open_cover sulle cover)
    # e viene generata da normalizza_azione, la stessa funzione del resolver.
    verbi = [("apri / attiva / avvia / premi", "turn_on"),
             ("chiudi / spegni / disattiva", "turn_off")]
    righe_verbi = []
    for etichetta, base in verbi:
        mappa_v = {}
        for dom in ("light", "switch", "cover", "lock", "button", "script", "scene", "media_player"):
            mappa_v.setdefault(M.normalizza_azione(base, dom), []).append(dom)
        righe_verbi.append(f"  {etichetta} → " + ", ".join(
            f"{az} ({'/'.join(dd)})" for az, dd in mappa_v.items()))

    righe_dominio = []
    for dom in ("light", "switch", "cover", "climate", "media_player", "fan",
                "lock", "button", "script", "scene", "vacuum"):
        if dom not in M.DOMAINS:
            continue
        azioni = [a for a in M.DOMAINS[dom]]
        par = {p for a in azioni for p in M.ACTIONS[a].parametri}
        riga = f"- {dom}: " + "/".join(sorted(azioni))
        if par:
            riga += "  [par: " + ", ".join(sorted(par)) + "]"
        righe_dominio.append(riga)

    intents = "\n".join(f"- {k}: {v}" for k, v in M.INTENTS.items()
                        if k != "VERIFY_WITH_AI_AGENT")
    esempi = "\n".join(_esempi(b, location_id, mappa_righe))
    core = "\n".join(_esempi_coreference(b, mappa_righe))

    return f"""Sei JARVIS, router di un assistente domotico. Ricevi MAPPA ENTITÀ, CONTESTO e COMANDO.
Rispondi SOLO con un JSON, niente altro testo.

INTENT (scegline uno):
{intents}

BERSAGLIO DI UN COMANDO DOMOTICO — un solo campo, "entity".
La MAPPA ENTITÀ è annidata così: {{"zona": {{"stanza": {{"dominio": ["nomi dei dispositivi"]}}}}}}.
"entity" è UN nome copiato dalla mappa, e può essere:
- una CHIAVE (zona o stanza) oppure "ovunque" → il comando vale per tutto ciò che contiene.
  Si usa quando l'utente nomina un luogo o parla al plurale: "le luci della cucina",
  "luci soggiorno", "tutto in zona giorno", "spegni tutto".
- un NOME DENTRO UNA LISTA → il comando vale solo per quel dispositivo.
  Si usa quando l'utente nomina il dispositivo: "strip led", "tv cucina".
Regole dure:
- Copia il nome ESATTAMENTE come sta nella mappa: non comporlo, non unirne due,
  non aggiungere la stanza. Se il nome non è nella mappa, non esiste.
- Se l'utente nomina un LUOGO (o parla al plurale), entity è quel LUOGO, mai un
  dispositivo preso da dentro.
- "tutte/tutti/tutto" NON significa "ovunque": se accanto c'è un luogo, entity è
  QUEL luogo ("spegni tutte le luci del giardino" → entity "Giardino").
  Usa "ovunque" solo quando non è nominato nessun luogo ("spegni tutto").
- Se il nome che senti non compare nella mappa, NON scriverlo: scegli il nome
  della mappa che gli somiglia di più. Il parlato è spesso storpiato, la mappa no.

AZIONI AMMESSE PER DOMINIO (non inventarne altre):
{chr(10).join(righe_dominio)}

LO STESSO VERBO CAMBIA AZIONE A SECONDA DEL DOMINIO:
{chr(10).join(righe_verbi)}
  "metti/porta a N gradi" → set_temperature (climate), mai set_hvac_mode
  "metti/togli il muto" → volume_mute (media_player)

TESTO LIBERO: play_music vuole sempre parameters.query (cosa suonare, senza stanza né
verbi); web_search vuole params.query. Sono gli unici campi che scrivi liberamente.

DOMANDA, NON COMANDO: se la frase chiede un'informazione invece di ordinare un'azione
("quali/quante/quanti", "che stato", "è accesa/spenta/aperta", "che temperatura",
"quanto consuma", "cosa c'è in", "dimmi", "mi dici", "elenca", "mostrami") allora è
SIMPLE_CHAT, MAI HOME_CONTROL, anche se nomina luci o stanze.

DOMANDE SU CASA → SIMPLE_CHAT con api_call=entity_discover, params:
  room (come l'ha detta l'utente, mai espansa a una stanza più specifica),
  domain, search (testo libero), device_class (temperature|humidity|power|energy|motion|battery).
Dati di casa: sempre entity_discover, MAI web_search. web_search solo per fatti esterni
(meteo, notizie, "chi è X"). Saluti e chiacchiere: risposta diretta, nessuna api_call.
AI_AGENT quando serve uno strumento esterno o più di un passo. Segnali:
- email, posta, "scrivi a", rispondere a un messaggio
- calendario, agenda, appuntamenti, impegni, prenotazioni
- trading, portfolio, crypto, borsa
- dati di casa nel PASSATO o confrontati: "ieri", "stanotte", "questa settimana",
  "il mese scorso", "di solito", "perché", "com'è andato", "anomalo", "confronta",
  "trend", "analizza", "quante volte"
- musica: libreria e playlist personali, consigli ("consigliami"), ricerche precise
(MA: il valore ATTUALE di un sensore resta SIMPLE_CHAT con entity_discover.)

RIFERIMENTI AL TURNO PRECEDENTE ("ora accendila", "e l'umidità?"): risolvi il pronome
usando [CONVERSAZIONE RECENTE], che è l'unica fonte del riferimento. Non usare mai i
nomi che compaiono qui sotto negli esempi.

Location sconosciuta su un comando domotico → RETRY.

FORMATO (una sola di queste forme):
  domotica su un luogo   {{"intent":"HOME_CONTROL","confidence":0.95,"response":"...","payload":{{"location":"{location_id}","entity":"<zona o stanza>","domain":"...","action":"..."}}}}
  domotica su un device  {{"intent":"HOME_CONTROL","confidence":0.95,"response":"...","payload":{{"location":"{location_id}","entity":"<nome del dispositivo>","domain":"...","action":"..."}}}}
  domanda su casa        {{"intent":"SIMPLE_CHAT","confidence":0.9,"response":"...","payload":{{"api_call":"entity_discover","params":{{}}}}}}
  ricerca esterna        {{"intent":"SIMPLE_CHAT","confidence":0.9,"response":"...","payload":{{"api_call":"web_search","params":{{"query":"..."}}}}}}
  chiacchiera            {{"intent":"SIMPLE_CHAT","confidence":0.95,"response":"..."}}
  strumento esterno      {{"intent":"AI_AGENT","confidence":0.95,"interim_response":"..."}}

ESEMPI (nomi reali di questa casa):
{esempi}

{core}"""
