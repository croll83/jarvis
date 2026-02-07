# JARVIS AtomS3R - Wake Word Device

Firmware ESP-IDF per M5Stack AtomS3R che funge da dispositivo di input vocale per JARVIS.

## Features

- **Wake Word Detection**: Riconosce "Jarvis" usando ESP-SR WakeNet (modello `wn9_jarvis` da partizione esterna)
- **Streaming Audio con VAD**: Invia audio in tempo reale, termina automaticamente quando rileva silenzio
- **Display TFT 128x128**: Mostra stato, ora e temperatura
- **DND Mode**: Click sul bottone (GPIO0) per attivare/disattivare "Do Not Disturb"
- **Integrazione Home Assistant**: Mostra temperatura della stanza

## Hardware

- **M5Stack AtomS3R** (ESP32-S3-PICO-1-N8R8)
  - ESP32-S3 + 8MB Flash OPI + 8MB PSRAM OPI
  - Display TFT 128x128 ST7789
  - Microfono PDM integrato (GPIO1 CLK, GPIO2 DATA)
  - Bottone integrato (GPIO0)

### Pinout

| Periferica | Pin ESP32-S3 | Note |
|------------|--------------|------|
| LCD SCK | 17 | Clock display |
| LCD MOSI | 21 | Dati display |
| LCD DC | 33 | Data/Command |
| LCD RST | 34 | Reset |
| LCD CS | 15 | Chip Select |
| MIC DATA | 2 | I2S Microphone |
| MIC CLK | 1 | I2S Clock |
| SCREEN BTN | 0 | Bottone sotto il display |

## Requisiti

- **ESP-IDF v5.1+** (testato con v5.2)
- Python 3.8+
- Connessione WiFi
- JARVIS Orchestrator in esecuzione

## Layout Partizioni (8MB Flash)

```
Nome       Tipo    Dimensione    Descrizione
─────────────────────────────────────────────────
nvs        data    24KB          NVS storage
phy_init   data    4KB           PHY calibration
factory    app     2MB           Applicazione (SENZA modello embedded)
model      spiffs  2.5MB         WakeNet wn9_jarvis (index + data)
icons      spiffs  1MB           Icone Lucide + font
storage    spiffs  2MB           Config, cache, dati sistema
```

## Installazione ESP-IDF

Se non hai ancora ESP-IDF installato:

```bash
# Clona ESP-IDF
mkdir -p ~/esp
cd ~/esp
git clone -b v5.2 --recursive https://github.com/espressif/esp-idf.git

# Installa
cd esp-idf
./install.sh esp32s3

# Attiva l'ambiente (da fare ogni volta che apri un nuovo terminale)
source ~/esp/esp-idf/export.sh
```

## Build e Flash

### 1. Configura le credenziali

```bash
cd atoms3r-jarvis
idf.py menuconfig
```

Vai in **JARVIS Configuration** e imposta:
- WiFi SSID
- WiFi Password
- JARVIS Server Host (es. `192.168.1.100` o `jarvis.local`)
- JARVIS Server Port (default: `5000`)
- Device Room (es. `salotto`, `cucina`, `camera`)
- Device ID (es. `atoms3r_salotto`)

### 2. Build

```bash
idf.py build
```

### 3. Flash dell'applicazione

```bash
# Connetti l'AtomS3R via USB-C
idf.py -p /dev/ttyUSB0 flash

# Oppure su macOS
idf.py -p /dev/cu.usbserial-* flash
```

### 4. Flash del modello WakeNet (IMPORTANTE!)

Il modello `wn9_jarvis` **non è embedded** nell'applicazione, ma deve essere flashato separatamente nella partizione `model`.

#### Download del modello

Scarica i file del modello da: https://github.com/espressif/esp-sr/tree/master/model/wakenet

I file necessari sono:
- `wn9_jarvis` (o `wn9_jarvis_data` + `wn9_jarvis_index`)

#### Crea immagine SPIFFS con il modello

```bash
# Installa mkspiffs se non presente
pip install mkspiffs

# Crea una directory con i file del modello
mkdir -p model_data
cp wn9_jarvis* model_data/

# Crea l'immagine SPIFFS (2.5MB = 0x280000)
python $IDF_PATH/components/spiffs/spiffsgen.py \
    0x280000 \
    model_data \
    model.bin

# Flash nella partizione 'model'
# L'offset si calcola da partitions.csv: dopo factory (2MB) = 0x9000 + 0x1000 + 0x200000
esptool.py --chip esp32s3 --port /dev/ttyUSB0 \
    write_flash 0x207000 model.bin
```

> **Nota**: Devi flashare il modello solo una volta. L'applicazione può essere aggiornata senza toccare la partizione del modello.

### 5. Monitor (opzionale)

```bash
idf.py -p /dev/ttyUSB0 monitor
```

Per uscire dal monitor: `Ctrl+]`

### Build + Flash + Monitor in un comando

```bash
idf.py -p /dev/ttyUSB0 flash monitor
```

## Struttura Progetto

```
atoms3r-jarvis/
├── CMakeLists.txt              # Progetto principale
├── sdkconfig.defaults          # Configurazione SDK default
├── partitions.csv              # Tabella partizioni (8MB ottimizzata)
├── main/
│   ├── CMakeLists.txt
│   ├── idf_component.yml       # Dipendenze (esp-sr)
│   ├── Kconfig.projbuild       # Menu configurazione
│   ├── main.cpp                # Entry point (C++)
│   └── jarvis_config.h         # Configurazione + pinout
└── components/
    ├── jarvis_display/         # Modulo display ST7789
    │   ├── include/
    │   │   ├── jarvis_display.h
    │   │   └── jarvis_icons.h
    │   └── jarvis_display.c
    ├── jarvis_audio/           # Modulo audio + ESP-SR (carica da SPIFFS)
    │   ├── include/
    │   │   └── jarvis_audio.h
    │   ├── idf_component.yml   # Dipendenza esp-sr
    │   └── jarvis_audio.c
    └── jarvis_network/         # Modulo network
        ├── include/
        │   └── jarvis_network.h
        └── jarvis_network.c
```

## Utilizzo

1. **Accensione**: Il device si connette al WiFi e mostra ora/temperatura
2. **Attivazione**: Dì "Jarvis" per attivare l'ascolto
3. **Comando**: Parla normalmente, il device invia l'audio in streaming
4. **Fine**: Quando smetti di parlare (~1s di silenzio), l'audio viene processato
5. **Risposta**: Il display mostra "Speaking..." mentre JARVIS risponde

### Stati del Display

| Stato | Visualizzazione |
|-------|-----------------|
| IDLE | Ora + Temperatura |
| LISTENING | Icona microfono + animazione onde |
| PROCESSING | Icona microfono + "Processing..." |
| BUSY | Icona megafono + "Speaking..." |
| DND | Come IDLE ma con bordo rosso |

### DND Mode (Do Not Disturb)

- **Attivazione**: Premi il bottone (GPIO0) sotto il display
- **Effetto**: Il wake word viene ignorato, bordo rosso sul display
- **Disattivazione**: Premi di nuovo il bottone

## Configurazione per Stanza

Per ogni AtomS3R in stanze diverse, modifica via `idf.py menuconfig`:

| Stanza | DEVICE_ROOM | DEVICE_ID |
|--------|-------------|-----------|
| Salotto | `salotto` | `atoms3r_salotto` |
| Camera | `camera` | `atoms3r_camera` |
| Cucina | `cucina` | `atoms3r_cucina` |
| Ufficio | `ufficio` | `atoms3r_ufficio` |

## Sensibilità Wake Word

La sensibilità è configurata in `components/jarvis_audio/jarvis_audio.c`:

```c
afe_config.wakenet_mode = DET_MODE_95;  // Cambia questo valore
```

| Valore | Sensibilità | False Positive | Ambiente |
|--------|-------------|----------------|----------|
| `DET_MODE_90` | Alta | ~1-2/ora | Silenzioso |
| `DET_MODE_95` | Media | ~1/giorno | Casa normale |
| `DET_MODE_2G75` | Medio-bassa | Raro | Rumori moderati |
| `DET_MODE_3G75` | Bassa | Molto raro | Rumoroso |

## Integrazione con Orchestrator

### Endpoint Utilizzati

| Funzione | Endpoint | Metodo |
|----------|----------|--------|
| Streaming audio | `/voice_stream` | POST (chunked) |
| Polling stato | `/device_status?room={room}` | GET |
| Notifica DND | `/device_status` | POST |
| Temperatura | `/room_temperature/{room}` | GET |

### Flow Audio

```
AtomS3R → (streaming audio) → Orchestrator
    ↓
Orchestrator → RNNoise (denoise)
    ↓
Orchestrator → Faster-Whisper (STT)
    ↓
Orchestrator → Resemblyzer (speaker ID)
    ↓
Orchestrator → LLM + azioni
```

## Troubleshooting

### "Wake word non rilevato"

1. Verifica che il modello sia stato flashato nella partizione `model`
2. Controlla i log per errori SPIFFS mount
3. Verifica che PSRAM sia abilitato
4. Prova ad aumentare la sensibilità a `DET_MODE_90`

### "Model partition not found" o "SPIFFS mount failed"

1. Verifica che `partitions.csv` sia flashato correttamente
2. Flash il modello con i comandi nella sezione "Flash del modello WakeNet"
3. Pulisci e riflasha tutto: `idf.py fullclean && idf.py flash`

### "WiFi non si connette"

1. Verifica SSID e password in `menuconfig`
2. Controlla che il router sia raggiungibile
3. Prova con IP statico invece di `.local`

### "Temperatura non mostrata"

1. Verifica che JARVIS Orchestrator sia in esecuzione
2. Controlla che l'endpoint `/room_temperature/{room}` risponda
3. Verifica il nome della stanza

### "Build fallisce con errore PSRAM"

1. Assicurati di usare ESP-IDF 5.1+
2. Verifica che `CONFIG_SPIRAM=y` sia in `sdkconfig.defaults`
3. Pulisci la build: `idf.py fullclean && idf.py build`

### "ESP-SR non trovato"

Il component manager scarica automaticamente ESP-SR. Se fallisce:
```bash
idf.py reconfigure
idf.py build
```

## Note Tecniche

- **Modello esterno**: Il modello `wn9_jarvis` è caricato da SPIFFS, non embedded. Questo riduce la dimensione dell'applicazione e permette aggiornamenti separati.
- **PSRAM OPI**: Necessario per ESP-SR, l'AtomS3R ha 8MB di PSRAM in modalità Octal SPI per massime prestazioni.
- **Flash OPI**: 8MB di Flash in modalità Octal SPI.
- **VAD**: Voice Activity Detection integrato in ESP-SR AFE.
- **Streaming**: HTTP chunked transfer encoding per bassa latenza.

## Aggiornamento Modello

Per aggiornare solo il modello senza toccare l'applicazione:

```bash
# Crea nuova immagine SPIFFS con il modello aggiornato
python $IDF_PATH/components/spiffs/spiffsgen.py 0x280000 model_data model.bin

# Flash solo la partizione model
esptool.py --chip esp32s3 --port /dev/ttyUSB0 write_flash 0x207000 model.bin
```

## Aggiornamento Icone

Per aggiornare solo le icone senza toccare applicazione e modello:

```bash
# Crea immagine SPIFFS con le icone (1MB = 0x100000)
python $IDF_PATH/components/spiffs/spiffsgen.py 0x100000 icons_data icons.bin

# Flash nella partizione icons (offset = model offset + model size)
esptool.py --chip esp32s3 --port /dev/ttyUSB0 write_flash 0x487000 icons.bin
```
