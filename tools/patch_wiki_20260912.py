#!/usr/bin/env python3
"""Aggiorna la llm-wiki con i cambi dell'11-12 settembre 2026.

1. concepts/local-llm-heavy.md   — switch llama.cpp/Qwopus3.6 -> vLLM/Qwen3.8-Flash-Next
2. concepts/jarvis-infrastructure.md + quickref — swap STT Canary -> Parakeet-TDT v3
3. concepts/comfyui-gx10-setup.md — mutua esclusivita' ComfyUI <-> LLM (gb10-mode)
4. log.md  — append dell'azione (richiesto da SCHEMA.md)
5. index.md — local-llm-heavy non e' piu' STUB

Idempotente: ogni blocco ha un marker e viene saltato se gia' presente.
Va eseguito sulla workstation (100.116.99.9), in /home/jarvis/wiki.
"""
import re
import sys
from pathlib import Path

WIKI = Path("/home/jarvis/wiki")
TODAY = "2026-09-12"

# ─────────────────────────────────────────────────────── 1. local-llm-heavy

LLM_NEW_SECTION1 = """## 1. Stack in uso (2026-09-12)

**Engine:** **vLLM** (nightly `v0.1.dev20073+g8e685d198`) in Docker — container `vllm-fn-tp1`,
image `vllm/vllm-openai:qwen38-flash-next`. Sostituisce llama.cpp, che resta documentato
in §2.3 come stack precedente.

**Modello:** **Qwen3.8-Flash-Next**, checkpoint ablit
`drowzeys/keys-Qwen3.8-flash-next-ablit-Mia-Single-Spark-only` (34 shard, 98.57 GiB su disco).
E' uno splice della recipe *single-DGX-Spark* di Mia-AiLab sopra `Mia-AiLab/Qwen3.8-Flash-Next-NVFP4`.

| Caratteristica | Valore |
|---|---|
| Architettura | `Qwen3_8FlashNextForConditionalGeneration`, MoE 512 esperti / **10 attivi** per token, 48 layer, hidden 2560 |
| Quantizzazione | **NVFP4** sul corpo + **MXFP8** sull'attention `o_proj` |
| Context | 262144 nativo, **servito a 131072** (`YARN=0`; con `YARN=1` si arriva a 524288) |
| Speculative decode | **MTP** nativo, `num_speculative_tokens=3`, draft vocab ristretto (47k token EN+code) |
| Multimodale | encoder `qwen3_5_vision`: legge **immagini e video** (`image-text-to-text`). **Non genera** nulla di visivo → [[comfyui-gx10-setup]] |
| Served name | `dark-jarvis` su `:30000` (OpenAI-compatible). L'alias `dark-opus` **non esiste piu'** |
| Gestione | **non** systemd: `flash-next/start.sh` + `stop.sh` (Docker) |

> ⚠️ Il checkpoint e' **gated** su Hugging Face e non e' drop-in: l'`o_proj` MXFP8 lo rende
> incompatibile con gli NVFP4 experts-only di NVIDIA/RadixArk, con i GGUF e con le MLX.

### 1.1 Repo dei parametri — `/home/jarvis/flash-next/`

Tutto il profilo di lancio vive in `.env`, letto da `start.sh` (precedenza:
env della shell > `.env` > default interno). `./start.sh --no-launch` stampa il comando
senza avviare.

| Chiave `.env` | Valore | Nota |
|---|---|---|
| `ABLIT` | `1` | usa il checkpoint ablit gated (0 = Mia NVFP4 stock) |
| `PORT` | `30000` | |
| `SERVED_MODEL_NAME` | `dark-jarvis` | |
| `MAX_MODEL_LEN` | `131072` | `YARN_MAX_MODEL_LEN=524288` se `YARN=1` |
| `MTP_NUM_SPECULATIVE_TOKENS` | `3` | `0` disabilita MTP e libera 1.5 GiB |
| `KV_CACHE_DTYPE` | `fp8` | ~1.85x il pool KV |
| `MAMBA_SSM_CACHE_DTYPE` | `bfloat16` | stato ricorrente GDN |
| `MAX_NUM_SEQS` | `2` | |
| `MAX_NUM_BATCHED_TOKENS` | `2048` | ampiezza chunk di prefill; il commento nel file stesso nota che 8192 comprerebbe ~11% di prefill |
| `COMPILATION_MODE` | `0` | torch.compile disattivato |
| `HOST_RESERVE_GIB` | `34` | **safety critica**, vedi §1.3 |
| `HOST_SLACK_GIB` | `5` | cap cgroup del container = budget GPU + questo |
| `REQUIRE_IDLE_GPU` | `false` | start.sh **non** verifica da se' che la GPU sia libera |

Le patch GB10-specifiche stanno in `flash-next/files/` e vengono montate **read-only sopra
il pacchetto vllm dentro il container** (11 bind-mount): PLE offload (`ple_offload_layer.py`,
`worker.py`, `protocol.py`, `connector.py`), `ple_layer.py`, `mtp.py`, `qsa.py`, `qsa_ops`,
`modelopt.py`. Due bug dell'offload path su GB10 sono corretti li' (le stream memory op CUDA
non sono supportate su GB10 e deadlockavano il worker dopo la graph capture).

### 1.2 Reasoning effort — default patchato

Il chat template del checkpoint usa `reasoning_effort|default('xhigh')`, che inietta
*"think carefully, validate key assumptions, consider plausible alternatives"*: sul codice
il primo token **utile** arrivava dopo ~17s (mediana) e 1 prompt su 5 esauriva il budget in
reasoning senza mai scrivere la risposta.

Il default e' stato portato a `low` **lato server** (2026-09-12), patchando
`reasoning_effort|default('low')` nei due punti dove vive il template dello snapshot
(`chat_template.jinja` **e** il campo `chat_template` dentro `tokenizer_config.json`).
Backup `*.bak-20260912T152211` accanto ai file; rollback = ripristino + `docker restart vllm-fn-tp1`.
Scelta deliberatamente **trasparente per i client**, dato che Flash Next e' anche il
fallback di tutti i gateway di [[hermes-ai-agent|Hermes]].

Misurato su 15 run per livello, categoria code:

| `reasoning_effort` | time-to-first-**content** p50 | tok/s | MTP acc. | affidabilita' |
|---|---:|---:|---:|---:|
| `none` (thinking off) | 0.24s | 53.5 | 1.96 | 5/5 |
| **`low` (default attuale)** | **2.36s** | **53.1** | **1.94** | **15/15** |
| `medium` | 2.50s | 52.3 | 1.88 | 15/15 |
| `xhigh` (default del template) | 17.2s | 44.3 | 1.52 | 4/5 |

`low` e `medium` sono indistinguibili nel rumore. Gli override per singola richiesta
funzionano in entrambe le direzioni (`reasoning_effort: "xhigh"`, oppure
`enable_thinking: false` per spegnere il thinking).

> ⚠️ Le vLLM nightly streammano il reasoning in `delta.reasoning`, **non**
> `delta.reasoning_content`: chi parsa il campo vecchio misura TTFT falsati di ~17s.

### 1.3 Footprint memoria — la safety che tiene in piedi la macchina

La pool del GB10 e' **unificata** (~121.69 GiB condivisi CPU/GPU). Saturarla **blocca il
kernel senza OOM ne' log** — e' avvenuto tre volte il 2026-09-04. Il budget GPU e' percio'
limitato *dal lato host*: GMU × MemTotal non supera mai `MemTotal - HOST_RESERVE_GIB`.

| | Valore |
|---|---|
| Checkpoint su disco | 98.57 GiB |
| di cui tabella PLE | 26.82 GiB — **non sulla GPU**: servita dal worker CPU-offload da file **memory-mapped** (pagine evictable) |
| Pesi sulla GPU | 71.75 GiB |
| Overhead runtime | ~5.6 GiB |
| KV cache | ~8.8 GiB misurati all'ultimo avvio = 525.598 token |
| Processo vllm (misurato) | ~82.6 GiB |

Un watchdog (`files/memwatch.sh`) ferma il container se `MemAvailable` o `MemFree` restano
sotto soglia, archiviando prima il log.

**Avvio: ~10 minuti** (98 GiB di pesi + build della tabella PLE + autotune FlashInfer +
cudagraph capture). Va messo in conto in ogni switch di modalita' (§ [[comfyui-gx10-setup]]).

### 1.4 Performance misurate (2026-09-12, `thinking off`)

Benchmark riproducibile: `openclaw/notebooks/bench_v5_vllm.py` (erede di `bench_v4b.py`, che
leggeva i log llama.cpp e su vLLM non funziona). Misura TTFT, time-to-first-content, tok/s,
acceptance MTP da `/metrics` e **compila/parsa** davvero l'output generato.

| Categoria | TTFT p50 | tok/s | MTP acc. | affidabilita' |
|---|---:|---:|---:|---:|
| code | 236ms | 53.5 | 1.96/3 | 5/5 |
| prose | 259ms | 34.6 | 0.93/3 | 5/5 |
| structured | 275ms | 55.0 | 2.16/3 | 4/5 (1 troncamento a 512 token) |

Sono sopra del 50-65% sul throughput rispetto al baseline dell'autore del checkpoint
(`results/speed.json`: 31-35 tok/s su code), con TTFT e acceptance allineati.

**Prefill** (il costo del primo turno a contesto lungo):

| Prompt | A freddo | A caldo (prefix cache) |
|---|---:|---:|
| 4.551 tok | 2.20s | 2.22s |
| 18.160 tok | 7.47s | 1.65s |
| 36.295 tok | 9.75s | 2.41s |

La prefix cache e' attiva e sopra i 18k taglia il TTFT di ~4.5x; sotto i 4k non aiuta.
vLLM avvisa due volte che `max_num_batched_tokens=2048` e' stato clampato da MTP=3 ed e'
subottimale sul prefill lungo — **non modificato** per scelta.

---

"""

LLM_23_OLD = "### 2.3 Upstream stock + MTP — ATTUALE"
LLM_23_NEW = "### 2.3 Upstream stock + MTP — SUPERATO (in uso fino al 2026-09-11)"

LLM_24 = """
### 2.4 vLLM + Qwen3.8-Flash-Next — ATTUALE

**Quando:** 2026-09-11/12.

**Perche' si e' lasciato llama.cpp:** Flash Next e' un MoE 512-esperti con attention MXFP8
mista, PLE table da offloadare su CPU e MTP nativo — roba che upstream llama.cpp non serve.
Il checkpoint Mia-AiLab e' distribuito per **vLLM su singolo DGX Spark**, con le patch GB10
necessarie (§1.1).

**Cosa si guadagna:** MoE con 10 esperti attivi su 512 (capacita' molto superiore ai 27B densi
precedenti a parita' di banda), context 131k, **input multimodale con video**, e su codice
~53 tok/s contro i 20-26 t/s di Qwopus3.6 (§1.4 vs §1 storica).

**Cosa costa:** avvio di ~10 minuti contro pochi secondi, footprint che non lascia spazio
alla pipeline video sulla stessa macchina (da cui la mutua esclusivita', vedi
[[comfyui-gx10-setup]]), e un checkpoint gated non sostituibile a caldo.

**Predecessori sostituiti:** Qwopus3.6-v2 su llama.cpp (§2.3) e, su vLLM, il
`Huihui-Qwen3.5-27B-Claude-4.6-Opus-abliterated` BF16 provato a fine marzo 2026.

---
"""

# ─────────────────────────────────────────────── 2. STT: Canary -> Parakeet

INFRA_STT_OLD_PREFIX = "| Canary STT      | 9000  |"
INFRA_STT_NEW = (
    "| Parakeet STT    | 9000  | systemd `parakeet-stt.service` | **4.14 GiB** (BF16, misurata) "
    "| `nvidia/parakeet-tdt-0.6b-v3` — NeMo `EncDecRNNTBPEModel`, RNNT loss TDT. **Swap da Canary "
    "a Parakeet il 2026-09-12**: il modello non e' piu' hardcoded ma letto da env "
    "(`MODEL_NAME = os.environ.get(\"STT_MODEL\", \"nvidia/parakeet-tdt-0.6b-v3\")`), quindi il "
    "rollback e' `STT_MODEL=nvidia/canary-1b-v2` nel service + restart — entrambi i modelli "
    "restano in HF cache. `SUPPORTS_FORCED_LANG` e' ora condizionale (`\"canary\" in MODEL_NAME`): "
    "i parametri `source_lang`/`target_lang` valgono solo con Canary. **VRAM dimezzata**: 8.42 GiB "
    "(Canary BF16) → 4.14 GiB. Il naming smette di essere fuorviante — unit, script "
    "(`parakeet-gpu-server.py`), venv e log prefix ora corrispondono al modello caricato. "
    "**Verificato 2026-09-12** (codice + journal `Model EncDecRNNTBPEModel was successfully restored "
    "from .../parakeet-tdt-0.6b-v3.nemo` + `Model loaded in 6.2s on cuda:0 dtype=torch.bfloat16` + "
    "nvidia-smi PID 4098998 → 4243 MiB). Backup pre-swap: `parakeet-gpu-server.py.canary.bak`. "
    "Storia: Parakeet-TDT v3 → Canary (giu-lug 2026, l'auto-LID sbagliava IT→RU su audio corti) "
    "→ Parakeet-TDT v3 (2026-09-12) |"
)

INFRA_SVC_OLD = (
    "- `parakeet-stt.service` (⚠️ nome legacy — il modello reale è **Canary** "
    "`nvidia/canary-1b-v2`, vedi tabella sopra; :9000 da sempre)"
)
INFRA_SVC_NEW = (
    "- `parakeet-stt.service` (dal 2026-09-12 il nome **corrisponde** al modello: "
    "`nvidia/parakeet-tdt-0.6b-v3`, override via `STT_MODEL`; :9000 da sempre)"
)

INFRA_CMD_OLD = "# STT (Canary-1b-v2 — wrapper storico parakeet-stt, porta 9000 da sempre)"
INFRA_CMD_NEW = "# STT (Parakeet-TDT-0.6b-v3 dal 2026-09-12, override via STT_MODEL; porta 9000 da sempre)"

INFRA_ASCII_OLD = "│  • Canary STT    (:9000)                    │"
INFRA_ASCII_NEW = "│  • Parakeet STT  (:9000)                    │"

INFRA_DOCKER_OLD = "STT alternativo (non in produzione; STT attivo: Canary su GX10)"
INFRA_DOCKER_NEW = "STT alternativo (non in produzione; STT attivo: Parakeet-TDT v3 su GX10)"

QUICKREF_STT_OLD_PREFIX = "| 9000 | Canary STT (nvidia/canary-1b-v2) |"
QUICKREF_STT_NEW = (
    "| 9000 | Parakeet STT (`nvidia/parakeet-tdt-0.6b-v3`) | systemd `parakeet-stt.service` — "
    "script `parakeet-gpu-server.py`, porta invariata da sempre. Swap da Canary il 2026-09-12; "
    "modello ora da env `STT_MODEL` (rollback: `nvidia/canary-1b-v2`). **4.14 GiB VRAM** BF16 "
    "(Canary ne usava 8.42) |"
)

QUICKREF_ENGINE_OLD = "| `STT_ENGINE` | `parakeet` (valore storico → server Canary :9000) |"
QUICKREF_ENGINE_NEW = "| `STT_ENGINE` | `parakeet` (dal 2026-09-12 coerente col modello reale, :9000) |"

# ──────────────────────────────────── 3. ComfyUI: mutua esclusivita'

COMFY_SECTION = """
## Mutua esclusività con il LLM heavy (`gb10-mode`)

**Dal 2026-09-12 ComfyUI e il [[local-llm-heavy|LLM heavy]] non possono girare insieme.**
La pool di memoria del GB10 è unificata (~121 GiB condivisi CPU/GPU) e Qwen3.8-Flash-Next da
solo ne occupa ~82 GiB: sommarci ComfyUI + WAN 2.2 la satura, e saturarla **blocca il kernel
senza OOM né log** (già avvenuto tre volte il 2026-09-04). Le due pipeline sono quindi
modalità alternative, commutabili dalla dashboard.

### Le due modalità

| Modalità | vllm (`:30000`) | `comfyui` | `ace-step` | TTS / STT / dashboard |
|---|---|---|---|---|
| `llm` | **UP** | down | down | up |
| `creator` | down | **UP** | **UP** | up |

TTS (`cosyvoice3-tts`), STT (`parakeet-stt`) e questa dashboard restano su in **entrambe**:
sono leggeri e servono comunque. Vedi [[jarvis-infrastructure]] per il loro footprint.

### `gb10-mode`

`/usr/local/bin/gb10-mode {status|llm|creator}` (root:root 0755; sorgente versionato in
`jarvis/tools/gb10-mode.sh`). Gira come `jarvis`, usa `sudo systemctl` per comfyui/ace-step e
`flash-next/start.sh|stop.sh` per vllm.

```bash
gb10-mode status          # sola lettura, sempre sicuro; JSON con modalità + salute servizi
gb10-mode creator         # spegne vllm, poi accende ComfyUI + AceStep
gb10-mode llm             # spegne ComfyUI + AceStep, poi riaccende vllm (~10 min di load)
```

Le safety, dato che `REQUIRE_IDLE_GPU=false` e start.sh non verifica da sé:

- **mai sovrapposizione**: spegne del tutto la modalità uscente → attende che `MemAvailable`
  risalga sopra soglia (`MEM_FOR_VLLM=80`, `MEM_FOR_CREATOR=60` GiB) → solo allora accende;
- in caso di dubbio **aborta invece di forzare** — il blocco del kernel non è recuperabile;
- lock in `/var/lib/gb10-mode/lock` contro switch concorrenti;
- stato in `/var/lib/gb10-mode/state.json`, log in `flash-next/logs/gb10-mode.log`;
- `status` distingue anche lo stato `mixed` (pezzi di entrambe le modalità attivi).

### Toggle dalla dashboard

`https://images.mintwork.it` espone il badge di modalità e il toggle:
`GET /api/mode` (modalità + salute di ogni servizio) e `POST /api/mode/switch`, che lancia lo
switch **detached** perché dura minuti. I pulsanti `.btn-generate` sono disabilitati quando
ComfyUI non risponde, e gli endpoint che dipendono da ComfyUI restituiscono **503
`creator_offline`** con un messaggio leggibile invece di un 500 col traceback.

> **Fix abilitante:** `comfyui-dashboard.service` aveva `Requires=comfyui.service` e veniva
> trascinato giù insieme a ComfyUI — rendendo impossibile riaccendere il creator dal browser.
> Rimosso (backup `.bak-*` in `/etc/systemd/system/`); `After=` resta, perché ordina senza
> creare dipendenza forte. Nota: un drop-in con `Requires=` vuoto **non** resetta la lista su
> questa versione di systemd, va editata l'unit.

### Prompt refinement: Sonnet 5 via billing proxy

Il refinement dei prompt della dashboard usava Gemini (se `GEMINI_API_KEY`) e come fallback
llama.cpp locale sul modello `dark-opus` — che non esiste più, quindi era **già rotto**, e in
modalità creator sarebbe stato irraggiungibile comunque (vllm spento).

Dal 2026-09-12 usa **Claude Sonnet 5** (`claude-sonnet-5`) attraverso
[[hermes-billing-proxy]] su `100.116.99.9:18801`, con l'SDK ufficiale `anthropic` nel venv
della dashboard. Funziona in entrambe le modalità, perché il proxy sta fuori dal GB10.
Il proxy fa **token swap** (inietta l'OAuth di Claude Code), quindi la `api_key` nel codice è
un placeholder dichiarato, non un segreto; inietta anche i tool stub di Claude Code, quindi
ogni chiamata costa ~2.7k token di input anche su un prompt di dieci parole.
Su qualunque errore `refine_prompt` restituisce il prompt originale: il refinement è
opzionale e non deve mai impedire una generazione.

---
"""

# ────────────────────────────────────────────────────────────── helper

def bump_updated(text: str, when: str = TODAY) -> str:
    return re.sub(r"^updated: .*$", f"updated: {when}", text, count=1, flags=re.M)


def patch(path: Path, fn) -> bool:
    """Applica fn al testo; scrive solo se cambia. Ritorna True se scritto."""
    src = path.read_text()
    out = fn(src)
    if out is None or out == src:
        return False
    path.write_text(out)
    return True


def require(cond: bool, msg: str) -> None:
    if not cond:
        print(f"ERRORE: {msg}", file=sys.stderr)
        sys.exit(1)


# ────────────────────────────────────────────────────────────── patchers

def do_llm(src: str):
    if "Qwen3.8-Flash-Next" in src:
        print("  local-llm-heavy: già aggiornata")
        return None

    out = src
    # frontmatter: data + tag
    out = bump_updated(out)
    out = out.replace(
        "tags: [jarvis, llm, local, gpu, llama-cpp, gx10, qwen, qwopus, nvfp4, mtp]",
        "tags: [jarvis, llm, local, gpu, gx10, qwen, nvfp4, mtp, vllm, flash-next, moe, inference, model]",
        1,
    )
    # intro: l'alias dark-opus non esiste piu'
    out = out.replace(
        "Modello esposto come **`dark-jarvis`** / `dark-opus` su `http://gx10-3b82.local:30000`",
        "Modello esposto come **`dark-jarvis`** su `http://gx10-3b82.local:30000`",
        1,
    )
    out = out.replace(
        "> NON da confondere con il [[llama-router-llm|Router LLM]] (Qwen 2.5 7B Q6_K su atomman, *lightweight* per intent classification).",
        "> NON da confondere con il [[llama-router-llm|Router LLM]] (Qwen 2.5 7B Q6_K su atomman,\n"
        "> *lightweight* per intent classification). Consumer aggiuntivo dal 2026-09: anche\n"
        "> [[crypto-redteam]] usa `dark-jarvis` per i task refusal-heavy.",
        1,
    )

    # sezione 1 -> nuova (dalla vecchia intestazione fino a "## 2. Cosa abbiamo provato")
    m = re.search(r"## 1\. Stack in uso \(2026-05-25\).*?(?=## 2\. Cosa abbiamo provato)", out, re.DOTALL)
    require(bool(m), "sezione 1 di local-llm-heavy non trovata")
    old_s1 = m.group(0)
    # la vecchia sezione diventa storia dentro la 2.3
    out = out[:m.start()] + LLM_NEW_SECTION1 + out[m.end():]

    require(LLM_23_OLD in out, "intestazione 2.3 non trovata")
    out = out.replace(LLM_23_OLD, LLM_23_NEW, 1)

    # dettagli dello stack llama.cpp preservati sotto la 2.3
    anchor_23_end = "**Modello migrato:** `Qwopus3.6-27B-v2`"
    require(anchor_23_end in out, "chiusura 2.3 non trovata")
    hist = (
        "**Stack completo di allora** (per riferimento, era la §1 di questa pagina fino al 2026-09-11):\n\n"
        "<details>\n<summary>llama.cpp + Qwopus3.6-v2 — parametri e performance</summary>\n\n"
        + old_s1.replace("## 1. Stack in uso (2026-05-25)", "#### Stack llama.cpp (2026-05-25)").rstrip()
        + "\n\n</details>\n\n"
    )
    out = out.replace(anchor_23_end, hist + anchor_23_end, 1)

    # nuova 2.4 prima della sezione 3
    require("## 3. Pipeline di build modello" in out, "sezione 3 non trovata")
    out = out.replace("## 3. Pipeline di build modello",
                      LLM_24.lstrip("\n") + "\n## 3. Pipeline di build modello", 1)
    # la pipeline di build riguarda Qwopus: marcala come storica
    out = out.replace(
        "## 3. Pipeline di build modello",
        "## 3. Pipeline di build modello (storica — Qwopus3.6/GGUF)\n\n"
        "> Vale per lo stack llama.cpp di §2.3. Flash Next non si costruisce in casa: si scarica\n"
        "> il checkpoint gated (`ABLIT=1 ./download.sh` in `flash-next/`).",
        1,
    )

    # consumer: aggiorna endpoint e alias
    out = out.replace(
        '- `/v1/models` → ritorna `{"id":"dark-jarvis"}` (aliases registrato `dark-opus` ma `/v1/models` espone solo `dark-jarvis`)',
        '- `/v1/models` → ritorna `{"id":"dark-jarvis"}`\n'
        "- `/metrics` → contatori Prometheus, inclusa l'acceptance MTP (`vllm:spec_decode_*`)",
        1,
    )
    out = out.replace("- `/props` (slot state + config)", "", 1)

    # roadmap
    out = out.replace(
        "- ✅ Migrazione da v5 fork → upstream stock con MTP nativo (2026-05-25)",
        "- ✅ Migrazione da v5 fork → upstream stock con MTP nativo (2026-05-25)\n"
        "- ✅ Migrazione llama.cpp/Qwopus3.6 → **vLLM/Qwen3.8-Flash-Next** (2026-09-11/12)\n"
        "- ✅ `reasoning_effort` default `xhigh` → `low` lato server (2026-09-12)\n"
        "- ✅ Mutua esclusività con la pipeline video via `gb10-mode` (2026-09-12)\n"
        "- 🔲 Alzare `MAX_NUM_BATCHED_TOKENS` a 8192 (~11% di prefill, richiede restart ~10 min)\n"
        "- 🔲 Provare `COMPILATION_MODE=3` (torch.compile) — rischioso su sm_121, serve finestra di rollback",
        1,
    )
    out = out.replace(
        "- [[llama-router-llm]] — Router lightweight, NON è questo",
        "- [[llama-router-llm]] — Router lightweight, NON è questo\n"
        "- [[crypto-redteam]] — Consumer per i task refusal-heavy\n"
        "- [[hermes-billing-proxy]] — Percorso alternativo (Anthropic) quando serve un modello cloud",
        1,
    )
    return out


def do_infra(src: str):
    if "Parakeet STT    | 9000" in src:
        print("  jarvis-infrastructure: già aggiornata")
        return None
    out = bump_updated(src)

    # riga di tabella (lunga): sostituisci dall'inizio riga al newline
    i = out.find(INFRA_STT_OLD_PREFIX)
    require(i != -1, "riga tabella Canary STT non trovata")
    j = out.find("\n", i)
    out = out[:i] + INFRA_STT_NEW + out[j:]

    require(INFRA_SVC_OLD in out, "elenco servizi (parakeet legacy) non trovato")
    out = out.replace(INFRA_SVC_OLD, INFRA_SVC_NEW, 1)
    require(INFRA_CMD_OLD in out, "commento comando STT non trovato")
    out = out.replace(INFRA_CMD_OLD, INFRA_CMD_NEW, 1)
    out = out.replace(INFRA_ASCII_OLD, INFRA_ASCII_NEW)
    out = out.replace(INFRA_DOCKER_OLD, INFRA_DOCKER_NEW)
    return out


def do_quickref(src: str):
    if "Parakeet STT (`nvidia/parakeet-tdt-0.6b-v3`)" in src:
        print("  quickref: già aggiornata")
        return None
    out = bump_updated(src)
    i = out.find(QUICKREF_STT_OLD_PREFIX)
    require(i != -1, "riga quickref Canary non trovata")
    j = out.find("\n", i)
    out = out[:i] + QUICKREF_STT_NEW + out[j:]
    require(QUICKREF_ENGINE_OLD in out, "riga STT_ENGINE non trovata")
    out = out.replace(QUICKREF_ENGINE_OLD, QUICKREF_ENGINE_NEW, 1)
    return out


def do_comfy(src: str):
    if "gb10-mode" in src:
        print("  comfyui-gx10-setup: già aggiornata")
        return None
    out = bump_updated(src)
    out = out.replace(
        "tags: [jarvis, hardware, deployment, docker, inference, automation]",
        "tags: [jarvis, hardware, deployment, docker, inference, automation, gx10, gpu, pipeline]",
        1,
    )
    anchor = "## Maintenance & Operations"
    require(anchor in out, "sezione Maintenance non trovata in comfyui-gx10-setup")
    out = out.replace(anchor, COMFY_SECTION.lstrip("\n") + "\n" + anchor, 1)
    # lo stato "24/7" non è più vero
    out = out.replace(
        "- **Status:** Running 24/7 as part of [[jarvis-infrastructure]]",
        "- **Status:** attivo **solo in modalità `creator`** — mutuamente esclusivo con il\n"
        "  [[local-llm-heavy|LLM heavy]], vedi § Mutua esclusività ([[jarvis-infrastructure]])",
        1,
    )
    return out


def do_index(src: str):
    if "[[local-llm-heavy|Local LLM Heavy]] (STUB WIP)" not in src:
        print("  index: già aggiornato")
        return None
    return src.replace(
        "### 6. [[local-llm-heavy|Local LLM Heavy]] (STUB WIP)",
        "### 6. [[local-llm-heavy|Local LLM Heavy]]",
        1,
    )


def do_log(src: str):
    if "2026-09-12 — switch LLM heavy" in src:
        print("  log: già aggiornato")
        return None
    entry = f"""
## {TODAY} — switch LLM heavy, swap STT, mutua esclusività GPU

- **[[local-llm-heavy]]** riscritta §1: lo stack passa da llama.cpp + `Qwopus3.6-v2-Abl-MTP-NVFP4`
  a **vLLM + Qwen3.8-Flash-Next** (checkpoint ablit gated `drowzeys/keys-...-Mia-Single-Spark-only`,
  MoE 512/10 attivi, NVFP4 + MXFP8 `o_proj`, MTP=3, context 131k, input multimodale con video).
  Documentati il repo dei parametri `/home/jarvis/flash-next/` (`.env` + `start.sh`/`stop.sh`),
  il footprint della pool unificata e le performance misurate. Il vecchio stack è conservato
  in §2.3; nuova §2.4 sul perché del cambio.
- **[[local-llm-heavy]]** §1.2: `reasoning_effort` del chat template portato da `xhigh` a `low`
  lato server — il default del template costava ~17s di reasoning prima del primo token utile.
- **[[jarvis-infrastructure]]** + **[[jarvis-infrastructure-quickref]]**: STT swappato da
  **Canary `nvidia/canary-1b-v2`** a **Parakeet `nvidia/parakeet-tdt-0.6b-v3`** (2026-09-12).
  Il modello non è più hardcoded ma da env `STT_MODEL`; VRAM 8.42 → 4.14 GiB; il naming
  `parakeet-*` smette di essere legacy e torna a corrispondere al modello. Porta :9000 invariata.
- **[[comfyui-gx10-setup]]**: nuova sezione sulla **mutua esclusività** ComfyUI ↔ LLM heavy via
  `gb10-mode {{status|llm|creator}}`, con le safety di memoria, il toggle dalla dashboard
  (`/api/mode`, `/api/mode/switch`) e il disaccoppiamento di `comfyui-dashboard.service` da
  `comfyui.service`. Aggiunto il passaggio del prompt refinement a **Claude Sonnet 5** via
  [[hermes-billing-proxy]].
- **[[index]]**: `local-llm-heavy` non è più marcata STUB WIP.

Verifiche: modello STT confermato da codice + journal + nvidia-smi; performance LLM da
`bench_v5_vllm.py` (15 run/livello); porte confermate con `ss -tlnp` (Parakeet :9000,
ACE-Step :7865 — le due erano invertite in una nota precedente).
"""
    return src.rstrip() + "\n" + entry


# ────────────────────────────────────────────────────────────────── main

def main() -> int:
    targets = [
        ("concepts/local-llm-heavy.md", do_llm),
        ("concepts/jarvis-infrastructure.md", do_infra),
        ("concepts/jarvis-infrastructure-quickref.md", do_quickref),
        ("concepts/comfyui-gx10-setup.md", do_comfy),
        ("index.md", do_index),
        ("log.md", do_log),
    ]
    changed = []
    for rel, fn in targets:
        p = WIKI / rel
        if not p.exists():
            print(f"ERRORE: {rel} non esiste", file=sys.stderr)
            return 1
        if patch(p, fn):
            print(f"  aggiornato: {rel}")
            changed.append(rel)
    print(f"\n{len(changed)} file modificati")
    return 0


if __name__ == "__main__":
    sys.exit(main())
