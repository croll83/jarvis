#!/usr/bin/env python3
"""Aggiunge al comfyui-dashboard gli endpoint di switch modalita' GB10.

Idempotente: se il marker e' gia' presente non fa nulla.
Va eseguito sul GB10, in /home/jarvis/comfyui-dashboard/.
"""
import ast
import json
import sys

PATH = "/home/jarvis/comfyui-dashboard/app.py"
MARKER = "GB10_MODE_BIN"

BLOCK = '''# -- Modalita GB10 (LLM <-> creator) -----------------------------------------
# La pool di memoria unificata del GB10 non regge vllm e la pipeline video
# insieme, quindi le due modalita sono mutuamente esclusive. Lo switch e'
# delegato a gb10-mode, che applica le safety di memoria (mai sovrapporle:
# saturare la pool blocca il kernel senza OOM ne' log).


@app.exception_handler(httpx.ConnectError)
async def _comfy_offline(request: Request, exc: httpx.ConnectError):
    """ComfyUI spento non e' un errore del server: e' la modalita LLM attiva."""
    return JSONResponse(
        status_code=503,
        content={
            "error": "creator_offline",
            "detail": "ComfyUI non risponde: il GB10 e' in modalita LLM. "
                      "Passa alla modalita creator per usare questa funzione.",
        },
    )


async def _mode_status() -> dict:
    proc = await asyncio.create_subprocess_exec(
        GB10_MODE_BIN, "status",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    try:
        return json.loads(out.decode() or "{}")
    except json.JSONDecodeError:
        raise HTTPException(500, f"gb10-mode status illeggibile: {err.decode()[:200]}")


@app.get("/api/mode")
async def get_mode():
    """Modalita corrente + salute di ogni servizio. Sola lettura."""
    return await _mode_status()


@app.post("/api/mode/switch")
async def switch_mode(payload: dict):
    """Avvia lo switch in background: dura minuti (vllm carica ~98 GiB)."""
    target = (payload or {}).get("mode")
    if target not in ("llm", "creator"):
        raise HTTPException(400, "mode deve essere 'llm' o 'creator'")

    current = await _mode_status()
    if current.get("busy"):
        raise HTTPException(409, "uno switch e' gia' in corso")
    if current.get("mode") == target:
        return {"accepted": False, "reason": "already-in-mode", "mode": target}

    # start_new_session: lo switch deve sopravvivere alla richiesta HTTP.
    subprocess.Popen(
        [GB10_MODE_BIN, target],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"accepted": True, "target": target, "from": current.get("mode")}


'''

IMPORT_OLD = (
    "import httpx\n"
    "from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect\n"
    "from fastapi.responses import FileResponse, Response\n"
)
IMPORT_NEW = (
    "import asyncio\n"
    "import subprocess\n"
    "\n"
    "import httpx\n"
    "from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect\n"
    "from fastapi.responses import FileResponse, JSONResponse, Response\n"
)

CONST_ANCHOR = 'GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"'
CONST_ADD = (
    "\n\n# Switch di modalita del GB10 (LLM <-> creator). Vedi: gb10-mode --help\n"
    'GB10_MODE_BIN = os.environ.get("GB10_MODE_BIN", "/usr/local/bin/gb10-mode")'
)

ROOT_ANCHOR = '@app.get("/")'


def main() -> int:
    src = open(PATH).read()

    if MARKER in src:
        print("gia' patchato, nulla da fare")
        return 0

    out = src
    if IMPORT_OLD not in out:
        print("ERRORE: blocco import non trovato", file=sys.stderr)
        return 1
    out = out.replace(IMPORT_OLD, IMPORT_NEW, 1)

    if CONST_ANCHOR not in out:
        print("ERRORE: anchor GEMINI_URL non trovato", file=sys.stderr)
        return 1
    out = out.replace(CONST_ANCHOR, CONST_ANCHOR + CONST_ADD, 1)

    n = out.count(ROOT_ANCHOR)
    if n != 1:
        print(f"ERRORE: anchor root trovato {n} volte, atteso 1", file=sys.stderr)
        return 1
    out = out.replace(ROOT_ANCHOR, BLOCK + ROOT_ANCHOR, 1)

    try:
        ast.parse(out)
    except SyntaxError as e:
        print(f"ERRORE: il risultato non compila: {e}", file=sys.stderr)
        return 1

    open(PATH, "w").write(out)
    print("app.py patchato, sintassi valida")
    return 0


if __name__ == "__main__":
    sys.exit(main())
