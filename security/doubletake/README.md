# DoubleTake - Facial Recognition

## Overview

DoubleTake è il sistema di riconoscimento facciale integrato con Frigate per identificare i membri della famiglia.

## Accesso

- **URL**: http://localhost:3000
- **Default**: Nessuna autenticazione (configura in produzione!)

## Enrollment Volti

### Procedura Passo-Passo

1. **Accedi alla UI**: http://localhost:3000

2. **Vai in Train > Add Person**

3. **Inserisci il nome**
   - IMPORTANTE: Il nome deve corrispondere ESATTAMENTE al nome utente in JARVIS
   - Case-sensitive: "Marco" ≠ "marco"

4. **Carica le foto**
   - Minimo 5-10 foto
   - Idealmente 15-20 per migliore accuratezza

5. **Clicca Train**
   - Attendi il completamento del training
   - Verifica il risultato in "Match > Test"

### Requisiti Foto

| Criterio | Requisito |
|----------|-----------|
| Formato | JPG, PNG |
| Risoluzione minima | 200x200 pixel (volto) |
| Qualità | Nitida, non sfocata |
| Illuminazione | Variata |
| Sfondo | Qualsiasi |

### Foto Raccomandate

Per ogni persona, includi foto con:

- [ ] Luce naturale (giorno)
- [ ] Luce artificiale (sera)
- [ ] Vista frontale
- [ ] Vista 3/4 (lato destro)
- [ ] Vista 3/4 (lato sinistro)
- [ ] Con occhiali (se li usa)
- [ ] Senza occhiali
- [ ] Espressione neutra
- [ ] Sorridente
- [ ] Indoor
- [ ] Outdoor

### Dove Trovare le Foto

1. **Galleria smartphone** - Selfie e foto varie
2. **Snapshots Frigate** - `/media/frigate/clips/` (dopo detection)
3. **Webcam** - Scatta foto con illuminazione diversa

## Configurazione Avanzata

### File di Configurazione

DoubleTake legge la configurazione da `.storage/config.yml`:

```yaml
frigate:
  url: http://frigate:5000
  update_sub_labels: true

mqtt:
  host: mqtt
  port: 1883

detectors:
  compreface:
    url: http://compreface:8000
    key: your-api-key
```

### Detector Backend

DoubleTake supporta diversi backend:

| Backend | Pro | Contro |
|---------|-----|--------|
| Built-in | Zero config | Meno accurato |
| CompreFace | Molto accurato | Richiede container aggiuntivo |
| DeepStack | Accurato | Richiede container aggiuntivo |
| AWS Rekognition | Molto accurato | Costo per richiesta |

### Setup CompreFace (Opzionale)

Per migliore accuratezza, aggiungi CompreFace al docker-compose:

```yaml
services:
  compreface:
    image: exadel/compreface:latest
    ports:
      - "8000:8000"
    environment:
      - POSTGRES_PASSWORD=postgres
    volumes:
      - ./compreface:/data
```

## Integrazione JARVIS

### Flow di Riconoscimento

```
Frigate Detection    DoubleTake          JARVIS
      │                  │                  │
      ├──snapshot───────▶│                  │
      │                  │                  │
      │                  ├──face match─────▶│
      │                  │                  │
      │                  │              (if unknown)
      │                  │                  ├──Alert Telegram
      │                  │              (if known)
      │                  │                  └──Log silenzioso
```

### Eventi MQTT

DoubleTake pubblica su MQTT topic `frigate/events`:

```json
{
  "type": "new",
  "camera": "ingresso",
  "match": {
    "name": "Marco",
    "confidence": 0.92,
    "detector": "compreface"
  }
}
```

## Troubleshooting

### Riconoscimento Fallisce

1. **Aggiungi più foto di training**
   - Varietà > Quantità
   - Includi condizioni simili alla camera

2. **Verifica qualità snapshot Frigate**
   - Aumenta risoluzione detect
   - Migliora illuminazione della zona

3. **Abbassa la soglia di match**
   - In config: `match.confidence: 0.6` (default 0.7)

### Training Lento

- Riduci risoluzione foto prima dell'upload
- Usa meno foto per test iniziale

### Match Errati

- Rimuovi foto problematiche dal training set
- Aumenta soglia confidence
- Verifica che i nomi non siano simili

## Backup

### Esporta Face Models

```bash
# Backup completo
tar -czf doubletake_backup.tar.gz .storage/

# Solo face data
cp -r .storage/faces/ /backup/
```

### Ripristino

```bash
tar -xzf doubletake_backup.tar.gz -C /opt/jarvis/security/doubletake/
docker compose -f docker-compose.security.yml restart doubletake
```

## Privacy

### Best Practices

- Face data conservati solo localmente
- No cloud processing (a meno di AWS Rekognition)
- Retention configurabile per snapshots
- Log audit per ogni riconoscimento

### GDPR Compliance

Se in EU, considera:
- Consenso esplicito per enrollment
- Diritto alla cancellazione (delete training data)
- Log degli accessi ai dati
