"""Watcher esiti scenari — annuncio differito degli errori.

Il path voce è fire-and-forget ("Avvio Buonanotte" in <1s): se poi il run
fallisce, l'utente non lo saprebbe mai. Qui:

- `watch_scenario_run`: sorveglia il run appena avviato (trace HA via WS) e,
  SOLO in caso di errore, annuncia sul device che ha chiesto lo scenario.
  Successo = silenzio (il registro esiti vive nel campanello della dashboard).
- `scenario_error_sweep_loop`: rete di sicurezza per i run NON voce
  (schedulati, HA UI): ogni 5 min cerca run in errore recenti sugli scenari
  noti e notifica via Telegram.

Formato trace (verificato sul campo, run Buonanotte 2026-07-30):
  trace/list → [{run_id, timestamp: {start}, state, script_execution, error}]
  script_execution: finished | error | failed_single | cancelled ...
"""
import asyncio
import logging
import time
from datetime import datetime

import config
from multi_ha import multi_ha

logger = logging.getLogger("JARVIS_SCENARIO_WATCH")

WATCH_POLL_S = 3
WATCH_TIMEOUT_S = 150
SWEEP_INTERVAL_S = 300


def _short_error(err: str) -> str:
    """Prima riga dell'errore, senza tecnicismi inutili a voce."""
    if not err:
        return "errore sconosciuto"
    e = str(err).strip().splitlines()[0]
    e = e.replace("[Errno 104] Connection reset by peer", "connessione col dispositivo interrotta")
    e = e.replace("Connection reset by peer", "connessione col dispositivo interrotta")
    return e[:180]


def _run_start_ts(trace_entry: dict) -> float:
    try:
        raw = (trace_entry.get("timestamp") or {}).get("start") or ""
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


async def _resolve_trace_target(location: str, entity_id: str) -> tuple:
    """(domain, item_id) per trace/list.

    I nostri scenari sono script-wrapper di automazioni omonime
    (script.buonanotte → automation.buonanotte): l'errore vero vive nella
    trace dell'AUTOMAZIONE, quindi se esiste la gemella si guarda quella.
    Per le automazioni item_id = id numerico di config, non lo slug.
    """
    slug = entity_id.split(".", 1)[1]
    if entity_id.startswith("script."):
        twin = await multi_ha.get_state(location, f"automation.{slug}")
        if twin:
            num_id = (twin.get("attributes") or {}).get("id")
            if num_id:
                return "automation", str(num_id)
        return "script", slug
    st = await multi_ha.get_state(location, entity_id)
    num_id = ((st or {}).get("attributes") or {}).get("id")
    return "automation", str(num_id) if num_id else slug


async def watch_scenario_run(location: str, entity_id: str, label: str,
                             context: dict, deliver, trigger_ts: float) -> None:
    """Sorveglia il run di uno scenario appena avviato; annuncia solo i guai."""
    try:
        domain, item_id = await _resolve_trace_target(location, entity_id)
        deadline = time.time() + WATCH_TIMEOUT_S
        run = None
        while time.time() < deadline:
            await asyncio.sleep(WATCH_POLL_S)
            ok, res = await multi_ha.ws_command(location, {
                "type": "trace/list", "domain": domain, "item_id": item_id})
            if not ok or not isinstance(res, list):
                continue
            # run più recente partita dopo il trigger (10s di tolleranza clock)
            cand = None
            for t in res:
                if _run_start_ts(t) >= trigger_ts - 10:
                    cand = t
            if not cand:
                continue
            run = cand
            if cand.get("state") != "running":
                break
        if not run:
            logger.warning(f"watch {entity_id}: nessuna run trovata entro {WATCH_TIMEOUT_S}s")
            return
        exec_state = run.get("script_execution")
        error = run.get("error")
        if not error and exec_state in ("finished", "done", None):
            logger.info(f"watch {entity_id}: run completata ok")
            return
        if exec_state == "failed_single":
            msg = (f"Attenzione: {label} risultava già in esecuzione, "
                   f"il nuovo avvio è stato ignorato.")
        elif error:
            msg = f"Ho avuto un problema con {label}: {_short_error(error)}."
        else:
            msg = f"Lo scenario {label} non è andato a buon fine ({exec_state})."
        logger.warning(f"watch {entity_id}: esito={exec_state} err={error!r} → annuncio")
        await deliver(msg, context, sound_type="negative")
    except Exception as e:  # noqa: BLE001 — il watcher non deve mai far male al chiamante
        logger.error(f"scenario watcher {entity_id} fallito: {e}")


async def scenario_error_sweep_loop() -> None:
    """Rete di sicurezza per i run non-voce: errori recenti → Telegram."""
    if not config.SCENARIO_SWEEP_ENABLED:
        return
    from integrations import send_telegram
    location = None
    seen_runs: set = set()
    first_pass = True
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            if location is None:
                from database import get_default_location_id
                location = get_default_location_id() or "wagmi"
            for slug in config.SCENARIO_SWEEP_SLUGS:
                domain, item_id = await _resolve_trace_target(location, f"script.{slug}")
                ok, res = await multi_ha.ws_command(location, {
                    "type": "trace/list", "domain": domain, "item_id": item_id})
                if not ok or not isinstance(res, list):
                    continue
                for t in res:
                    rid = t.get("run_id")
                    if not rid or rid in seen_runs:
                        continue
                    seen_runs.add(rid)
                    # al primo giro censiamo lo storico senza notificare
                    if first_pass or t.get("state") == "running":
                        continue
                    error = t.get("error")
                    if error or t.get("script_execution") not in ("finished", "done", None):
                        await send_telegram(
                            f"⚠️ *Scenario {slug}* in errore: "
                            f"{_short_error(error or t.get('script_execution'))}")
                        logger.warning(f"sweep: {slug} run {rid[:8]} in errore → Telegram")
            first_pass = False
            if len(seen_runs) > 500:
                seen_runs = set(list(seen_runs)[-200:])
        except Exception as e:  # noqa: BLE001
            logger.error(f"scenario sweep fallito: {e}")
