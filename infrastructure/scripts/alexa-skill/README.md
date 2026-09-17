# Alexa Skill — Cloudflare tunnel + app (Wagmi)

Replica dello stack Alexa di Albani (CT200) su Wagmi (CT211). Due pezzi:

1. **Container + cloudflared** → gestiti da Terraform in
   `infrastructure/terraform/haos-wagmi/lxc_alexa.tf`.
2. **App Docker della skill** (questo dir) → `ma-alexa-skill` su `:5000`,
   buildata dal prototipo [`alams154/music-assistant-alexa-skill-prototype`](https://github.com/alams154/music-assistant-alexa-skill-prototype).

## Architettura (identica ad Albani)
```
Alexa  ──►  skill Amazon (SKILL_ID)
              │  endpoint = https://wagmialexa.mintwork.it
              ▼
        Cloudflare tunnel "wagmi-alexa"
              ├─ wagmialexa.mintwork.it       ──► 127.0.0.1:5000   (app ma-alexa-skill, nel CT)
              └─ wagmialexastream.mintwork.it ──► <HAOS_IP>:8097    (Music Assistant stream server)
```
- `:5000` = web app che traduce le directive Alexa in comandi Music Assistant.
- `:8097` = stream server di Music Assistant (add-on su HAOS) da cui gli Echo
  scaricano l'audio. `MA_HOSTNAME` nell'app punta qui.

## Riferimento Albani (estratto dal tunnel `cb8d7329…`)
| | Albani | Wagmi |
|---|---|---|
| route 1 | `albani20alexa.mintwork.it` → `127.0.0.1:5000` | `wagmialexa.mintwork.it` → `127.0.0.1:5000` |
| route 2 | `albani20stream.mintwork.it` → `192.168.1.18:8097` | `wagmialexastream.mintwork.it` → `<HAOS_IP>:8097` |
| SKILL_ID | `amzn1.ask.skill.bf594a54-…` | **da creare** (skill Amazon nuova) |

## Setup app (dentro al container CT211)
```bash
export MA_HOSTNAME="http://<HAOS_IP>:8097"
export SKILL_HOSTNAME="https://wagmialexa.mintwork.it"
export LOCALE="it-IT"                 # Echo italiani
export APP_USERNAME="alexamass"
export APP_PASSWORD="<password basic-auth>"
pct exec 211 -- bash /opt/alexa-skill/setup-app.sh   # dall'host pve-wagmi
```
`APP_USERNAME`/`APP_PASSWORD` proteggono la UI web e `/setup` con **basic auth**.
Il volume `ask_data` persiste le credenziali ASK CLI tra i restart.

## Creazione skill = flusso `/setup` (automatico)
**Non** serve creare la skill a mano nella Amazon Developer Console: apri
`https://wagmialexa.mintwork.it/setup` (login basic-auth `alexamass`/<password>)
e il flusso fa tutto: autorizzazione ASK CLI, creazione/aggiornamento skill,
upload interaction model, build e abilitazione test. Lo `SKILL_ID` viene gestito
dall'app (persistito in `ask_data`), per questo non è più una env var.

Prerequisiti lato Amazon:
- account sviluppatore: https://developer.amazon.com/en-US/docs/alexa/ask-overviews/create-developer-account.html
- "Skill Access Management" abilitato: https://developer.amazon.com/alexa/console/ask/settings/access-management
- guida upstream: https://github.com/alams154/music-assistant-alexa-skill-prototype#how-to-run

## Note
- `<HAOS_IP>` è in DHCP (ora `192.168.68.97`). Conviene una **prenotazione DHCP**
  sul router (MAC HAOS `BC:24:11:3E:20:64`); se cambia, aggiorna sia
  `MA_HOSTNAME` sia la route 2 del tunnel (vedi `manage-tunnel.sh`).
- Lo stream (`:8097`) funziona solo quando su HAOS Wagmi è installato e avviato
  l'add-on **Music Assistant** (vedi `../haos-replicate/`).
