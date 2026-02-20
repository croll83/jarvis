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

**Engine:** edge-tts (Microsoft, gratis) + ffmpeg (conversione locale)

#### Voice Profiles

| User | Voice | Personality | Use Case |
|------|-------|-------------|----------|
| **Marco** | `it-IT-GiuseppeMultilingualNeural` | Baritonale, confidenziale, autorevole | Comandi primari, decisioni importanti |
| **Ada** | `it-IT-IsabellaNeural` | Professionale, chiara, rassicurante | Ricette, reminder, organizzazione |
| **Giorgio** | `it-IT-DiegoNeural` | Giovane, entusiasta, dinamico | Entertainment, comandi veloci |
| **Sofia** | `it-IT-ElsaNeural` | Dolce, rassicurante, premurosa | Storie, supporto emotivo, reminder gentili |

#### Commands

```bash
# Generate MP3 for a voice
edge-tts --voice "it-IT-GiuseppeMultilingualNeural" \
  --text "Acceso. Tutto funziona." \
  --write-media /tmp/jarvis-voice.mp3

# Convert to OGG (required for Telegram voice messages)
ffmpeg -y -i /tmp/jarvis-voice.mp3 -c:a libopus -b:a 64k /tmp/jarvis-voice.ogg

# Send voice message via Telegram
message send --target marco --filePath /tmp/jarvis-voice.ogg --asVoice true
```

#### Speaker Auto-Detection

Voice profiles are auto-selected by **speaker identification** (Resemblyzer):
- If voice detected → use that speaker's voice
- If confidence < 0.65 → fallback to context-based selection
- If context unknown → use Marco's voice (admin default)

#### Regole

- **Voice messages in, voice messages out:** Quando Marco/Ada/Giorgio/Sofia mandano nota vocale → rispondere con la LORO voce, non con Marco. Formatta la risposta per un TTS diretto: evita emojii, bullet points ed elenchi numerati, aggiungi enfasi e punteggiatura
- **Streaming TTS:** Leggi frase per frase durante risposta OpenClaw (non aspettare EOF)
- **Graceful fallback:** Se edge-tts timeout → usa TTS remoto (OpenClaw ripiego)
- **Config:** `/skills/jarvis-orchestrator/voice_profiles.json` — profili, rate, pitch, volume

### Smart Home — Infrastruttura

- **Qualsiasi richiesta Smart Home / Domotica** (accendi, spegni, apri, chiudi, alza, abbassa, casta, muta, ecc), deve essere veicolata all'orchestrator tramite la **skill jarvis-orchestrator** che è documentata in .openclaw/workspace/skills/jarvis-orchestrator/SKILL.md
- Smart Home è indipendente e separata da Ontology e ACL: se il messaggio arriva da Marco su Telegram o da jarvis-orchestrator con o senza speaker-id, non è necessario verificare ACL.

⚠️ **NON salvare entity IDs qui.** Usa SEMPRE `entity_resolve` / `entity_discover` dell'orchestrator per risolvere le entity. L'orchestrator è la single source of truth. Questa regola vale anche quando esiste l'ontologia: le entity HA non vanno duplicate da nessuna parte.

#### Workaround & Quirk
- **TV quirk:** DLNA a volte spegne la TV → `turn_on` + sleep 8 prima del cast

### Crypto Twitter Digest — Setup Tecnico

- Scraping via CDP (Chrome DevTools Protocol) su browser headless
- Chrome: `ws://127.0.0.1:18800`, loggato come @SatoshiAzimut
- Cookies encrypted: `.secrets/twitter-creds.enc`
- ~60 account in 3 tier di priorità, scoring crypto-first
- Cron: 8:30 + 20:00 CET su **Sonnet** (Gemini Flash troppo inaffidabile)

### Cron Job IDs

| Job | ID |
|-----|-----|
| Trading Engine v2.1 | `f19cf05c-9f27-401d-9597-c27224655fd0` |
| Sentiment Scraper | `329c1cc6-104f-4239-8a1d-04bdea357eb4` |
| Trade Postmortem | `2edcaf74-f801-4290-ac55-0e2215858741` |
| Daily Report | `6621d9ad-639c-4c3e-80ff-bbfc86d41c61` |
| Crypto Twitter Digest | `87bb2049-3724-40cf-a90b-5af95f469d0f` |
| Crypto Newsletter Digest | `ee9643b4-b6ea-4c78-b47f-914bf8dde42f` |
| Update Ontology Graph | `cabf0de9-fd1a-4dc9-95c7-ba34c181d3d3` |

### Lezioni Operative (Tips & Workaround)

- Per bridge Hyperliquid: usare **CoreDepositWallet** `0x6b9e77...`, MAI `0x2222...`
- Nitter non affidabile per scraping Twitter → CDP meglio
- Gemini Flash non affidabile per task complessi (digest) → Sonnet
- TV media cast: usa jarvis-orchestrator `media_cast/upload` per file locali (DLNA), `media_cast` + `force_browser` per URL

### Hyperliquid Wallet
- **Private key:** encrypted at `.secrets/hl-wallet.enc` (AES-256-CBC, key from machine-id)
- **Mnemonic:** same file
- **Decrypt:** `openssl enc -aes-256-cbc -pbkdf2 -d -in .secrets/hl-wallet.enc -pass pass:$(cat /etc/machine-id)`
