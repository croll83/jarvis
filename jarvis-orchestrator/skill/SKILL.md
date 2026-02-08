---
name: jarvis-orchestrator
description: Smart home control, memory, speaker ID, and security for multi-location Home Assistant
user-invocable: true
---

# JARVIS Orchestrator

Smart home control, memory, speaker identification, and security for a multi-location Home Assistant setup.

## When to use

- User wants to control smart home devices (lights, covers, climate, locks)
- User asks about their home (temperatures, entity states, locations)
- User asks about past conversations or personal facts (memory system)
- User needs security actions (privacy mode, alarm)
- User sends a voice command that needs speaker identification

## Configuration

- **Base URL**: `${JARVIS_ORCHESTRATOR_URL:-http://localhost:5000}/api/tools`
- **Auth**: Bearer token (`$OPENCLAW_GATEWAY_TOKEN`)

## Tools

### jarvis_home_control

Control Home Assistant entities (lights, covers, climate, locks, etc.) with security level enforcement.

**POST** `/home_control`

```json
{
  "entity_name": "luce soggiorno",
  "action": "turn_on",
  "parameters": {"brightness": 200},
  "location_id": "wagmi",
  "source_channel": "openclaw_telegram"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Eseguito light.turn_on su light.soggiorno",
  "entity_id": "light.soggiorno",
  "security_level": "L1",
  "approval_required": false
}
```

**Security Levels:**
- L1 (lights, sensors, switches): always allowed from Telegram
- L2 (covers, climate, fans): allowed with context check
- L3 (locks, alarm, cameras): requires confirmation via JARVIS approval bot
- L4 (email/unknown sources): always blocked

**Entity names** can be friendly names ("luce soggiorno") or entity_ids ("light.soggiorno"). The system resolves them automatically based on the location's entity map.

**Location** is auto-resolved if not provided. For voice commands, it comes from the AtomS3R device. For Telegram, from the user's last known location.

### jarvis_speaker_id

Identify a speaker from audio data using Resemblyzer voice embeddings.

**POST** `/speaker_id`

```json
{
  "audio_base64": "<base64 encoded float32 audio>",
  "sample_rate": 16000
}
```

### jarvis_get_user_context

Get user profile, current location, and preferences.

**GET** `/user_context?user_id=john`

Returns user info, active location, role, and global preferences.

### jarvis_security

Execute security actions (privacy mode, alarm control).

**POST** `/security`

```json
{
  "action": "set_privacy_mode",
  "parameters": {"enabled": true},
  "source_channel": "openclaw_telegram"
}
```

### jarvis_memory_query

Query the stratified memory system (SQL hot/warm/cold + vector long-term).

**POST** `/memory_query`

```json
{
  "user_id": "john",
  "location_id": "wagmi",
  "query": "cosa abbiamo detto ieri sera?",
  "context_type": "routing"
}
```

`context_type` can be `"routing"` (compact, for quick decisions) or `"reasoning"` (full, for complex questions).

### jarvis_entity_resolve

Resolve a friendly entity name to its Home Assistant entity_id.

**POST** `/entity_resolve`

```json
{
  "friendly_name": "luce cucina",
  "location_id": "wagmi",
  "domain_filter": "light"
}
```

Returns the entity_id, domain, area, and alternatives if not found.

### jarvis_tts

Text-to-speech passthrough. Speaks text via Alexa/smart speaker.

**POST** `/tts`

```json
{
  "text": "Buongiorno, le luci sono accese",
  "speaker_entity": "media_player.echo_salotto",
  "location_id": "wagmi",
  "sound": "positive"
}
```

Sound options: `"positive"`, `"negative"`, `"neutral"`, or any Alexa sound ID.

### jarvis_get_locations

List all configured locations with Home Assistant health status.

**GET** `/locations`

Returns array of locations with `location_id`, `name`, `hass_url`, `healthy`, `has_security`.

### jarvis_audit_log

Log an event to the audit trail for tracking and dashboard visibility.

**POST** `/audit_log`

```json
{
  "event_type": "home_control",
  "details": "Accesa luce soggiorno via Telegram",
  "user_id": "john",
  "source": "openclaw",
  "severity": "info"
}
```

## Location System

JARVIS supports multiple Home Assistant instances across locations:

- **wagmi** (primary): Local HA at 192.168.1.x
- **napoli**: Remote HA via Tailscale VPN

Each location has its own entity map, HA URL/token, and security configuration.

Location resolution priority:
1. Explicit `location_id` in request
2. Voice: device_id (MAC address) → DB lookup → location
3. Telegram: user's sticky location from DB
4. Fallback: default location from config

**Override**: User can say "accendi luci a Napoli" to target a different location.

## Security Model

The L1-L4 security model prevents prompt injection attacks:

| Level | Domains | Voice | Telegram | Email |
|-------|---------|-------|----------|-------|
| L1 | lights, sensors, switches | pass | pass | blocked |
| L2 | covers, climate, fans | pass | context check | blocked |
| L3 | locks, alarm, cameras | pass | approval bot | blocked |
| L4 | — | — | — | always blocked |

**Voice** commands (certified by Resemblyzer speaker ID) are always trusted up to L3.

**L3 confirmation** goes through a separate JARVIS Telegram bot (not OpenClaw) to prevent injection in the confirmation channel.

All security mappings are configurable via the admin dashboard.

## Memory System

Stratified memory with 4 tiers:

- **Hot** (30 min): Raw recent messages
- **Warm** (24 hours): Hourly summaries
- **Cold** (7 days): Daily summaries
- **Long-term**: Vector store (ChromaDB) for semantic search

The `memory_query` tool retrieves context from all tiers, optimized by `context_type`.

## Speaker Identification

Uses Resemblyzer (deep speaker embeddings) for voice biometric identification:

1. Audio → Whisper STT → text
2. Audio → Resemblyzer → speaker embedding → cosine similarity → user_id
3. User context loaded from DB (preferences, location, role)

Minimum 3 enrollment samples required. Threshold: 0.75 similarity.
