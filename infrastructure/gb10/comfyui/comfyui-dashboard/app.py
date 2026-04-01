"""ComfyUI Dashboard — lightweight web UI for Phr00t image generation workflows."""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

COMFY = "http://127.0.0.1:8188"
OLLAMA_SYSTEM = (
    "You are an expert AI image generation prompt engineer. "
    "Take the user's description (in any language) and rewrite it as a detailed, "
    "descriptive English prompt optimized for AI image generation. Include details "
    "about composition, lighting, style, colors, mood, and camera angle. Keep it "
    "concise (max 150 words) but rich in visual detail. Output ONLY the rewritten "
    "prompt, nothing else."
)

app = FastAPI(title="ComfyUI Dashboard")

# ---------------------------------------------------------------------------
# Workflow builders
# ---------------------------------------------------------------------------

def _base_nodes(prompt: str, width: int, height: int, seed: int | None,
                refine: bool, batch: int = 1, steps: int = 6,
                cfg: float = 1.0, sampler_name: str = "euler_ancestral",
                scheduler: str = "beta", denoise: float = 1.0) -> dict:
    """Return shared nodes: checkpoint, empty latent, positive/negative encoder, sampler, decode, save."""
    s = seed if seed is not None else random.randint(0, 2**63)
    nodes: dict = {
        "ckpt": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "Qwen-Rapid-AIO-NSFW-v23.safetensors"},
        },
        "latent": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": batch},
        },
        "positive": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {
                "clip": ["ckpt", 1],
                "prompt": prompt if not refine else ["ollama_gen", 0],
                "vae": ["ckpt", 2],
                "target_latent": ["latent", 0],
            },
        },
        "negative": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {
                "clip": ["ckpt", 1],
                "prompt": "",
                "vae": ["ckpt", 2],
                "target_latent": ["latent", 0],
            },
        },
        "sampler": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["ckpt", 0],
                "positive": ["positive", 0],
                "negative": ["negative", 0],
                "latent_image": ["latent", 0],
                "seed": s,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "denoise": denoise,
            },
        },
        "decode": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sampler", 0], "vae": ["ckpt", 2]},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"images": ["decode", 0], "filename_prefix": "Dashboard"},
        },
    }
    if refine:
        nodes["ollama_conn"] = {
            "class_type": "OllamaConnectivityV2",
            "inputs": {
                "url": "http://127.0.0.1:11434",
                "model": "qwen3.5-128k:latest",
                "keep_alive": 5,
                "keep_alive_unit": "minutes",
            },
        }
        nodes["ollama_gen"] = {
            "class_type": "OllamaGenerateV2",
            "inputs": {
                "connectivity": ["ollama_conn", 0],
                "system": OLLAMA_SYSTEM,
                "prompt": prompt,
                "think": False,
                "keep_context": False,
                "format": "text",
            },
        }
    return nodes


def build_t2i(prompt: str, width: int, height: int, seed: int | None,
              refine: bool, **kw) -> dict:
    return _base_nodes(prompt, width, height, seed, refine, **kw)


def build_edit(prompt: str, width: int, height: int, seed: int | None,
               refine: bool, image: str, **kw) -> dict:
    nodes = _base_nodes(prompt, width, height, seed, refine, **kw)
    nodes["img1"] = {
        "class_type": "LoadImage",
        "inputs": {"image": image, "upload": "image"},
    }
    nodes["positive"]["inputs"]["image1"] = ["img1", 0]
    return nodes


def build_fusion(prompt: str, width: int, height: int, seed: int | None,
                 refine: bool, images: list[str], gates: list[bool], **kw) -> dict:
    nodes = _base_nodes(prompt, width, height, seed, refine, **kw)
    for i, (img, enabled) in enumerate(zip(images, gates)):
        n = i + 1
        load_key = f"img{n}"
        gate_key = f"gate{n}"
        nodes[load_key] = {
            "class_type": "LoadImage",
            "inputs": {"image": img, "upload": "image"},
        }
        if n == 1:
            # Image 1 always connected directly
            nodes["positive"]["inputs"]["image1"] = [load_key, 0]
        else:
            nodes[gate_key] = {
                "class_type": "ImageGate",
                "inputs": {"enabled": enabled, "image": [load_key, 0]},
            }
            nodes["positive"]["inputs"][f"image{n}"] = [gate_key, 0]
    return nodes


def _assign_numeric_ids(nodes: dict) -> dict:
    """Convert string keys to numeric IDs for ComfyUI API."""
    key_to_id = {}
    for idx, key in enumerate(nodes, start=1):
        key_to_id[key] = str(idx)
    result = {}
    for key, node in nodes.items():
        nid = key_to_id[key]
        new_inputs = {}
        for iname, ival in node["inputs"].items():
            if isinstance(ival, list) and len(ival) == 2 and isinstance(ival[0], str) and ival[0] in key_to_id:
                new_inputs[iname] = [key_to_id[ival[0]], ival[1]]
            else:
                new_inputs[iname] = ival
        result[nid] = {"class_type": node["class_type"], "inputs": new_inputs}
    return result


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/generate")
async def generate(data: dict):
    mode = data.get("mode", "t2i")
    prompt = data.get("prompt", "")
    width = data.get("width", 1024)
    height = data.get("height", 1024)
    seed = data.get("seed")
    refine = data.get("refine", True)
    # Sampler parameters
    kw = {
        "batch": data.get("batch", 1),
        "steps": data.get("steps", 6),
        "cfg": data.get("cfg", 1.0),
        "sampler_name": data.get("sampler_name", "euler_ancestral"),
        "scheduler": data.get("scheduler", "beta"),
        "denoise": data.get("denoise", 1.0),
    }

    if mode == "t2i":
        nodes = build_t2i(prompt, width, height, seed, refine, **kw)
    elif mode == "edit":
        nodes = build_edit(prompt, width, height, seed, refine, data.get("image", ""), **kw)
    elif mode == "fusion":
        images = data.get("images", [])
        gates = data.get("gates", [True] * len(images))
        nodes = build_fusion(prompt, width, height, seed, refine, images, gates, **kw)
    else:
        return {"error": f"Unknown mode: {mode}"}

    workflow = _assign_numeric_ids(nodes)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{COMFY}/prompt", json={"prompt": workflow})
        return resp.json()


@app.get("/api/samplers")
async def get_samplers():
    """Return available sampler names and schedulers from ComfyUI."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{COMFY}/object_info/KSampler")
        info = resp.json()
    ks = info.get("KSampler", {}).get("input", {}).get("required", {})
    return {
        "samplers": ks.get("sampler_name", [[]])[0],
        "schedulers": ks.get("scheduler", [[]])[0],
    }


@app.post("/api/upload")
async def upload(file: UploadFile):
    content = await file.read()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{COMFY}/upload/image",
            files={"image": (file.filename, content, file.content_type or "image/png")},
            data={"overwrite": "true"},
        )
        return resp.json()


@app.get("/api/history")
async def history(limit: int = 50):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{COMFY}/history", params={"max_items": limit})
        raw = resp.json()
    # Flatten into a sorted list
    items = []
    for pid, data in raw.items():
        status = data.get("status", {})
        if not status.get("completed"):
            continue
        outputs = data.get("outputs", {})
        for nid, out in outputs.items():
            for img in out.get("images", []):
                items.append({
                    "prompt_id": pid,
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                })
    return items


@app.get("/api/image/{filename}")
async def get_image(filename: str, subfolder: str = "", type: str = "output"):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{COMFY}/view",
            params={"filename": filename, "subfolder": subfolder, "type": type},
        )
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/png"),
            headers={"Cache-Control": "public, max-age=3600"},
        )


@app.websocket("/api/ws")
async def ws_proxy(ws: WebSocket):
    await ws.accept()
    import websockets
    try:
        async with websockets.connect(f"ws://127.0.0.1:8188/ws?clientId={uuid.uuid4()}") as comfy_ws:
            import asyncio

            async def forward_to_client():
                async for msg in comfy_ws:
                    if isinstance(msg, str):
                        await ws.send_text(msg)

            async def forward_to_comfy():
                while True:
                    data = await ws.receive_text()
                    await comfy_ws.send(data)

            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_to_client()),
                 asyncio.create_task(forward_to_comfy())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        pass


# Static files & index
STATIC = Path(__file__).parent / "static"

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")

app.mount("/static", StaticFiles(directory=STATIC), name="static")
