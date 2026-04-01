# =============================================================================
# XTTSv2 API Server — Blackwell patch (RTX 5070, sm_120)
# =============================================================================
# L'immagine daswer123 funziona ma ha PyTorch vecchio (fino a sm_90).
# Partiamo da lì e sostituiamo SOLO PyTorch con la versione CUDA 12.8.

FROM daswer123/xtts-api-server:latest

# Sostituisci PyTorch con versione CUDA 12.8 (supporta sm_120 Blackwell)
RUN pip3 install --no-cache-dir --force-reinstall \
    torch==2.7.0 torchaudio==2.7.0 \
    --index-url https://download.pytorch.org/whl/cu128

# PyTorch 2.7 ha cambiato weights_only default da False a True.
# I checkpoint XTTS usano pickle con classi custom → serve weights_only=False.
RUN python3 -c "\
import pathlib; \
p = pathlib.Path('/usr/local/lib/python3.10/dist-packages/TTS/utils/io.py'); \
t = p.read_text(); \
t = t.replace( \
    'torch.load(f, map_location=map_location, **kwargs)', \
    'torch.load(f, map_location=map_location, weights_only=False, **kwargs)' \
); \
p.write_text(t); \
print('Patched TTS/utils/io.py → weights_only=False')"

# =============================================================================
# CUDA memory cleanup middleware
# =============================================================================
# Problema: xtts-api-server ha ZERO cleanup dopo ogni inference.
# PyTorch caching allocator trattiene VRAM per riuso, ma su un server
# con richieste di lunghezza variabile questo causa frammentazione e crescita
# irreversibile (da ~1.9 GB a ~2.7 GB in poche ore). Coqui è unmaintained.
#
# Soluzione: middleware FastAPI che chiama gc.collect() + torch.cuda.empty_cache()
# dopo ogni request TTS. Costo: ~50-100ms post-response, trascurabile dopo
# un'inference TTS da 0.5-3s. La VRAM si stabilizza a model_weights + ~200 MB.
# =============================================================================

# 1. Crea il modulo middleware
RUN cat > /app/cuda_cleanup.py << 'PYEOF'
import gc
import logging
import torch
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("XTTS_CLEANUP")

# Endpoint che generano tensori GPU intermedi e beneficiano del cleanup
_TTS_PATHS = {"/tts_to_audio/", "/tts_to_file", "/tts_stream"}

class CUDACleanupMiddleware(BaseHTTPMiddleware):
    """Rilascia VRAM cached da PyTorch dopo ogni inference TTS."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if request.url.path in _TTS_PATHS and torch.cuda.is_available():
            # gc.collect() rompe reference cycles Python → libera tensori GPU orfani
            # empty_cache() restituisce i blocchi freed al driver CUDA
            gc.collect()
            torch.cuda.empty_cache()

        return response
PYEOF

# 2. Patcha server.py per registrare il middleware + aggiungere endpoint diagnostico
#    NOTA: il base image ha il sorgente in /app/xtts_api_server/ (usato a runtime)
#    E una copia pip in dist-packages. Python risolve /app/ prima → patchiamo quello.
RUN python3 << 'PATCH_EOF'
import pathlib

# Il server gira da /app/xtts_api_server/ (non da dist-packages)
p = pathlib.Path('/app/xtts_api_server/server.py')
t = p.read_text()

# Aggiungi import del middleware in testa al file
import_line = 'import sys; sys.path.insert(0, "/app")\nfrom cuda_cleanup import CUDACleanupMiddleware\n'
t = import_line + t

# Registra il middleware DOPO il CORSMiddleware esistente
t = t.replace(
    'app.add_middleware(\n    CORSMiddleware,',
    'app.add_middleware(CUDACleanupMiddleware)\napp.add_middleware(\n    CORSMiddleware,'
)

# Aggiungi endpoint /gpu_memory per diagnostica
gpu_endpoint = '''
# === GPU Memory diagnostic endpoint (injected by Dockerfile) ===
@app.get("/gpu_memory")
async def gpu_memory():
    import torch
    if torch.cuda.is_available():
        return {
            "allocated_mb": round(torch.cuda.memory_allocated() / 1024**2, 1),
            "reserved_mb": round(torch.cuda.memory_reserved() / 1024**2, 1),
            "max_allocated_mb": round(torch.cuda.max_memory_allocated() / 1024**2, 1),
        }
    return {"error": "CUDA not available"}

'''
# Inserisci prima del primo endpoint
t = t.replace('@app.get("/speakers_list")', gpu_endpoint + '@app.get("/speakers_list")')

p.write_text(t)
print('Patched /app/xtts_api_server/server.py with CUDA cleanup middleware + /gpu_memory endpoint')
PATCH_EOF
