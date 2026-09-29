"""Test di integrazione del wakeword-server patchato: finto device + finto orchestrator.
Gira dentro l'immagine del wakeword-server (ha fastapi, websockets, opuslib, openwakeword)."""
import asyncio
import json
import os
import sys

import uvicorn
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

ORCH_PORT, WW_PORT = 18501, 18502
os.environ["ORCHESTRATOR_WS_URL"] = f"ws://127.0.0.1:{ORCH_PORT}/ws/audio"
os.environ["DEVICE_API_TOKEN"] = "tok"
sys.path.insert(0, "/app")

orch = FastAPI()
orch_events = []
orch_state = {}


@orch.websocket("/ws/audio")
async def orch_ws(ws: WebSocket):
    await ws.accept()
    dev = ws.query_params.get("device_id")
    orch_events.append(("relay_open", dev))
    orch_state["ws"] = ws
    try:
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        orch_events.append(("relay_closed", dev))


async def serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.05)
    return srv


async def wait_for(pred, timeout=3.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


async def main():
    import httpx
    import server as ww   # modulo patchato
    ww._TTS_PREPARE_TIMEOUT_S = 2.0   # watchdog corto per il test
    await serve(orch, ORCH_PORT)
    await serve(ww.app, WW_PORT)

    dev_msgs = []
    dev = await websockets.connect(f"ws://127.0.0.1:{WW_PORT}/ws/audio?device_id=AABBCCDDEEFF&token=tok")

    async def dev_reader():
        async for m in dev:
            dev_msgs.append(m if isinstance(m, str) else f"<bin {len(m)}>")
    asyncio.create_task(dev_reader())
    assert await wait_for(lambda: "AABBCCDDEEFF" in ww._connections)
    conn = ww._connections["AABBCCDDEEFF"]
    assert conn.state == ww.DeviceState.IDLE
    base = f"http://127.0.0.1:{WW_PORT}/api/prepare_tts"
    H = {"Authorization": "Bearer tok"}

    def relay_open():
        return conn.relay is not None and conn.relay.is_connected

    async with httpx.AsyncClient() as c:
        assert (await c.post(f"{base}/AABBCCDDEEFF")).status_code == 401
        assert (await c.post(f"{base}/XXXXXXXXXXXX", headers=H)).status_code == 404

        # 1) prepare -> relay aperto verso l'orchestrator, device BUSY
        r = await c.post(f"{base}/AABBCCDDEEFF", headers=H)
        assert r.status_code == 200, r.text
        assert await wait_for(lambda: ("relay_open", "AABBCCDDEEFF") in orch_events), orch_events
        assert conn.state == ww.DeviceState.BUSY and relay_open()
        assert (await c.post(f"{base}/AABBCCDDEEFF", headers=H)).status_code == 409

        # 2) l'orchestrator parla: tts_start, frame, tts_done -> inoltrati al device, IDLE, relay chiuso
        ows = orch_state["ws"]
        await ows.send_text(json.dumps({"type": "tts_start"}))
        await ows.send_bytes(b"\x01" * 40)
        await ows.send_text(json.dumps({"type": "tts_done"}))
        assert await wait_for(lambda: any('"tts_done"' in m for m in dev_msgs)), dev_msgs
        assert any('"tts_start"' in m for m in dev_msgs) and "<bin 40>" in dev_msgs, dev_msgs
        assert await wait_for(lambda: conn.state == ww.DeviceState.IDLE and conn.tts_prepare_token is None)
        assert await wait_for(lambda: not relay_open())
        print("1-2) prepare -> BUSY + relay; tts_start/frame/tts_done inoltrati; IDLE + relay chiuso")

        # 3) watchdog: prepare senza tts_done -> dopo il timeout torna IDLE e chiude il relay
        assert (await c.post(f"{base}/AABBCCDDEEFF", headers=H)).status_code == 200
        assert await wait_for(relay_open)
        assert conn.state == ww.DeviceState.BUSY
        assert await wait_for(lambda: conn.state == ww.DeviceState.IDLE, timeout=4.0), conn.state
        assert await wait_for(lambda: not relay_open())
        print("3) watchdog: senza tts_done torna IDLE e chiude il relay")

        # 4) wake word vera durante un avviso: il watchdog non deve piu' toccare il device
        assert (await c.post(f"{base}/AABBCCDDEEFF", headers=H)).status_code == 200
        conn.tts_prepare_token = None      # quello che fa _on_wake_detected
        conn.state = ww.DeviceState.STREAMING
        await asyncio.sleep(2.4)
        assert conn.state == ww.DeviceState.STREAMING and relay_open(), conn.state
        print("4) watchdog disinnescato da una sessione vera")
    await dev.close()
    print("ALL OK")


asyncio.run(main())
