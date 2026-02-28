# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

### Ontology Remote - Knowledge Graph API

**Server:** `http://127.0.0.1:8100`  
**Auth:** `X-Speaker-Id: marco` + Bearer token `$ONTOLOGY_API_TOKEN`

**Quick Commands:**

```bash
# Get Marco's profile
curl -s "$ONTOLOGY_URL/entities/pers_137ac654" -H "X-Speaker-Id: marco" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Get Marco's relations
curl -s "$ONTOLOGY_URL/entities/pers_137ac654/related" -H "X-Speaker-Id: marco" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Find crypto accounts
curl -s "$ONTOLOGY_URL/entities?type=Account" -H "X-Speaker-Id: marco" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" | jq '.[] | select(.properties.type_enum == "crypto_wallet")'

# Create entity
curl -X POST "$ONTOLOGY_URL/entities" -H "X-Speaker-Id: marco" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" -H "Content-Type: application/json" -d '{"type":"Task","properties":{"title":"...","status_enum":"open"}}'
```

**Full API Reference + Entity Types + Relation Types:** → `skills/ontology-remote/SKILL.md`

---

### TTS — Voice Profiles (Family)
Questo vale solo per le **note vocali** che crei in locale da mandare sui **canali Telegram e Whatsapp**

**Engine:** XTTS (REST) remoto su server esterno con GPU con interfaccia OpenAI
**Speaker:** (Profilo vocale usato da XTTS): 'jarvis'

```bash
# Send voice message via Telegram
message send --target marco --filePath /tmp/jarvis-voice.ogg --asVoice true
```

### Regole TTS e provider openresponses-user (quando ti invoca Jarvis Orchestrator) o hint 'Live Session' o source 'AtomS3R' o 'VirtualMic' 
- **Streaming TTS:** Leggi frase per frase durante risposta OpenClaw (non aspettare EOF)
- **Graceful fallback:** Se edge-tts timeout → usa TTS remoto (OpenClaw ripiego)

### Smart Home — Infrastruttura

- **Qualsiasi richiesta Smart Home / Domotica** (accendi, spegni, apri, chiudi, alza, abbassa, casta, muta, ecc), deve essere veicolata all'orchestrator tramite la **skill jarvis-orchestrator** che è documentata in .openclaw/workspace/skills/jarvis-orchestrator/SKILL.md
- Smart Home è indipendente e separata da Ontology e ACL: se il messaggio arriva da Marco su Telegram o da jarvis-orchestrator con o senza speaker-id, non è necessario verificare ACL.

⚠️ **NON salvare entity IDs qui.** Usa SEMPRE `entity_resolve` / `entity_discover` dell'orchestrator per risolvere le entity. L'orchestrator è la single source of truth. Questa regola vale anche quando esiste l'ontologia: le entity HA non vanno duplicate da nessuna parte.

#### Workaround & Quirk
- **TV quirk:** DLNA a volte spegne la TV → `turn_on` + sleep 8 prima del cast

### Cron Job IDs

### Generazione Immagini (nano-banana-pro)

- **Path output:** Generare sempre in `/home/jarvis/.openclaw/workspace/` — il tool `message` non accetta `/tmp/`
- Dopo l'invio, fare cleanup: `rm /home/jarvis/.openclaw/workspace/<filename>`

### Lezioni Operative (Tips & Workaround)

- Per bridge Hyperliquid: usare **CoreDepositWallet** `0x6b9e77...`, MAI `0x2222...`
- Nitter non affidabile per scraping Twitter → CDP meglio
- Gemini Flash non affidabile per task complessi (digest) → Sonnet
- TV media cast: usa jarvis-orchestrator `media_cast/upload` per file locali (DLNA), `media_cast` + `force_browser` per URL
