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

### 2. Resolve an entity name

Use this when you need to find the correct entity_id for a friendly name.

```bash
curl -s -X POST "$JARVIS_ORCHESTRATOR_URL/api/tools/entity_resolve" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"friendly_name": "luce cucina", "location_id": "wagmi"}'
```

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
-> Call step 1 with entity_name="luce soggiorno", action="turn_on"

User: "Che temperatura c'e in casa?"
-> Call step 3 to get locations, then call step 5 for user context

User: "Chiudi tutte le tapparelle"
-> Call step 1 with entity_name="tapparelle", action="close_cover"

User: "Ti ricordi cosa mi hai detto ieri?"
-> Call step 4 with query="cosa mi hai detto ieri"

User: "Attiva la modalita privacy"
-> Call step 6 with action="set_privacy_mode"

User: "Dì buongiorno su Alexa"
-> Call step 7 with text="Buongiorno!"

## Constraints

- Always pass `source_channel: "openclaw_telegram"` when the request comes from Telegram
- Do NOT try to control L3 devices (locks, alarm) without mentioning that confirmation will be required
- Entity names are in Italian — pass them as-is, the API resolves them
- If location_id is not specified by the user, omit it — the API auto-resolves it
- Always log important actions via step 8 (audit_log)
- Respond to the user in the same language they used (Italian or English)
