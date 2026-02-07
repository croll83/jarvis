# Prompt: Implementazione Multi-Location Home Assistant

## Obiettivo

Estendere JARVIS per supportare **multiple istanze Home Assistant** in location diverse (es: WAGMI Villa a Napoli, Albani20 a Milano), con routing intelligente basato sulla sorgente del comando.

## Contesto

- JARVIS gira su un server a Napoli (WAGMI Villa)
- Una seconda casa a Milano (Albani20) avrà il suo HA raggiungibile via Tailscale
- Gli AtomS3R saranno in entrambe le case
- Telegram e OpenClaw devono sapere quale HA usare
- La configurazione HA deve essere dinamica (database, non config.py)

---

## 1. Database Changes

### 1.1 Nuova tabella `locations`

```sql
CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,              -- "wagmi", "albani" (slug)
    name TEXT NOT NULL,               -- "WAGMI Villa", "Albani20"
    city TEXT,                        -- "Napoli", "Milano"
    hass_url TEXT NOT NULL,           -- "http://homeassistant:8123" o "http://100.x.x.x:8123"
    hass_token TEXT NOT NULL,         -- Long-lived access token
    entity_map_path TEXT,             -- "wagmi_ha_entities.json"
    has_security BOOLEAN DEFAULT FALSE, -- True solo per WAGMI (Frigate, DoubleTake)
    enabled BOOLEAN DEFAULT TRUE,     -- Per disabilitare temporaneamente
    created_at REAL DEFAULT (strftime('%s', 'now'))
);
```

### 1.2 Nuova tabella `user_locations`

```sql
CREATE TABLE IF NOT EXISTS user_locations (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    location_id TEXT REFERENCES locations(id),
    source TEXT,                      -- "voice", "telegram_sticky", "telegram_inline"
    updated_at REAL
);
```

### 1.3 Seed data iniziale

Inserire in `init_db()` le due location di default:

```python
# WAGMI Villa (locale)
INSERT OR IGNORE INTO locations (id, name, city, hass_url, hass_token, entity_map_path, has_security) 
VALUES ('wagmi', 'WAGMI Villa', 'Napoli', 'http://homeassistant:8123', '', 'wagmi_ha_entities.json', TRUE);

# Albani20 (via Tailscale) - token vuoto inizialmente
INSERT OR IGNORE INTO locations (id, name, city, hass_url, hass_token, entity_map_path, has_security) 
VALUES ('albani', 'Albani20', 'Milano', 'http://100.x.x.x:8123', '', 'albani_ha_entities.json', FALSE);
```

### 1.4 Funzioni database

In `database.py` aggiungere:

```python
def get_all_locations(enabled_only: bool = True) -> List[dict]:
    """Recupera tutte le location configurate."""

def get_location(location_id: str) -> Optional[dict]:
    """Recupera una location per ID."""

def create_location(id: str, name: str, city: str, hass_url: str, hass_token: str, 
                   entity_map_path: str, has_security: bool = False) -> bool:
    """Crea una nuova location."""

def update_location(location_id: str, **kwargs) -> bool:
    """Aggiorna campi di una location."""

def delete_location(location_id: str) -> bool:
    """Elimina una location (soft delete: enabled=False)."""

def get_user_location(user_id: int) -> Optional[dict]:
    """Recupera la location corrente di un utente."""

def set_user_location(user_id: int, location_id: str, source: str) -> bool:
    """Imposta la location corrente di un utente."""

def clear_user_location(user_id: int) -> bool:
    """Rimuove la location sticky di un utente."""
```

---

## 2. Multi-HA Client

### 2.1 Nuovo file `multi_ha.py`

Creare un client che gestisce multiple connessioni HA:

```python
class MultiHomeAssistant:
    """
    Gestisce connessioni a multiple istanze Home Assistant.
    Carica configurazione dal database.
    """
    
    def __init__(self):
        self.clients: Dict[str, HomeAssistantClient] = {}
        self.entity_maps: Dict[str, dict] = {}
        self._load_from_db()
    
    def _load_from_db(self):
        """Carica tutte le location abilitate dal DB."""
        locations = get_all_locations(enabled_only=True)
        for loc in locations:
            self.clients[loc['id']] = HomeAssistantClient(
                loc['hass_url'], 
                loc['hass_token']
            )
            # Carica entity map
            if loc['entity_map_path']:
                self.entity_maps[loc['id']] = self._load_entity_map(loc['entity_map_path'])
    
    def reload(self):
        """Ricarica configurazione dal DB (chiamato dopo modifiche da dashboard)."""
        self.clients.clear()
        self.entity_maps.clear()
        self._load_from_db()
    
    async def call_service(self, location_id: str, domain: str, service: str, data: dict) -> Tuple[bool, str]:
        """Chiama un servizio su una specifica location."""
        client = self.clients.get(location_id)
        if not client:
            return False, f"Location '{location_id}' non configurata o disabilitata"
        return await client.call_service(domain, service, data)
    
    def get_entity_map(self, location_id: str) -> dict:
        """Restituisce l'entity map per una location."""
        return self.entity_maps.get(location_id, {})
    
    def get_all_entity_maps(self) -> Dict[str, dict]:
        """Restituisce tutte le entity maps (per il router)."""
        return self.entity_maps

# Singleton
multi_ha = MultiHomeAssistant()
```

### 2.2 Aggiornare `integrations.py`

- Sostituire `ha_client` singleton con import da `multi_ha`
- Modificare `call_hass_service()` per accettare `location_id`:

```python
async def call_hass_service(location_id: str, domain: str, service: str, service_data: dict) -> Tuple[bool, str]:
    """Wrapper principale per chiamate HA con location."""
    return await multi_ha.call_service(location_id, domain, service, service_data)
```

- Aggiornare `speak()`, `play_sound()`, `quick_feedback()` per accettare location

---

## 3. Service Status Multi-Location

### 3.1 Modificare `service_status.py`

I servizi HA devono essere dinamici basati sulle location nel DB:

```python
class ServiceStatus:
    def __init__(self):
        self.services: Dict[str, ServiceHealth] = {
            "ollama_router": ServiceHealth(),
            "ollama_reasoning": ServiceHealth(),
            "whisper": ServiceHealth(),
            "openclaw": ServiceHealth(),
            # HA services aggiunti dinamicamente
        }
        self._load_ha_services()
    
    def _load_ha_services(self):
        """Carica servizi HA dal database."""
        locations = get_all_locations(enabled_only=True)
        for loc in locations:
            service_key = f"home_assistant_{loc['id']}"
            if service_key not in self.services:
                self.services[service_key] = ServiceHealth()
    
    def reload_ha_services(self):
        """Ricarica servizi HA (dopo modifiche location)."""
        # Rimuovi vecchi servizi HA
        self.services = {k: v for k, v in self.services.items() 
                        if not k.startswith("home_assistant_")}
        self._load_ha_services()
    
    async def _check_home_assistant(self, location_id: str, hass_url: str, hass_token: str):
        """Health check per una specifica istanza HA."""
        service_key = f"home_assistant_{location_id}"
        # ... implementazione check con hass_url e token
    
    def can_do_home_control(self, location_id: str) -> bool:
        """Verifica se HA per una location è disponibile."""
        service_key = f"home_assistant_{location_id}"
        ha = self.services.get(service_key)
        if not ha:
            return False
        return ha.state in [ServiceState.ONLINE, ServiceState.DEGRADED]
    
    def get_status_for_prompt(self, location_id: str = None) -> str:
        """
        Genera stringa per il router.
        Se location_id specificato, indica solo lo stato di quella location.
        """
        # ... aggiornare per mostrare stati per-location
```

---

## 4. Router Changes

### 4.1 Nuovo intent `SET_LOCATION`

Aggiungere al system prompt (`router_system_prompt.txt`):

```
• SET_LOCATION - Imposta posizione utente ("sono a Milano", "sono a Napoli")
  payload: { "location": "albani" | "wagmi" | "reset" }
```

### 4.2 Context Multi-Location

Il contesto passato al router deve includere:

```python
context = {
    "source": "Telegram",
    "speaker_id": 1,
    "speaker_name": "Marco",
    "is_admin": True,
    "room": "Unknown",           # Per Telegram
    "location": "wagmi",         # Location corrente utente (o "unknown")
    "available_locations": ["wagmi", "albani"],  # Per parsing
}
```

### 4.3 Entity Maps nel Prompt

Il prompt deve includere le entity maps di TUTTE le location:

```
[MAPPA ENTITÀ WAGMI]:
{ ... }

[MAPPA ENTITÀ ALBANI]:
{ ... }

Se il comando specifica una location ("a Milano", "a Napoli", "a WAGMI", "a casa", "qui"), 
usa quella location per il payload.
Se non specificata, usa la location dal [CONTESTO].
Se location="unknown" e intent=HOME_CONTROL, imposta intent="RETRY" e chiedi "In quale casa?".
```

### 4.4 Payload con Location

Output router deve includere location:

```json
{
  "intent": "HOME_CONTROL",
  "confidence": 0.95,
  "payload": {
    "location": "wagmi",
    "domain": "light",
    "entity": "Luci Salotto",
    "action": "turn_on"
  }
}
```

### 4.5 Parsing Location Keywords

Il router deve riconoscere:
- "a Milano", "Milano", "Albani", "Albani20" → `albani`
- "a Napoli", "Napoli", "WAGMI", "villa" → `wagmi`
- "qui", "questa casa" → usa context.location
- "l'altra casa", "a casa" (se sei fuori) → inferisci

---

## 5. Main.py Changes

### 5.1 Estrazione Location da Device ID

```python
def extract_location_from_device(device_id: str) -> Optional[str]:
    """
    Estrae location da device_id.
    atoms3r_wagmi_salotto → wagmi
    atoms3r_albani_camera → albani
    """
    if not device_id:
        return None
    parts = device_id.split('_')
    if len(parts) >= 2:
        return parts[1]  # wagmi, albani
    return None
```

### 5.2 Voice Endpoint

```python
@app.post("/voice_stream")
async def voice_stream(request: Request, device_id: str = Form(...), room: str = Form(...)):
    # Estrai location dal device
    location = extract_location_from_device(device_id)
    
    context = {
        "source": "AtomS3R",
        "device_id": device_id,
        "room": room,
        "location": location,  # Da device ID
        ...
    }
```

### 5.3 Telegram Location Resolution

Creare funzione per risolvere location per Telegram:

```python
async def resolve_telegram_location(user_id: int, text: str, router_data: dict) -> Tuple[str, bool]:
    """
    Risolve la location per un comando Telegram.
    
    Returns:
        (location_id, needs_keyboard): location risolta o None, True se serve keyboard
    """
    # 1. Check se il router ha parsato una location esplicita
    payload_location = router_data.get("payload", {}).get("location")
    if payload_location and payload_location != "unknown":
        # Aggiorna user location (inline)
        set_user_location(user_id, payload_location, "telegram_inline")
        return payload_location, False
    
    # 2. Check sticky location
    user_loc = get_user_location(user_id)
    if user_loc and user_loc.get("source") == "telegram_sticky":
        return user_loc["location_id"], False
    
    # 3. Nessuna location → serve keyboard
    return None, True
```

### 5.4 Telegram Keyboard

```python
async def send_location_keyboard(chat_id: str, original_text: str, action_context: dict):
    """Invia keyboard inline per selezione location."""
    locations = get_all_locations(enabled_only=True)
    
    keyboard = {
        "inline_keyboard": [[
            {"text": f"🏠 {loc['name']}", "callback_data": f"loc:{loc['id']}:{action_context_id}"}
            for loc in locations
        ]]
    }
    
    await send_telegram(
        "In quale casa vuoi eseguire questo comando?",
        reply_markup=keyboard
    )
```

### 5.5 Handler SET_LOCATION

```python
elif intent == "SET_LOCATION":
    payload = router_data.get("payload", {})
    new_location = payload.get("location")
    
    if new_location == "reset":
        clear_user_location(speaker_id)
        response = "Ho rimosso la tua posizione. La prossima volta ti chiederò dove sei."
    else:
        location = get_location(new_location)
        if location:
            set_user_location(speaker_id, new_location, "telegram_sticky")
            response = f"Perfetto! Ho impostato {location['name']} come tua posizione attuale."
        else:
            response = f"Non conosco la location '{new_location}'."
    
    await deliver_final_response(response, context, sound_type="positive")
```

### 5.6 Aggiornare HOME_CONTROL Handler

```python
elif intent == "HOME_CONTROL" and conf >= conf_high:
    payload = router_data.get("payload", {})
    location = payload.get("location") or context.get("location")
    
    # Per Telegram: risolvi location se mancante
    if source == "Telegram" and not location:
        location, needs_keyboard = await resolve_telegram_location(speaker_id, text, router_data)
        if needs_keyboard:
            await send_location_keyboard(chat_id, text, {"intent": intent, "payload": payload})
            return  # Aspetta callback
    
    # Check se HA per questa location è disponibile
    if not service_status.can_do_home_control(location):
        response = f"Home Assistant per {location} non è raggiungibile."
        await deliver_final_response(response, context, sound_type="negative")
        return
    
    # Esegui comando
    success, err = await call_hass_service(location, domain, action, service_data)
    # ... resto della logica
```

---

## 6. Admin Dashboard

### 6.1 Nuova Tab "Locations"

Aggiungere tab nella dashboard per gestire le location:

**Vista lista:**
- Tabella con: ID, Nome, Città, URL, Status (LED), Azioni
- Bottone "Aggiungi Location"

**Form creazione/modifica:**
- ID (slug, readonly se edit)
- Nome
- Città
- Home Assistant URL
- Token (password field)
- Entity Map Path
- Has Security (checkbox)
- Enabled (checkbox)

**Azioni:**
- Edit
- Test Connection (chiama health check)
- Disable/Enable
- Delete (con conferma)

### 6.2 API Endpoints

In `admin_api.py` aggiungere:

```python
@router.get("/locations")
async def list_locations() -> List[dict]:
    """Lista tutte le location."""

@router.post("/locations")
async def create_location(data: LocationCreate) -> dict:
    """Crea una nuova location."""

@router.get("/locations/{location_id}")
async def get_location_detail(location_id: str) -> dict:
    """Dettaglio location con status."""

@router.put("/locations/{location_id}")
async def update_location(location_id: str, data: LocationUpdate) -> dict:
    """Aggiorna una location."""

@router.delete("/locations/{location_id}")
async def delete_location(location_id: str) -> dict:
    """Disabilita una location."""

@router.post("/locations/{location_id}/test")
async def test_location_connection(location_id: str) -> dict:
    """Testa connessione a HA per questa location."""

@router.post("/locations/reload")
async def reload_locations() -> dict:
    """Ricarica configurazione location (dopo modifiche)."""
    multi_ha.reload()
    service_status.reload_ha_services()
    return {"status": "reloaded"}
```

### 6.3 Dashboard Health Update

La sezione health deve mostrare:
- Servizi core (Ollama, Whisper, ecc.)
- **Per ogni location**: LED con stato HA

```
┌─────────────────────────────────────────────────────────┐
│ STATO SERVIZI                                           │
├─────────────────────────────────────────────────────────┤
│ ● Orchestrator    ● Ollama Router    ● Whisper         │
│ ● Gemini          ● OpenClaw                            │
├─────────────────────────────────────────────────────────┤
│ HOME ASSISTANT                                          │
│ ● WAGMI Villa (Napoli)     45ms                        │
│ ● Albani20 (Milano)        120ms                       │
└─────────────────────────────────────────────────────────┘
```

---

## 7. Config.py Cleanup

### 7.1 Rimuovere

```python
# RIMUOVERE - ora in database
HASS_URL = ...
HASS_TOKEN = ...
```

### 7.2 Mantenere

```python
# Defaults per nuove location (usati solo se non specificato)
DEFAULT_HASS_PORT = 8123

# Entity map directory
ENTITY_MAPS_DIR = BASE_DIR / "../config"
```

### 7.3 Aggiungere

```python
# Location keywords per parsing
LOCATION_KEYWORDS = {
    "wagmi": ["wagmi", "villa", "napoli", "naples"],
    "albani": ["albani", "albani20", "milano", "milan"],
}
```

---

## 8. Entity Maps

### 8.1 Struttura File

```
config/
├── wagmi_ha_entities.json      # Esistente, rinominare se necessario
├── albani_ha_entities.json     # Nuovo, da creare
```

### 8.2 Template Albani

Creare `albani_ha_entities.json` con struttura simile a WAGMI ma per l'appartamento Milano:

```json
{
  "Interno": {
    "Salotto": {
      "Luci": ["light.salotto"],
      "TV": ["media_player.samsung_tv_salotto"]
    },
    "Cucina": {
      "Luci": ["light.cucina"]
    },
    "Camera": {
      "Luci": ["light.camera"]
    },
    "Cameretta": {
      "Luci": ["light.cameretta"]
    },
    "Studio": {
      "Luci": ["light.studio"]
    }
  },
  "Dispositivi": {
    "Roomba": ["vacuum.roomba", "vacuum.brava"],
    "Garage": ["cover.meros_garage"]
  },
  "Speakers": {
    "salotto": "media_player.bose_salotto",
    "camera": "media_player.echo_camera",
    "cameretta": "media_player.echo_cameretta",
    "studio": "media_player.echo_studio",
    "cucina": "media_player.echo_cucina"
  }
}
```

---

## 9. Testing Checklist

### Tailscale Network
- [ ] Container Tailscale avviato e connesso
- [ ] `docker exec jarvis-tailscale tailscale status` mostra nodi
- [ ] `docker exec jarvis-tailscale tailscale ping ha-albani` funziona
- [ ] Orchestrator può risolvere hostname Tailscale (MagicDNS)
- [ ] Latenza accettabile (<200ms) verso HA remoto

### Voice (AtomS3R)
- [ ] Comando da `atoms3r_wagmi_salotto` → va a HA WAGMI
- [ ] Comando da `atoms3r_albani_salotto` → va a HA Albani
- [ ] Location corrente aggiornata dopo comando voice
- [ ] Audio feedback funziona su entrambe le location

### Telegram
- [ ] "Accendi le luci" senza sticky → mostra keyboard
- [ ] Click su keyboard → esegue + imposta sticky
- [ ] "Accendi le luci" con sticky → usa sticky location
- [ ] "Accendi le luci a Milano" → override, va ad Albani
- [ ] "Sono a Napoli" → imposta sticky WAGMI
- [ ] "Dove sono?" → risponde location corrente
- [ ] "Reset posizione" → rimuove sticky

### Cross-Location
- [ ] Da Milano (sticky): "Accendi le luci a Napoli" → va a WAGMI
- [ ] Da Napoli (sticky): "Spegni tutto a Milano" → va ad Albani
- [ ] Controllo remoto funziona con Tailscale attivo

### Graceful Degradation
- [ ] HA Albani offline (Tailscale down) → messaggio chiaro, WAGMI funziona
- [ ] HA WAGMI offline → messaggio chiaro, Albani funziona
- [ ] Entrambi offline → messaggio appropriato
- [ ] Tailscale container restart → riconnessione automatica
- [ ] Health check mostra stato corretto per-location

### Dashboard
- [ ] Visualizza tutte le location con status
- [ ] Crea nuova location
- [ ] Test connessione (locale e remota)
- [ ] Modifica token (mascherato)
- [ ] Disabilita location → sparisce da routing
- [ ] Riabilita location → riappare
- [ ] Health mostra LED separati per ogni HA
- [ ] Reload config senza restart

---

## 10. Migration Notes

### Ordine Implementazione Suggerito

**Fase 1: Infrastruttura**
1. **Docker Compose** - Aggiungi servizio Tailscale
2. **Tailscale Setup** - Auth key, test connessione
3. **Database** - Tabelle locations + user_locations + funzioni

**Fase 2: Backend Core**
4. **multi_ha.py** - Nuova classe MultiHomeAssistant
5. **Service Status** - Aggiornamento per multi-location health
6. **Config cleanup** - Rimuovi HASS_URL/TOKEN, aggiungi LOCATION_KEYWORDS
7. **integrations.py** - Aggiorna call_hass_service per accettare location

**Fase 3: Router & Logic**
8. **Router prompt** - Intent SET_LOCATION + payload location + multi entity maps
9. **Main.py** - Handler updates, location resolution, Telegram keyboard

**Fase 4: Admin UI**
10. **Admin API** - Endpoints locations CRUD
11. **Dashboard** - Tab Locations con form e test connection

**Fase 5: Documentation & Config**
12. **Entity Map Albani** - Creare albani_ha_entities.json
13. **README principale** - Sezione Multi-Location, architettura, network setup
14. **README Orchestrator** - Location context resolution, config migration

**Fase 6: Testing**
15. **Test Voice** - Comandi da entrambe le location
16. **Test Telegram** - Sticky, inline, keyboard
17. **Test Cross-Location** - Controllo remoto
18. **Test Degradation** - HA offline per-location

### Backward Compatibility

- Se nessuna location nel DB → crea automaticamente "wagmi" con env vars legacy
- Entity map mancante → warning, usa mappa vuota
- Token vuoto → location disabilitata automaticamente
- Tailscale down → solo location locali funzionano (graceful degradation)

---

---

## 11. Tailscale Setup

### 11.1 Architettura Network

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TAILSCALE MESH NETWORK                             │
│                          (100.x.x.x private IPs)                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   NAPOLI (WAGMI Villa)                      MILANO (Albani20)               │
│   ┌─────────────────────┐                   ┌─────────────────────┐         │
│   │  Proxmox (AtomMan)  │                   │  Mini PC / NUC      │         │
│   │  ┌───────────────┐  │                   │  ┌───────────────┐  │         │
│   │  │ LXC Docker    │  │                   │  │ Home Assistant│  │         │
│   │  │ ┌───────────┐ │  │                   │  │    (HAOS)     │  │         │
│   │  │ │ Tailscale │◀┼──┼───── Tunnel ──────┼──▶    +          │  │         │
│   │  │ │ Container │ │  │                   │  │  Tailscale    │  │         │
│   │  │ └───────────┘ │  │                   │  │  (add-on)     │  │         │
│   │  │       │       │  │                   │  └───────────────┘  │         │
│   │  │       ▼       │  │                   │         │           │         │
│   │  │ ┌───────────┐ │  │                   │         │           │         │
│   │  │ │Orchestrator│ │  │                   │         ▼           │         │
│   │  │ └───────────┘ │  │                   │  ┌───────────────┐  │         │
│   │  │       │       │  │                   │  │   AtomS3R     │  │         │
│   │  │       ▼       │  │                   │  │   Devices     │  │         │
│   │  │ ┌───────────┐ │  │                   │  └───────────────┘  │         │
│   │  │ │  HA WAGMI │ │  │                   │         │           │         │
│   │  │ │  (locale) │ │  │                   │         ▼           │         │
│   │  │ └───────────┘ │  │                   │  ┌───────────────┐  │         │
│   │  └───────────────┘  │                   │  │  Echo/Bose    │  │         │
│   └─────────────────────┘                   │  │  Speakers     │  │         │
│                                             │  └───────────────┘  │         │
│                                             └─────────────────────┘         │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.2 Perché Tailscale

- **NAT Traversal**: Starlink e router consumer hanno NAT complessi, Tailscale li bypassa
- **Zero Config Firewall**: Nessuna porta da aprire
- **Mesh Network**: Ogni nodo può parlare con ogni altro nodo direttamente
- **MagicDNS**: Nomi come `ha-albani.tailnet-xxx.ts.net` invece di IP
- **ACL**: Controllo granulare su chi può accedere a cosa

### 11.3 Setup Milano (Albani20)

**Opzione A: HAOS con Tailscale Add-on (Consigliato)**

1. Installa add-on Tailscale da Add-on Store
2. Configura con auth key da Tailscale admin console
3. Abilita "Expose Home Assistant" nelle opzioni add-on
4. HA sarà raggiungibile come `ha-albani.tailnet-xxx.ts.net:8123`

**Opzione B: Tailscale su host + HA in Docker**

```bash
# Su mini PC Milano
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --authkey=tskey-xxx --hostname=ha-albani
```

### 11.4 Setup Napoli (WAGMI) - Container Dedicato

Creare un container Tailscale che fa da gateway per tutta la rete Docker.

**Vantaggi:**
- Tutti i container possono raggiungere la rete Tailscale
- Isolamento: solo Tailscale container ha accesso alla VPN
- Facile da aggiornare/riavviare senza toccare altri servizi

---

## 12. Docker Compose Updates

### 12.1 Nuovo Servizio Tailscale

Aggiungere a `docker-compose.yml`:

```yaml
services:
  # ... altri servizi esistenti ...

  # ============================================
  # TAILSCALE - VPN Mesh per Multi-Location
  # ============================================
  tailscale:
    image: tailscale/tailscale:latest
    container_name: jarvis-tailscale
    hostname: jarvis-wagmi
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    volumes:
      - tailscale-data:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    environment:
      - TS_AUTHKEY=${TAILSCALE_AUTHKEY}
      - TS_STATE_DIR=/var/lib/tailscale
      - TS_USERSPACE=false
      - TS_ACCEPT_DNS=true
      # Subnet routing per permettere accesso alla rete Docker
      - TS_EXTRA_ARGS=--advertise-routes=172.20.0.0/16 --accept-routes
    networks:
      jarvis-net:
        ipv4_address: 172.20.0.250  # IP fisso per Tailscale gateway

  # ============================================
  # ORCHESTRATOR (aggiornato)
  # ============================================
  orchestrator:
    build: ./Orchestrator
    container_name: jarvis-orchestrator
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - OLLAMA_URL=http://ollama:11434
      - WHISPER_URL=http://whisper:9000
      # HA URLs ora vengono dal database, non più da env
      # Ma manteniamo per backward compatibility / fallback
      - HASS_URL_DEFAULT=http://homeassistant:8123
      - OPENCLAW_URL=http://openclaw:18789
    volumes:
      - ./data:/app/data
      - ./voice_models:/app/voice_models
      - ./config:/app/config:ro
    depends_on:
      - ollama
      - whisper
      - tailscale  # Dipende da Tailscale per raggiungere HA remoti
    networks:
      - jarvis-net
    # Usa Tailscale come DNS per risolvere nomi .ts.net
    dns:
      - 100.100.100.100  # Tailscale MagicDNS

volumes:
  tailscale-data:
    name: jarvis-tailscale-data

networks:
  jarvis-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16
```

### 12.2 Environment Variables

Aggiungere a `.env`:

```bash
# Tailscale
TAILSCALE_AUTHKEY=tskey-auth-xxxxx  # Genera da admin.tailscale.com

# Tailscale hostnames per le location (MagicDNS)
# Questi sono esempi - usa i tuoi hostname reali
TS_HOSTNAME_WAGMI=homeassistant-wagmi
TS_HOSTNAME_ALBANI=ha-albani
```

### 12.3 Docker Network Considerations

```yaml
# Se HA WAGMI gira in un container separato (non HAOS VM):
homeassistant:
  # ... config esistente ...
  networks:
    jarvis-net:
      ipv4_address: 172.20.0.10
  # Esponi anche su Tailscale se necessario
  # (utile per accesso da app mobile fuori casa)
```

### 12.4 Healthcheck per Tailscale

```yaml
tailscale:
  # ... config sopra ...
  healthcheck:
    test: ["CMD", "tailscale", "status", "--json"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 30s
```

---

## 13. README Principale Updates

### 13.1 Nuova Sezione: Multi-Location Architecture

Aggiungere dopo la sezione "Infrastruttura Docker":

```markdown
## Multi-Location Support

JARVIS supporta il controllo di **multiple abitazioni** con istanze Home Assistant separate, connesse via Tailscale VPN mesh.

### Architettura Multi-Casa

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              JARVIS AI CORE                                  │
│                           (AtomMan G7 - Napoli)                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         ORCHESTRATOR                                 │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │   │
│  │  │  Qwen 7B │  │ OpenClaw │  │ Whisper  │  │   Multi-HA       │   │   │
│  │  │  Router  │  │ + Gemini │  │   STT    │  │   Client         │   │   │
│  │  └──────────┘  └──────────┘  └──────────┘  └────────┬─────────┘   │   │
│  │                                                      │             │   │
│  │                              ┌───────────────────────┴──────┐      │   │
│  │                              │                              │      │   │
│  └──────────────────────────────┼──────────────────────────────┼──────┘   │
│                                 │                              │          │
│                    ┌────────────▼────────────┐    ┌────────────▼────────┐ │
│                    │     HA WAGMI            │    │     TAILSCALE       │ │
│                    │     (locale)            │    │     Gateway         │ │
│                    │     172.20.0.10         │    │     172.20.0.250    │ │
│                    └────────────┬────────────┘    └────────────┬────────┘ │
└─────────────────────────────────┼──────────────────────────────┼──────────┘
                                  │                              │
                    ┌─────────────▼─────────────┐                │
                    │   WAGMI Villa (Napoli)    │                │
                    │   • BTicino MyHome        │                │
                    │   • Philips Hue           │     Tailscale  │
                    │   • Frigate + Cameras     │      Tunnel    │
                    │   • AtomS3R (6 stanze)    │     100.x.x.x  │
                    │   • Echo/Bose speakers    │                │
                    └───────────────────────────┘                │
                                                                 │
                                                    ┌────────────▼────────┐
                                                    │  HA ALBANI          │
                                                    │  (via Tailscale)    │
                                                    │  100.x.x.y:8123     │
                                                    └────────────┬────────┘
                                                                 │
                                                    ┌────────────▼────────┐
                                                    │ Albani20 (Milano)   │
                                                    │ • BTicino           │
                                                    │ • Philips Hue       │
                                                    │ • AtomS3R (5 stanze)│
                                                    │ • Echo/Bose         │
                                                    │ • Roomba/Brava      │
                                                    │ • Meros Garage      │
                                                    └─────────────────────┘
```

### Location Routing

JARVIS determina automaticamente quale Home Assistant usare:

| Sorgente | Metodo | Esempio |
|----------|--------|---------|
| **Voce (AtomS3R)** | Device ID | `atoms3r_wagmi_salotto` → HA WAGMI |
| **Telegram** | Sticky + Inline + Keyboard | "Accendi a Milano" → HA Albani |
| **OpenClaw** | Task context | "Prenota ristorante Milano" → location Milano |

### Comandi Location Telegram

```
"Jarvis, sono a Milano"     → Imposta Milano come default
"Jarvis, sono a Napoli"     → Imposta Napoli come default
"Accendi le luci a Milano"  → Override temporaneo
"Dove sono?"                → Mostra location corrente
"Reset posizione"           → Rimuove default, chiederà ogni volta
```

### Configurazione Location

Le location sono gestite dinamicamente via **Admin Dashboard** (`/admin` → Locations):
- Aggiungi/rimuovi location senza modificare codice
- Test connessione integrato
- Health monitoring per-location
- Token HA mascherati per sicurezza

### Setup Nuova Location

1. Installa Home Assistant nella nuova location
2. Configura Tailscale (add-on HAOS o standalone)
3. Crea Long-Lived Access Token in HA
4. Aggiungi location da Dashboard JARVIS
5. Crea entity map JSON per la nuova location
6. Installa AtomS3R con device_id appropriati (`atoms3r_{location}_{room}`)
```

### 13.2 Aggiornare Sezione Docker

Aggiungere Tailscale alla tabella container:

```markdown
| Container | Funzione | GPU? |
|-----------|----------|------|
| **tailscale** | VPN mesh multi-location | ❌ |
| **ollama** | Qwen 7B (router) | ✅ |
| ... resto tabella ...
```

### 13.3 Aggiornare Sezione Credenziali

```markdown
## Credenziali (Vault)

> ⚠️ Spostare in `.env` prima del deploy!

- Telegram Bot: @wagmivilla_bot
- Google Cloud Project: jarvis-wagmi
- Brave Search API: tier Free AI
- **Tailscale Auth Key**: tskey-auth-xxx (genera da admin.tailscale.com)
- **HA Tokens**: Gestiti in database via Dashboard (non in .env)
```

### 13.4 Nuova Sezione: Network Setup

```markdown
## Network Setup

### Tailscale VPN

JARVIS usa Tailscale per connettere location geograficamente distribuite senza aprire porte o configurare NAT.

**Nodi Tailscale:**
| Nodo | Hostname | Ruolo |
|------|----------|-------|
| AtomMan G7 (Napoli) | `jarvis-wagmi` | Gateway Docker, Orchestrator |
| HA Milano | `ha-albani` | Home Assistant Albani20 |

**Setup:**
1. Crea account Tailscale (free per uso personale)
2. Genera Auth Key (Settings → Keys → Generate auth key)
3. Aggiungi a `.env`: `TAILSCALE_AUTHKEY=tskey-auth-xxx`
4. `docker-compose up -d tailscale`
5. Verifica: `docker exec jarvis-tailscale tailscale status`

**Troubleshooting:**
- `tailscale ping ha-albani` - Verifica raggiungibilità
- `tailscale status` - Lista nodi connessi
- Logs: `docker logs jarvis-tailscale`
```

---

## 14. Orchestrator README Updates

### 14.1 Aggiornare Sezione Config

Sostituire la sezione configurazione con:

```markdown
## Configurazione

### Configurazione Statica (config.py)

Parametri che raramente cambiano:

```python
# Modelli AI
ROUTER_MODEL = "qwen2.5:7b-instruct-q4_K_M"

# Memory limits
ROUTER_MEMORY_HIGH_PRIORITY = 15
# ... etc

# Location keywords per parsing comandi
LOCATION_KEYWORDS = {
    "wagmi": ["wagmi", "villa", "napoli"],
    "albani": ["albani", "albani20", "milano"],
}
```

### Configurazione Dinamica (Database)

Parametri gestibili da Dashboard senza restart:

| Tabella | Contenuto |
|---------|-----------|
| `locations` | URL, token, entity map per ogni Home Assistant |
| `global_preferences` | Soglie confidence, suoni, DND mode |
| `user_preferences` | Preferenze per-utente |
| `user_locations` | Location corrente sticky per utente |

### Migration da config.py a Database

**Prima (deprecato):**
```python
# config.py
HASS_URL = "http://homeassistant:8123"
HASS_TOKEN = "eyJ0eXAiOiJKV1..."
```

**Dopo:**
```sql
-- Database locations table
INSERT INTO locations (id, name, hass_url, hass_token, ...)
VALUES ('wagmi', 'WAGMI Villa', 'http://homeassistant:8123', 'eyJ0eXAi...', ...);
```

**Vantaggi:**
- Aggiungi location senza modificare codice
- Token non in file (più sicuro)
- Hot-reload configurazione
- UI per gestione
```

### 14.2 Nuova Sezione: Location Context Resolution

```markdown
## Location Context Resolution

### Flusso Determinazione Location

```
┌─────────────────────────────────────────────────────────────────────┐
│                    LOCATION RESOLUTION                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  VOICE INPUT (AtomS3R)                                              │
│  ════════════════════                                                │
│  device_id = "atoms3r_wagmi_salotto"                                │
│       │                                                              │
│       └──▶ extract_location_from_device() ──▶ location = "wagmi"    │
│                                                                      │
│  ───────────────────────────────────────────────────────────────    │
│                                                                      │
│  TELEGRAM INPUT                                                      │
│  ══════════════                                                      │
│  "Accendi le luci del salotto"                                      │
│       │                                                              │
│       ▼                                                              │
│  1. Router parsa location esplicita nel testo?                      │
│     "Accendi a Milano" ──▶ payload.location = "albani" ──▶ USA      │
│       │                                                              │
│       ▼ No                                                           │
│  2. User ha sticky location? (user_locations table)                 │
│     source = "telegram_sticky" ──▶ USA                              │
│       │                                                              │
│       ▼ No                                                           │
│  3. Mostra Telegram Keyboard                                        │
│     [🏠 WAGMI Villa]  [🏢 Albani20]                                 │
│       │                                                              │
│       └──▶ User clicca ──▶ Esegui + Imposta sticky                  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Context Injection nel Router

Il contesto passato a Qwen include:

```python
context = {
    "source": "Telegram",          # o "AtomS3R"
    "speaker_id": 1,
    "speaker_name": "Marco",
    "room": "salotto",             # Da device_id o "Unknown"
    "location": "wagmi",           # Location risolta o "unknown"
    "available_locations": [       # Per parsing keywords
        {"id": "wagmi", "name": "WAGMI Villa", "keywords": ["napoli", "villa"]},
        {"id": "albani", "name": "Albani20", "keywords": ["milano", "albani"]}
    ]
}
```

### Entity Maps Multi-Location

Il router riceve TUTTE le entity maps nel prompt:

```
[MAPPA ENTITÀ WAGMI]:
{ "Interno": { "Salotto": { "Luci": ["light.salotto"] } } }

[MAPPA ENTITÀ ALBANI]:
{ "Interno": { "Salotto": { "Luci": ["light.salotto"] } } }

REGOLE:
- Se comando contiene keyword location, usa quella
- Se non specificato, usa location dal CONTESTO
- Se location="unknown", intent="RETRY", chiedi "In quale casa?"
```

### User Location Tracking

```sql
-- Tabella user_locations
user_id | location_id | source           | updated_at
--------|-------------|------------------|--------------------
1       | wagmi       | voice            | 2024-01-15 10:30:00
1       | albani      | telegram_sticky  | 2024-01-15 14:00:00

-- source types:
-- "voice"           = Ultimo comando vocale da AtomS3R
-- "telegram_sticky" = Comando esplicito "sono a X"
-- "telegram_inline" = Override nel comando "accendi a X"
```
```

### 14.3 Aggiornare API Reference

Aggiungere alla sezione API:

```markdown
### Location API (`/api/admin/locations`)

| Endpoint | Method | Descrizione |
|----------|--------|-------------|
| `/` | GET | Lista tutte le location |
| `/` | POST | Crea nuova location |
| `/{id}` | GET | Dettaglio location + status |
| `/{id}` | PUT | Modifica location |
| `/{id}` | DELETE | Disabilita location |
| `/{id}/test` | POST | Test connessione HA |
| `/reload` | POST | Ricarica config da DB |
```

### 14.4 Aggiornare Diagramma Architettura

```markdown
## Architettura

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           JARVIS ORCHESTRATOR                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │   main.py   │    │ ai_engines  │    │  multi_ha   │    │  database   │  │
│  │  FastAPI +  │───▶│  Qwen 3B    │    │  N x HA     │◀───│   SQLite    │  │
│  │  Endpoints  │    │ Qwen 7B    │    │  Clients    │    │  locations  │  │
│  └─────────────┘    └─────────────┘    └──────┬──────┘    └─────────────┘  │
│         │                                     │                             │
│         │          ┌─────────────┐    ┌──────▼──────┐                      │
│         ├─────────▶│  security   │    │   Tailscale │                      │
│         │          │  Privacy    │    │   Gateway   │                      │
│         │          │  Approval   │    │ 100.x.x.x   │                      │
│         │          └─────────────┘    └──────┬──────┘                      │
│         │                                    │                              │
│         │          ┌─────────────┐    ┌──────▼──────┐    ┌─────────────┐   │
│         │          │service_stat │    │  HA WAGMI   │    │  HA ALBANI  │   │
│         └─────────▶│  per-loc    │    │  (locale)   │    │ (Tailscale) │   │
│                    │  health     │    └─────────────┘    └─────────────┘   │
│                    └─────────────┘                                          │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```
```

---

## 15. Dockerfile Updates

### 15.1 Orchestrator Dockerfile

Aggiungere dipendenze per Tailscale DNS resolution:

```dockerfile
FROM python:3.11-slim

# ... existing setup ...

# Installa strumenti network per debug Tailscale
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    dnsutils \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# ... rest of Dockerfile ...
```

### 15.2 .dockerignore

Assicurarsi che i token non finiscano nell'immagine:

```
.env
*.env
**/*.token
**/secrets/
tailscale-data/
```

---

## Note Finali

- I token HA sono sensibili: non loggarli mai, mascherarli in dashboard
- Tailscale può avere latenza variabile: timeout più alti per location remote (default 10s → 15s per remote)
- Considera retry automatico per location remote in caso di timeout singolo
- L'entity map Albani andrà popolata man mano che configuri HA Milano
- MagicDNS: usa hostname Tailscale (`ha-albani.tailnet-xxx.ts.net`) invece di IP per resilienza
- Tailscale auth key: usa reusable + ephemeral per container, non-ephemeral per dispositivi fissi
