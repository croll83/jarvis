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

## Installazione

```bash
sudo mkdir -p /opt/jarvis/gliner-router
sudo cp server.py /opt/jarvis/gliner-router/
sudo cp gliner-router.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gliner-router
curl -s localhost:11436/health
```

Il venv `/home/jarvis/gliner-eval` ha `gliner2`, `torch` cuda, `fastapi`,
`uvicorn`. VRAM occupata: ~563 MiB (fp16 quantizzato).
