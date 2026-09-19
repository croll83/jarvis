# gliner-router

Servizio di scoring GLiNER per il router dell'orchestrator.

## Perché un servizio a sé

Il container `jarvis_core` ha `torch 2.14.0+cpu` e nessun accesso alla GPU.
Misurato su CPU: bersaglio **1370ms**, azione 440ms, intento 155ms — circa 2
secondi in totale, peggio di Qwen 4B (1171ms). Sulla GPU le tre chiamate
costano **62ms** in tutto. Quindi il modello sta fuori dal container, come già
STT e TTS.

## Cosa NON fa

Non conosce la casa, non conosce gli intent, non decide niente. Riceve le
etichette e restituisce i punteggi. La politica vive in `router_model.py`
nell'orchestrator: quali intent esistono, quali azioni, come si risolve un
bersaglio. Le ancore dei vincoli si passano per nome, così il servizio non ha
bisogno di sapere cosa sia `HOME_CONTROL`.

## Rete

Il servizio ascolta su `0.0.0.0:11436` ed e' raggiungibile da **tutta la rete
Tailscale** — serve ad altri consumer oltre all'orchestrator (Hermes in primis).

Non e' esposto sulla LAN: `ufw` ha policy DROP e la porta e' aperta **solo** da
`100.64.0.0/10`:

```bash
sudo ufw allow from 100.64.0.0/10 to any port 11436 proto tcp \
    comment 'GLiNER router - Tailscale'
```

La regola vive in `/etc/ufw/user.rules` e sopravvive al riavvio — e' la stessa
convenzione degli altri servizi dell'host (11434 Ollama, 9000 STT, 8890 TTS).
Verificato: raggiungibile da un altro nodo Tailscale, rifiutato da `192.168.68.68`.

Bindare direttamente sull'IP Tailscale sarebbe piu' stretto, ma romperebbe
`localhost:11436` — che e' cio' che usa l'orchestrator sullo stesso host.

## Installazione

```bash
sudo mkdir -p /opt/jarvis/gliner-router
sudo cp server.py /opt/jarvis/gliner-router/
sudo cp gliner-router.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gliner-router
curl -s localhost:11436/health
```

Il venv `/home/jarvis/gliner-eval` ha `gliner2`, `torch` cuda, `fastapi`,
`uvicorn`. VRAM occupata: **~1,27 GiB** misurati con la NER spenta, **~2,77 GiB** con la
NER accesa (`GLINER_NER_ENABLED=true`): la NER richiede un secondo modello, perche'
il `Classifier` non estrae e l'`AutoExtractor` non applica vincoli. Con llama-server
a 4,5 GiB su una scheda da 8, accenderla lascia ~740 MiB e puo' far fallire
l'allocazione di un contesto lungo. Vecchia nota — 574 MiB di pesi piu' contesto CUDA e
spazi di lavoro. `quantize=True` non la riduce su questo percorso.
