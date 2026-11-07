"""Watchdog MyHOME (patch locale Jarvis) per anotherjulien/MyHOME 0.9.x.

Idempotente: rieseguirla non duplica niente. Backup *.bak-jarvis-<versione>.
Da riapplicare dopo OGNI aggiornamento HACS di MyHOME (sovrascrive la cartella).
Uso: python3 apply_watchdog.py /config/custom_components/myhome
"""
import json, re, shutil, sys
from pathlib import Path

D = Path(sys.argv[1])
ver = json.loads((D / "manifest.json").read_text())["version"]
MARK = "watchdog (patch locale Jarvis"

g = D / "gateway.py"; i = D / "__init__.py"
gs, is_ = g.read_text(), i.read_text()
if MARK in gs and MARK in is_:
    print(f"MyHOME {ver}: watchdog gia' presente, niente da fare"); sys.exit(0)
for f in (g, i):
    shutil.copy2(f, f.with_name(f.name + f".bak-jarvis-{ver}"))

def sost(s, a, b, cosa):
    if s.count(a) != 1:
        sys.exit(f"ERRORE: ancora non trovata ({cosa}) — la versione {ver} e' diversa, patch annullata")
    return s.replace(a, b)

# gateway.py
gs = sost(gs, "import asyncio\n", "import asyncio\nimport time\n", "import asyncio")
gs = sost(gs, "        self.send_buffer = asyncio.Queue()\n",
          "        self.send_buffer = asyncio.Queue()\n"
          "        # watchdog (patch locale Jarvis): vedi watchdog_tick\n"
          "        self._last_msg_ts = time.monotonic()\n"
          "        self._last_reload_ts = 0.0\n", "send_buffer in __init__")
gs = sost(gs, "            message = await _event_session.get_next()\n",
          "            message = await _event_session.get_next()\n"
          "            self._last_msg_ts = time.monotonic()  # watchdog: battito della event session\n",
          "get_next nel listening_loop")
gs = gs.rstrip("\n") + '''

    # ── watchdog (patch locale Jarvis, da riapplicare dopo ogni update di MyHOME) ──
    # La 0.9.x avvia i worker una volta sola: se la event session cade in silenzio
    # HA resta "caricato" ma sordo, con stati sbagliati (05/11/2026: Sala Depandance
    # 'off' in HA mentre era accesa). Ogni 60 s: riavvia i worker morti, chiede lo
    # stato generale delle luci (rinfresca gli stati e tiene viva la sessione), e se
    # dal gateway non arriva niente da 240 s ricarica l'entry, al massimo una volta
    # ogni 300 s (il MyHomeServer1 blocca l'IP dopo raffiche di connessioni).
    WATCHDOG_SILENCE_S = 240
    WATCHDOG_RELOAD_EVERY_S = 300

    async def watchdog_tick(self, now=None):
        if self._terminate_listener:
            return
        try:
            if self.listening_worker is None or self.listening_worker.done():
                LOGGER.warning("%s Watchdog: listening worker fermo, lo riavvio", self.log_id)
                self.listening_worker = self.hass.loop.create_task(self.listening_loop())
            for i, w in enumerate(list(self.sending_workers)):
                if w is None or w.done():
                    LOGGER.warning("%s Watchdog: sending worker %s fermo, lo riavvio", self.log_id, i)
                    self.sending_workers[i] = self.hass.loop.create_task(self.sending_loop(i))
            await asyncio.wait_for(self.send_status_request(OWNLightingCommand.status("0")), timeout=30)
        except Exception as e:  # pylint: disable=broad-except — il watchdog non deve morire
            LOGGER.warning("%s Watchdog: tick fallito: %s", self.log_id, e)
        silenzio = time.monotonic() - self._last_msg_ts
        if (silenzio > self.WATCHDOG_SILENCE_S
                and time.monotonic() - self._last_reload_ts > self.WATCHDOG_RELOAD_EVERY_S):
            self._last_reload_ts = time.monotonic()
            LOGGER.warning("%s Watchdog: nessun messaggio dal gateway da %.0f s, ricarico l'integrazione",
                           self.log_id, silenzio)
            self.hass.async_create_task(self.hass.config_entries.async_reload(self.config_entry.entry_id))
'''

# __init__.py
is_ = sost(is_, "    # Pruning lose entities and devices from the registry\n",
           "    # watchdog (patch locale Jarvis): un timer di HA core, non un task nudo,\n"
           "    # cosi' non puo' morire in silenzio; si cancella da solo all'unload.\n"
           "    from datetime import timedelta\n"
           "    from homeassistant.helpers.event import async_track_time_interval\n"
           "    entry.async_on_unload(async_track_time_interval(\n"
           "        hass, hass.data[DOMAIN][entry.data[CONF_MAC]][CONF_ENTITY].watchdog_tick,\n"
           "        timedelta(seconds=60)))\n\n"
           "    # Pruning lose entities and devices from the registry\n", "punto dopo i worker")

g.write_text(gs); i.write_text(is_)
print(f"MyHOME {ver}: watchdog applicato (backup *.bak-jarvis-{ver})")
