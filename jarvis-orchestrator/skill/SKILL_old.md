---
name: jarvis-orchestrator
description: Smart home control, memory, speaker ID, and security for multi-location Home Assistant
user-invocable: true
---

# JARVIS Orchestrator

Control smart home devices, query memory, and manage security across multiple Home Assistant locations.
All commands go through the JARVIS REST API.

## When to use

- User wants to control smart home devices (lights, covers, climate, locks)
- User asks about temperatures, entity states, or locations
- User asks about past conversations or personal facts
- User needs security actions (privacy mode, alarm)
- User asks "what can you do at home" or similar

## Steps

All API calls use the JARVIS orchestrator at `$JARVIS_ORCHESTRATOR_URL` (default: `http://localhost:5000`).
Always include the auth header: `Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN`

### 1. Control a device

Use this when the user says things like "accendi la luce", "chiudi le tapparelle", "alza il riscaldamento".

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/home_control" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "luce soggiorno",
    "action": "turn_on",
    "parameters": {"brightness": 200},
    "location_id": "wagmi",
    "source_channel": "openclaw_telegram"
  }'
```

- `entity_name`: friendly name ("luce soggiorno") or entity_id ("light.soggiorno") — auto-resolved
- `action`: "turn_on", "turn_off", "toggle", "open_cover", "close_cover", "set_temperature", etc.
- `parameters`: optional, depends on domain (brightness, temperature, position, etc.)
- `location_id`: optional, auto-resolved from user context if omitted
- `source_channel`: always use "openclaw_telegram" for Telegram messages

Security levels are enforced automatically:
- L1 (lights, sensors, switches): always allowed
- L2 (covers, climate, fans): allowed with context check
- L3 (locks, alarm, cameras): requires confirmation via separate approval bot
- L4 (email/unknown): always blocked

### 2. Resolve an entity name (with capabilities)

**Always call this FIRST before home_control.** It returns the entity_id, current state, and all available services with their parameters.

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/entity_resolve" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"friendly_name": "luce cucina", "location_id": "albani"}'
```

Response example:
```json
{
  "found": true,
  "entity_id": "light.cucina",
  "domain": "light",
  "friendly_name": "Luce Cucina",
  "area": "Piano 1 > Zona Giorno > Cucina",
  "state": "off",
  "device_class": null,
  "available_services": ["turn_on", "turn_off", "toggle"],
  "service_params": {
    "turn_on": {
      "brightness": "0-255 (luminosità)",
      "color_temp_kelvin": "2000-6500 (temperatura colore)",
      "transition": "secondi di transizione"
    }
  }
}
```

Use `available_services` to know which `action` to pass to `home_control`.
Use `service_params` to know which `parameters` are supported for each action.
Use `state` to know the current state before acting (e.g., don't turn_on if already "on").

### 2b. Discover entities (search/browse)

**Use this to explore what's available.** Search by room, domain, floor, zone, or free text.
This is essential when the user asks broad questions like "what's in the living room?" or "show me all cameras".

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/entity_discover" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"room": "soggiorno", "location_id": "albani"}'
```

Available filters (all optional, combinable):
- `room`: search by room/area name (e.g., "soggiorno", "cucina") — partial match
- `domain`: filter by entity type (e.g., "camera", "light", "media_player", "sensor")
- `zone`: filter by zone (e.g., "Zona Giorno", "Zona Notte") — partial match
- `floor`: filter by floor/piano (e.g., "Piano 1", "Garage") — partial match
- `search`: free text search in entity names and entity_ids (e.g., "cam", "temperatura")
- `limit`: max results (default 50)

Response example:
```json
{
  "location_id": "albani",
  "count": 8,
  "rooms_found": ["Soggiorno"],
  "domains_found": ["camera", "light", "media_player", "sensor"],
  "entities": [
    {"entity_id": "camera.cam_soggiorno", "friendly_name": "Cam Soggiorno", "domain": "camera", "room": "Soggiorno", "device_name": "Cam Soggiorno", "available_services": ["turn_on", "turn_off"]},
    {"entity_id": "light.soggiorno", "friendly_name": "Soggiorno", "domain": "light", "room": "Soggiorno", "device_name": "Luce Soggiorno", "available_services": ["turn_on", "turn_off", "toggle"]}
  ]
}
```

Use cases:
- "Cosa c'è in soggiorno?" → `{"room": "soggiorno"}`
- "Quali telecamere ci sono?" → `{"domain": "camera"}`
- "Cerca tutto con 'cam'" → `{"search": "cam"}`
- "Dispositivi al piano 1 zona notte" → `{"floor": "Piano 1", "zone": "Zona Notte"}`
- "Tutte le luci in cucina" → `{"room": "cucina", "domain": "light"}`

### 3. Get locations

Use this to list all configured Home Assistant locations with health status.

```bash
curl -s "$JARVIS_ORCHESTRATOR_URL/api/tools/locations" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN"
```

### 4. Query user memory

Use this when the user asks "cosa abbiamo detto ieri?", "ti ricordi di...?", or similar.

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/memory_query" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "john",
    "query": "cosa abbiamo detto ieri sera?",
    "context_type": "reasoning"
  }'
```

- `context_type`: "routing" (compact, for quick decisions) or "reasoning" (full, for complex questions)

### 5. Get user context

Use this to fetch user profile, current location, and preferences.

```bash
curl -s "$JARVIS_ORCHESTRATOR_URL/api/tools/user_context?user_id=john" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN"
```

### 6. Security actions

Use this for privacy mode, alarm control, etc.

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/security" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "set_privacy_mode",
    "parameters": {"enabled": true},
    "source_channel": "openclaw_telegram"
  }'
```

### 7. Text-to-speech

Use this to make Alexa/smart speakers say something.

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/tts" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Buongiorno!",
    "speaker_entity": "media_player.echo_salotto",
    "location_id": "wagmi"
  }'
```

### 8. Log an event

Use this to log actions for audit trail visibility.

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/audit_log" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "home_control",
    "details": "Accesa luce soggiorno via Telegram",
    "user_id": "john",
    "source": "openclaw",
    "severity": "info"
  }'
```

## Examples

User: "Accendi la luce del soggiorno"
-> Step 2 (entity_resolve "luce soggiorno") → get entity_id + available_services
-> Step 1 (home_control) with entity_name=entity_id, action="turn_on"

User: "Metti la luminosità della cucina al 50%"
-> Step 2 (entity_resolve "luce cucina") → see service_params.turn_on.brightness = "0-255"
-> Step 1 (home_control) with action="turn_on", parameters={"brightness": 128}

User: "Che c'è in soggiorno?"
-> Step 2b (entity_discover room="soggiorno") → list all entities with their types and services

User: "Quali telecamere ci sono?"
-> Step 2b (entity_discover domain="camera") → list all cameras with entity_ids

User: "Che temperatura c'e in casa?"
-> Step 2b (entity_discover domain="sensor", search="temperatura") → find temperature sensors with state
-> Or: Step 2 (entity_resolve "temperatura") → get state directly

User: "Chiudi tutte le tapparelle"
-> Step 2b (entity_discover domain="cover") → find all covers
-> Step 1 (home_control) for each, action="close_cover"

User: "Ti ricordi cosa mi hai detto ieri?"
-> Step 4 (memory_query) with query="cosa mi hai detto ieri"

User: "Attiva la modalita privacy"
-> Step 6 (security) with action="set_privacy_mode"

User: "Dì buongiorno su Alexa"
-> Step 7 (tts) with text="Buongiorno!"

## Constraints

- **Always resolve first**: Call step 2 (entity_resolve) before step 1 (home_control) to get the correct entity_id and available services
- Always pass `source_channel: "openclaw_telegram"` when the request comes from Telegram
- Do NOT try to control L3 devices (locks, alarm) without mentioning that confirmation will be required
- Entity names are in Italian — pass them as-is, the API resolves them
- If location_id is not specified by the user, omit it — the API auto-resolves it
- Use `available_services` from entity_resolve to choose the correct action — don't guess service names
- Use `state` from entity_resolve to check the current state before acting
- Always log important actions via step 8 (audit_log)
- Respond to the user in the same language they used (Italian or English)
