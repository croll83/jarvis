#!/usr/bin/env python3
# =============================================================================
# HAOS replicate — installa lo stack (add-on + HACS) su una NUOVA istanza HA
# =============================================================================
# Legge manifest.yaml, risolve i ${SEGRETI} da ambiente, e usa la WebSocket
# API di Home Assistant (proxy Supervisor, richiede token di un utente admin)
# per:
#   1. aggiungere gli store-repository
#   2. installare gli add-on + applicare opzioni/boot/auto_update + avviarli
#   3. (best-effort) installare i repo HACS
#
# NON tocca integrazioni, device, dashboard o automazioni.
#
# Uso:
#   export HASS_URL=https://<nuovo-haos>:8123
#   export HASS_TOKEN=<long-lived-token-del-nuovo-haos>
#   python3 replicate.py [--dry-run] [--skip-hacs] [--only addons|hacs|local]
#
# Idempotente: salta ciò che è già installato.
# =============================================================================
import argparse
import asyncio
import json
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("Manca PyYAML: pip install pyyaml")
try:
    import websockets
except ImportError:
    sys.exit("Manca websockets: pip install websockets")

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "manifest.yaml")


# ---------- util ----------
def log(msg, lvl="•"):
    print(f"[{lvl}] {msg}", flush=True)


def subst_env(obj):
    """Sostituisce ricorsivamente ${VAR} con os.environ (vuoto se assente)."""
    if isinstance(obj, str):
        out = obj
        while "${" in out:
            i = out.index("${")
            j = out.index("}", i)
            var = out[i + 2:j]
            out = out[:i] + os.environ.get(var, "") + out[j + 1:]
        return out
    if isinstance(obj, list):
        return [subst_env(x) for x in obj]
    if isinstance(obj, dict):
        return {k: subst_env(v) for k, v in obj.items()}
    return obj


def ws_url(http_url):
    u = http_url.rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://"):] + "/api/websocket"
    if u.startswith("http://"):
        return "ws://" + u[len("http://"):] + "/api/websocket"
    return u + "/api/websocket"


# ---------- WS client ----------
class HA:
    def __init__(self, url, token):
        self.url = ws_url(url)
        self.token = token
        self.ws = None
        self._id = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=None, open_timeout=30)
        hello = json.loads(await self.ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Handshake inatteso: {hello}")
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        res = json.loads(await self.ws.recv())
        if res.get("type") != "auth_ok":
            raise RuntimeError(f"Auth fallita: {res}")
        return self

    async def __aexit__(self, *a):
        if self.ws:
            await self.ws.close()

    async def cmd(self, payload, timeout=600):
        self._id += 1
        mid = self._id
        payload = dict(payload, id=mid)
        await self.ws.send(json.dumps(payload))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if msg.get("id") == mid and msg.get("type") == "result":
                return msg

    async def sup(self, endpoint, method="get", data=None, timeout=600):
        p = {"type": "supervisor/api", "endpoint": endpoint, "method": method, "timeout": timeout}
        if data is not None:
            p["data"] = data
        return await self.cmd(p, timeout=timeout + 30)


# ---------- steps ----------
async def add_store_repos(ha, repos, dry):
    log("Store repositories…", "STORE")
    cur = await ha.sup("/store", "get")
    existing = {r["source"] for r in cur.get("result", {}).get("repositories", [])} if cur.get("success") else set()
    for r in repos:
        url = r["url"]
        if url in existing:
            log(f"già presente: {url}", "=")
            continue
        if dry:
            log(f"[dry] add repo {url}", "+")
            continue
        res = await ha.sup("/store/repositories", "post", {"repository": url})
        log(f"add repo {url} -> {'ok' if res.get('success') else res.get('error')}",
            "+" if res.get("success") else "!")
    if not dry:
        await ha.sup("/store/reload", "post")
        log("store reloaded", "=")


async def addon_slug_maps(ha):
    """name -> slug per add-on installati e disponibili nello store."""
    installed, available = {}, {}
    r = await ha.sup("/addons", "get")
    for a in r.get("result", {}).get("addons", []):
        installed[a["name"]] = a["slug"]
    s = await ha.sup("/store/addons", "get")
    store = s.get("result", {})
    items = store.get("addons", store) if isinstance(store, dict) else store
    for a in items:
        available[a["name"]] = a["slug"]
    return installed, available


async def install_addon(ha, spec, installed, available, dry):
    name = spec["match_name"]
    slug = installed.get(name) or available.get(name)
    if not slug:
        log(f"slug non risolto per '{name}' (repo aggiunto? store reloaded?)", "!")
        return
    if name not in installed:
        if dry:
            log(f"[dry] install {slug}", "+")
        else:
            log(f"install {slug}…", "+")
            res = await ha.sup(f"/addons/{slug}/install", "post")
            if not res.get("success"):
                log(f"install {slug} FALLITO: {res.get('error')}", "!")
                return
    else:
        log(f"già installato: {slug}", "=")

    # opzioni + boot + auto_update in un'unica POST
    body = {}
    if spec.get("options") is not None:
        body["options"] = spec["options"]
    if "boot" in spec:
        body["boot"] = spec["boot"]
    if "auto_update" in spec:
        body["auto_update"] = spec["auto_update"]
    if body:
        if dry:
            log(f"[dry] options {slug}: {list(body)}", "·")
        else:
            res = await ha.sup(f"/addons/{slug}/options", "post", body)
            log(f"options {slug} -> {'ok' if res.get('success') else res.get('error')}",
                "·" if res.get("success") else "!")

    # start
    if dry:
        log(f"[dry] start {slug}", ">")
    else:
        res = await ha.sup(f"/addons/{slug}/start", "post")
        log(f"start {slug} -> {'ok' if res.get('success') else res.get('error')}",
            ">" if res.get("success") else "!")


async def install_local_addon(ha, spec, dry):
    slug = spec["slug"]
    log(f"Add-on locale {slug}…", "LOCAL")
    # reload per far apparire la cartella copiata in /addons
    if not dry:
        await ha.sup("/store/reload", "post")
    installed, available = await addon_slug_maps(ha)
    spec2 = dict(spec, match_name=spec["match_name"])
    # se non è né installato né disponibile, la cartella non è stata copiata
    if spec["match_name"] not in installed and spec["match_name"] not in available:
        log(f"'{spec['match_name']}' non trovato: copia prima {spec['dest_dir']} "
            f"sull'HAOS (usa SSH_TARGET in replicate.sh).", "!")
        return
    await install_addon(ha, spec2, installed, available, dry)


async def install_hacs(ha, hacs, dry):
    log("HACS (best-effort)…", "HACS")
    # verifica che HACS sia presente
    probe = await ha.cmd({"type": "hacs/repositories/list"})
    if not probe.get("success"):
        log("HACS non installato/configurato: installa HACS prima (vedi README). "
            "Repos da aggiungere a mano:", "!")
        for cat in ("integrations", "plugins"):
            for it in hacs.get(cat, []):
                log(f"  [{cat}] {it['full_name']}", " ")
        return

    known = {r.get("full_name"): r for r in probe.get("result", [])}
    for cat, hacs_cat in (("integrations", "integration"), ("plugins", "plugin")):
        for it in hacs.get(cat, []):
            fn = it["full_name"]
            rec = known.get(fn)
            if rec and rec.get("installed"):
                log(f"già installato: {fn}", "=")
                continue
            if dry:
                log(f"[dry] HACS download {fn} ({hacs_cat})", "+")
                continue
            # registra il repo se sconosciuto, poi scarica
            if not rec:
                await ha.cmd({"type": "hacs/repositories/add",
                              "repository": fn, "category": hacs_cat})
                probe = await ha.cmd({"type": "hacs/repositories/list"})
                known = {r.get("full_name"): r for r in probe.get("result", [])}
                rec = known.get(fn)
            if not rec:
                log(f"impossibile registrare {fn} — aggiungilo a mano in HACS", "!")
                continue
            res = await ha.cmd({"type": "hacs/repository/download", "repository": rec.get("id")})
            log(f"download {fn} -> {'ok' if res.get('success') else res.get('error')}",
                "+" if res.get("success") else "!")
    log("Dopo HACS: riavvia HA Core e, per i plugin lovelace, verifica le "
        "risorse in Impostazioni → Dashboard → Risorse.", " ")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-hacs", action="store_true")
    ap.add_argument("--only", choices=["addons", "hacs", "local"], default=None)
    args = ap.parse_args()

    url = os.environ.get("HASS_URL")
    token = os.environ.get("HASS_TOKEN")
    if not url or not token:
        sys.exit("Imposta HASS_URL e HASS_TOKEN (vedi .env.example).")

    with open(MANIFEST) as f:
        manifest = subst_env(yaml.safe_load(f))

    log(f"Target: {url}  (dry-run={args.dry_run})", "INIT")
    async with HA(url, token) as ha:
        cfg = await ha.cmd({"type": "get_config"})
        ver = cfg.get("result", {}).get("version", "?")
        log(f"Connesso a HA {ver}", "OK")

        do = lambda x: args.only in (None, x)

        if do("addons"):
            await add_store_repos(ha, manifest.get("store_repositories", []), args.dry_run)
            installed, available = await addon_slug_maps(ha)
            for spec in manifest.get("addons", []):
                await install_addon(ha, spec, installed, available, args.dry_run)

        if do("local") and manifest.get("local_addon"):
            await install_local_addon(ha, manifest["local_addon"], args.dry_run)

        if do("hacs") and not args.skip_hacs and manifest.get("hacs"):
            await install_hacs(ha, manifest["hacs"], args.dry_run)

    log("Fatto.", "DONE")


if __name__ == "__main__":
    asyncio.run(main())
