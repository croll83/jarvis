# Dark Jarvis — Heavy LLM su GB10 (DGX Spark)

> **Servizio:** `dark-jarvis.service` (systemd, auto-start on boot).
> **Modello:** Qwopus3.6-27B-v2 Abliterated + MTP NVFP4.
> **Engine:** upstream `ggml-org/llama.cpp` stock (build snapshot 2026-05-25).
> **Endpoint:** `http://100.98.187.12:30000/v1` (Tailscale), model `dark-jarvis` / `dark-opus`.

Documenta tutto il necessario per ricreare lo stack heavy LLM da zero su un GB10 fresco e per operarlo in produzione.

---

## 1. Quick reference

```bash
# Status / logs / restart
sudo systemctl status dark-jarvis
tail -F /var/log/dark-jarvis.log
sudo systemctl restart dark-jarvis

# Cambia parametri runtime (modifica cmdline, poi restart)
vim /home/jarvis/dark-jarvis-server/cmdline.txt
sudo systemctl restart dark-jarvis

# Test endpoint
curl -s http://localhost:30000/health
curl -s http://localhost:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"dark-jarvis","messages":[{"role":"user","content":"ciao"}],"max_tokens":20,"temperature":0.8}'
```

---

## 2. Prerequisiti hardware/software

| Componente | Valore |
|---|---|
| GPU | NVIDIA GB10 (Blackwell, SM 12.1, datacenter) |
| Memoria | 128 GiB unified (LPDDR5X, 273 GB/s peak bandwidth) |
| CPU | ARM aarch64 (sbsa) |
| OS | Ubuntu 24.04 LTS (arm64) |
| CUDA | 12.8+ (driver 580.x) — 13.x supportato |
| Disk libero | ≥ 80 GB (per build pipeline modelli) |
| RAM disco modello | ~50 GB peak (durante quantize NVFP4) |

---

## 3. Build engine

```bash
# Clone upstream stock (NO fork)
cd ~
git clone https://github.com/ggml-org/llama.cpp llama-cpp-stock
cd llama-cpp-stock

# Configure (Blackwell SM 12.0/12.1 — abilita FP4 MMA native, NVFP4 + MTP)
export PATH=/usr/local/cuda/bin:$PATH
cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="120;121" \
  -DGGML_CUDA_FA_ALL_QUANTS=ON \
  -DCMAKE_BUILD_TYPE=Release

# Build (~10 min cold compile, parallel)
cmake --build build --target llama-server llama-quantize llama-cli -j$(nproc)
```

**Verifica commit minimo richiesto:**
- PR [#22673](https://github.com/ggml-org/llama.cpp/pull/22673) "llama + spec: MTP Support" — MUST be merged (aprile 2026)
- PR [#23461](https://github.com/ggml-org/llama.cpp/pull/23461) "server: free draft/MTP resources on sleep" — fix VRAM leak (maggio 2026)
- PR [#23563](https://github.com/ggml-org/llama.cpp/pull/23563) "Add NVFP4 MTP scale tensors" — merged 2026-05-23
- Build attuale verificata: HEAD `c0c7e147e` 2026-05-25

```bash
~/llama-cpp-stock/build/bin/llama-server --version
# Atteso: version 9290+ (build c0c7e147e o successivo)
```

---

## 4. Pipeline modello — Qwopus3.6-v2 → Abliterated → NVFP4 + MTP GGUF

Riproduce `~/qwopus36-v2-mtp-abl-nvfp4/Qwopus3.6-v2-Abl-MTP-NVFP4.gguf` (19 GB) da zero. **Stop `dark-jarvis.service` durante gli step GPU-heavy (3, 4)** per liberare i 30 GiB di pool unified-mem.

```bash
# 0. Free disk + GPU
sudo systemctl stop dark-jarvis
df -h /home/jarvis  # serve ≥ 80 GB libero per peak

# 1. Download BF16 source
mkdir -p ~/qwopus36-v2-bf16
hf download Jackrong/Qwopus3.6-27B-v2 \
  --local-dir ~/qwopus36-v2-bf16 \
  --include "*.safetensors" "*.json" "*.jinja" "*.txt"
# Total ~52 GB, 15 shards

# 2. Abliterate (skip MTP head, project refusal_dir out of text layers 2..61)
#    Script: see ~/sviluppo/openclaw/notebooks/abliterate_qwopus.py (riferimento storico)
#    Versione MTP-safe: /tmp/abliterate_v2_mtp.py
python3 ~/sviluppo/jarvis/infrastructure/gb10/scripts/abliterate_qwopus_v2_mtp.py \
  ~/qwopus36-v2-bf16 \
  ~/qwopus36-v2-mtp-abl
# Output: 11 shard safetensors ~54 GB, MTP head INTATTO

# 3. ModelOpt NVFP4 quantize (text body)
#    HF Qwen3_5ForConditionalGeneration NON istanzia MTP head → ModelOpt quantizza solo text
python3 ~/llama-cpp-v5/tools/dflash-cli/quantize_nvfp4_plain.py \
  ~/qwopus36-v2-mtp-abl \
  ~/qwopus36-v2-mtp-abl-nvfp4
# Output: model.safetensors NVFP4 ~18 GB + mmproj_vision_model.safetensors 879 MB

# 4. Re-inject MTP weights (BF16) dall'originale Jackrong nell'NVFP4 dir
python3 -c "
from safetensors import safe_open
from safetensors.torch import save_file
import os, json
SRC = '/home/jarvis/qwopus36-v2-bf16'
DST = '/home/jarvis/qwopus36-v2-mtp-abl-nvfp4'
mtp = {}
for f in sorted(os.listdir(SRC)):
    if f.endswith('.safetensors') and f.startswith('model'):
        with safe_open(os.path.join(SRC, f), framework='pt') as h:
            for k in h.keys():
                if k.startswith('mtp.'):
                    mtp[k] = h.get_tensor(k)
save_file(mtp, os.path.join(DST, 'model-mtp.safetensors'))
# Update index
wm = {}
with safe_open(os.path.join(DST, 'model.safetensors'), framework='pt') as h:
    for k in h.keys(): wm[k] = 'model.safetensors'
for k in mtp: wm[k] = 'model-mtp.safetensors'
json.dump({'metadata':{'total_size':0},'weight_map':wm},
          open(os.path.join(DST, 'model.safetensors.index.json'),'w'), indent=2)
print(f'mtp injected: {len(mtp)} tensors')
"

# 5. Convert to GGUF with upstream stock script (PATCH: skip rope buffers)
cd ~/llama-cpp-stock
sed -i 's|".rotary_emb.inv_freq")|".rotary_emb.inv_freq", ".rotary_emb.original_inv_freq", ".rotary_pos_emb.inv_freq")|' \
  conversion/base.py
python3 convert_hf_to_gguf.py ~/qwopus36-v2-mtp-abl-nvfp4 \
  --outfile ~/qwopus36-v2-mtp-abl-nvfp4/Qwopus3.6-v2-Abl-MTP-NVFP4.gguf
# Output: 19 GB GGUF — blk.0..63 NVFP4 + blk.64 BF16 MTP (nextn.eh_proj, enorm, hnorm, shared_head_norm)

# 6. mmproj (vision unchanged dall'abliterazione)
python3 convert_hf_to_gguf.py ~/qwopus36-v2-bf16 \
  --mmproj --outtype f16 \
  --outfile ~/qwopus36-v2-mtp-abl-nvfp4/mmproj-Qwopus3.6-v2-Abl-MTP-F16.gguf

# 7. Verifica
python3 -c "
import gguf
r = gguf.GGUFReader('/home/jarvis/qwopus36-v2-mtp-abl-nvfp4/Qwopus3.6-v2-Abl-MTP-NVFP4.gguf')
blks = sorted({int(t.name.split('.')[1]) for t in r.tensors if t.name.startswith('blk.')})
print('blk count:', len(blks), 'min:', min(blks), 'max:', max(blks))
print('blk.64 tensors:', sum(1 for t in r.tensors if t.name.startswith('blk.64')))
# Atteso: blk count 65 min 0 max 64, blk.64 tensors 15
"

# 8. Riavvia il servizio
sudo systemctl start dark-jarvis
```

**Disco picco:** ~80 GB durante step 1-5 (BF16 source 52 + abl 54 + NVFP4 sf 18 + GGUF 19 = picco a step 5).

---

## 5. systemd service

**File:** `/etc/systemd/system/dark-jarvis.service`

```ini
[Unit]
Description=Dark Jarvis LLM (llama.cpp stock + NVFP4 + MTP draft)
Documentation=https://github.com/croll83/jarvis/tree/main/infrastructure/gb10
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/home/jarvis/dark-jarvis-server/run.sh
WorkingDirectory=/home/jarvis/dark-jarvis-server
User=jarvis
Group=jarvis
Restart=always
RestartSec=10
KillMode=mixed
TimeoutStopSec=30
Environment=HOME=/home/jarvis
Environment=PATH=/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LimitNOFILE=65536
LimitMEMLOCK=infinity
StandardOutput=append:/var/log/dark-jarvis.log
StandardError=append:/var/log/dark-jarvis.log

[Install]
WantedBy=default.target
```

**Wrapper:** `/home/jarvis/dark-jarvis-server/run.sh`

```bash
#!/bin/bash
# dark-jarvis llama-server runtime wrapper
set -e
cd "$(dirname "$0")"
exec $(cat cmdline.txt)
```

**Cmdline runtime:** `/home/jarvis/dark-jarvis-server/cmdline.txt`

```
/home/jarvis/llama-cpp-stock/build/bin/llama-server \
  -m /home/jarvis/qwopus36-v2-mtp-abl-nvfp4/Qwopus3.6-v2-Abl-MTP-NVFP4.gguf \
  --mmproj /home/jarvis/qwopus36-v2-mtp-abl-nvfp4/mmproj-Qwopus3.6-v2-Abl-MTP-F16.gguf \
  --spec-type draft-mtp --spec-draft-n-max 5 \
  --host 0.0.0.0 --port 30000 -c 262144 -np 2 -ngl 99 \
  -ctk q8_0 -ctv q8_0 \
  --slot-prompt-similarity 0.5 --cache-reuse 256 --cache-ram 16384 -bs \
  --jinja --chat-template-file /home/jarvis/qwopus36-v2-bf16/chat_template.jinja \
  --no-prefill-assistant --reasoning off \
  --alias dark-jarvis --alias dark-opus --no-webui --no-warmup
```

**Setup iniziale (una tantum):**

```bash
mkdir -p /home/jarvis/dark-jarvis-server
# Crea run.sh e cmdline.txt (vedi sopra)
chmod +x /home/jarvis/dark-jarvis-server/run.sh

sudo cp dark-jarvis.service /etc/systemd/system/
sudo touch /var/log/dark-jarvis.log
sudo chown jarvis:jarvis /var/log/dark-jarvis.log
sudo systemctl daemon-reload
sudo systemctl enable --now dark-jarvis
```

**Verifica health:**
```bash
for i in {1..20}; do
  if curl -s http://localhost:30000/health 2>/dev/null | grep -q ok; then
    echo "HEALTHY after ${i}*5s"; break
  fi
  sleep 5
done
```

---

## 6. Tuning parametri

I parametri seguenti sono nel `cmdline.txt`. Modifica + `sudo systemctl restart dark-jarvis`.

| Parametro | Valore attuale | Range | Effetto |
|---|---|---|---|
| `-c` | 262144 | 32k-262k | Context size totale (split tra slot via `-np`) |
| `-np` | 2 | 1-4 | Slot paralleli (Hermes usa 2) |
| `-ctk/-ctv` | q8_0/q8_0 | f16, q8_0, q5_0, q4_0, iq4_nl | KV cache quant (q8_0 = sweet spot quality/VRAM) |
| `--spec-draft-n-max` | 5 | 2-8 | Token draft per spec step. **5 ottimo per workload misto**; n=3 default upstream è troppo conservativo |
| `--cache-ram` | 16384 (16 GiB) | 0-32768 | Prompt prefix cache. Più alto = più hit warm per Hermes che ripete system prompt |
| `--cache-reuse` | 256 | 0-1024 | Min token per riusare un prefisso cached |
| `--slot-prompt-similarity` | 0.5 | 0.0-1.0 | Threshold per scegliere lo slot con prefisso più simile |
| `-bs` | enabled | flag | Backend sampling (esperimentale, +5-10% throughput) |
| `--reasoning` | off | off, auto | `off` salta il blocco `<think>` (decode più diretto, +12% media) |

**Acceptance rate atteso MTP:**
- Output strutturato (code, JSON, list, skill writing): 65-90%
- Reasoning naturale / prose: 50-70%
- Long context (>30k token) o vision-heavy: 30-50%

---

## 7. Troubleshooting

| Sintomo | Causa | Soluzione |
|---|---|---|
| Service non parte | Modello GGUF mancante o corrotto | Verifica con `llama-gguf` o ri-step 7 della pipeline |
| `Out of memory` | Context troppo grande / altri servizi GPU occupano | Stop comfyui/ace-step temporaneamente o riduci `-c` |
| Decode 5-7 t/s sostenuto | 2 slot decodificano insieme → halving aspettato | Imposta `-np 1` se single-user, oppure accetta il throughput aggregato uguale |
| Acceptance MTP <30% | Workload vision-heavy o context >50k | Normale; n_max=5 non aiuta sotto questa soglia |
| `verify_batch: embed failed` | Sei sul binary lucebox sbagliato | Usa `/home/jarvis/llama-cpp-stock/build/bin/llama-server`, NON `dflash_server` |
| Risposte garbage / loop | NVFP4 model rotto o chat template errato | Verifica `--jinja --chat-template-file ...` e `llama-gguf` su GGUF |

---

## 8. Rollback a stack legacy v5+DFlash

Solo se necessario (es. validazione regressione). Vedi `~/prod_cmdline_aeon_backup.txt` per la cmdline storica.

```bash
sudo systemctl stop dark-jarvis
sudo systemctl disable dark-jarvis
nohup bash -c "$(cat ~/prod_cmdline_aeon_backup.txt)" > /tmp/prod-rollback.log 2>&1 &
```

Riatti­va lo stack nuovo: `sudo systemctl enable --now dark-jarvis`.

---

## 9. Related

- [[local-llm-heavy]] (wiki) — Concetto + storia stack
- [[jarvis-infrastructure-quickref]] (wiki) — Porte e path GB10
- `~/sviluppo/jarvis/infrastructure/gb10/gx10-full-inventory.md` — Inventario GB10
- `~/sviluppo/jarvis/infrastructure/OLLAMA.md` — Router LLM lightweight (NON questo stack)
- Repo legacy: [croll83/llama.cpp-dgx](https://github.com/croll83/llama.cpp-dgx) (DEPRECATED 2026-05-25)
- Upstream: [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)
