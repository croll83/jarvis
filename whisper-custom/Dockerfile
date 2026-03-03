# =============================================================================
# JARVIS Whisper — speaches (CUDA 12.9) + CTranslate2 4.7.x (Blackwell INT8)
# =============================================================================
# Base: speaches-ai/speaches con CUDA 12.9.1 (supporta sm_120 / Blackwell)
# Fix:  CTranslate2 >= 4.7.0 re-abilita int8/int8_float16 su GPU Blackwell
#       (RTX 5060 Ti / 5070 / 5080 / 5090) — PR #1982
#       https://github.com/OpenNMT/CTranslate2/pull/1982
#
# NOTA: Il container base (fedirz/faster-whisper-server) usa CUDA 12.2 che
#       NON supporta INT8 cuBLAS su sm_120. Speaches usa CUDA 12.9.1 che SI.
#
# NOTA: Speaches carica i modelli dalla HF cache. Il modello locale viene
#       montato via volume e linkato nella struttura cache HuggingFace.
#       Nella richiesta API usare: model=deepdml/faster-whisper-large-v3-turbo-ct2
# =============================================================================

FROM ghcr.io/speaches-ai/speaches:latest-cuda

# Upgrade CTranslate2 da 4.5.0 a 4.7.x — abilita INT8 su Blackwell (sm_120)
RUN uv pip install --python /home/ubuntu/speaches/.venv/bin/python3 --no-cache 'ctranslate2>=4.7.0,<5'

# Crea la struttura HF cache per il modello montato via volume in /models/faster-whisper-large-v3-turbo
# HF cache richiede: refs/main -> hash, snapshots/<hash>/ -> model files
# Usiamo un hash finto ma di formato valido (40 hex chars)
ENV HF_CACHE_DIR=/home/ubuntu/.cache/huggingface/hub
ENV MODEL_CACHE_DIR=${HF_CACHE_DIR}/models--deepdml--faster-whisper-large-v3-turbo-ct2
ENV FAKE_HASH=0000000000000000000000000000000000000001

RUN mkdir -p ${MODEL_CACHE_DIR}/snapshots \
    && mkdir -p ${MODEL_CACHE_DIR}/refs \
    && ln -s /models/faster-whisper-large-v3-turbo ${MODEL_CACHE_DIR}/snapshots/${FAKE_HASH} \
    && printf '%s' "${FAKE_HASH}" > ${MODEL_CACHE_DIR}/refs/main
