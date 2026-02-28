# Agent Behavior

## Session Management
- Ogni cron job crea una sessione isolata
- La sessione si chiude al termine del task
- Non mantenere stato tra sessioni — usa ontology per persistence

## Memory
- **MEMORY.md**: memoria a lungo termine (strategie, lezioni, config)
- **memory/YYYY-MM-DD.md**: log giornaliero (trade eseguiti, decisioni, anomalie)
- Aggiorna MEMORY.md solo per informazioni durature (nuove lezioni, cambi config)
- Aggiorna il daily log per ogni sessione di trading significativa

## Comunicazione
- Invia report a Marco su Telegram (channel=telegram, to=${TELEGRAM_ADMIN_ID}) SOLO per:
  - Daily report (10:00 + 22:00)
  - Alert significativi (perdite >5%, crash, anomalie)
- NON inviare notifiche per ogni singolo trade — Marco non vuole spam

## Error Handling
- Se un tool fallisce: riprova 1 volta, poi logga l'errore e vai avanti
- Se l'API HL e down: logga e skip il ciclo, riprova al prossimo cron
- Se ontology e down: procedi senza, ma logga il problema
- Non bloccarti mai su un errore — il prossimo cron ciclo ripartira comunque
