# Jarvis Core Orchestrator
FROM python:3.11-slim

# Metadata
LABEL maintainer="WAGMI Villa"
LABEL description="Jarvis AI Home Assistant Orchestrator"

# Variabili d'ambiente
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Cartella di lavoro
WORKDIR /app

# Installa dipendenze di sistema (per websockets, audio, resemblyzer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements prima (per cache Docker)
COPY requirements.txt .

# Installa PyTorch CPU-only PRIMA delle altre dipendenze
# Evita che resemblyzer/chromadb tirino dentro nvidia-cuda (~15 GB)
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir onnxruntime && \
    pip install --no-cache-dir -r requirements.txt

# Copia il codice
COPY . .

# Crea cartelle necessarie
RUN mkdir -p /app/data /app/config

# Esponi porta API
EXPOSE 5000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

# Comando di avvio
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
