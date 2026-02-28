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
