# HAOS Replicate — stack Albani20 → HAOS Wagmi

Ricrea su una **nuova** istanza Home Assistant OS lo *stack* di Albani20
(add-on, HACS, memory-service) **senza** integrazioni, device, dashboard o
automazioni (la "entity map" è esclusa di proposito).

Stato di riferimento (Albani, giugno 2026): **HA OS 17.3 · Supervisor 2026.05.1 · Core 2026.6.0**.

---

## Ordine delle operazioni

### 1. Crea la VM (Terraform)
```bash
cd ../../terraform/haos-wagmi
cp terraform.tfvars.example terraform.tfvars   # compila password pve-wagmi
terraform init && terraform plan
terraform apply         # crea la VM 210 su pve-wagmi (start_on_create=false)
ssh root@100.99.14.73 "qm start 210"           # avviala quando vuoi
```
La VM nasce **a vuoto** (HAOS pulito). Annota il MAC (output Terraform) per
fissare l'IP via DHCP sul router.

### 2. Onboarding HA
Apri `http://<ip>:8123`, crea l'utente admin, imposta nome/posizione.
Poi genera un **Long-Lived Token**: Profilo → in fondo → *Token di lunga durata*.

### 3. Installa HACS (una tantum, prerequisito)
HACS non si auto-installa: dal terminale dell'add-on SSH (o console) esegui lo
script ufficiale, poi aggiungi l'integrazione HACS e completa l'OAuth GitHub.
```bash
wget -O - https://get.hacs.xyz | bash -
# poi: Impostazioni → Dispositivi e servizi → Aggiungi integrazione → HACS
```
> Se preferisci, prima installa l'add-on SSH dal passo 4 e usalo come shell.

### 4. Replica add-on + HACS (questo script)
```bash
cp .env.example .env
nano .env          # HASS_URL, HASS_TOKEN del NUOVO haos + segreti nuovi
./replicate.sh --dry-run    # anteprima, non scrive nulla
./replicate.sh              # esegue davvero
```
Lo script è **idempotente**: rilancialo quante volte vuoi.

---

## Cosa fa lo script
- aggiunge i 4 **store-repository** (community add-ons, AlexxIT, MASS, ESPHome);
- installa e avvia i **10 add-on**, applicando opzioni/boot/auto_update presi
  da Albani (vedi `manifest.yaml`);
- copia e installa l'**add-on locale JARVIS HA Memory** (se `SSH_TARGET` è
  impostato in `.env`), con `location_id` cambiato in `wagmi`;
- installa i **13 repo HACS** (best-effort via WS; se l'API HACS cambia, stampa
  la lista da aggiungere a mano).

## Cosa resta MANUALE (non clonabile)
| Elemento | Perché | Azione |
|---|---|---|
| **Tailscale login** | è un nodo nuovo, non si duplica | apri l'ingress dell'add-on e autentica come `ha-wagmi` |
| **SSH add-on** | credenziali nuove per sicurezza | imposta `SSH_ADDON_PASSWORD` / `SSH_AUTHORIZED_KEY` in `.env` |
| **Nginx Proxy Manager** | i proxy-host stanno nel DB SQLite dell'add-on | ricreali dalla UI, **oppure** copia `/addon_configs/<slug>_nginxproxymanager/` da Albani |
| **Integrazioni/device** | esclusi per richiesta | riconfigura solo ciò che serve su Wagmi |
| **Lovelace plugins** | risorse frontend | dopo HACS, verifica Impostazioni → Dashboard → Risorse |

## Note backend JARVIS HA Memory
Le options puntano ad **atomman** (`100.88.84.81`: chroma:8000, ollama:11434,
embeddings:11435). Se Wagmi deve usare un backend diverso, modifica
`local_addon.options` in `manifest.yaml` prima di lanciare. `location_id=wagmi`
tiene i dati separati da quelli di Albani sullo stesso ChromaDB.

## Sicurezza
- `.env` è git-ignored. Non committarlo.
- Le credenziali SSH/HA di Albani **non** vengono riusate: questo flusso genera
  segreti nuovi per la VM Wagmi.
