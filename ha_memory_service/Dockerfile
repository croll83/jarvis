FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn aiohttp pydantic chromadb requests

COPY main.py .

ENV LOCATION_ID=unknown
ENV HA_URL=http://supervisor/core
ENV HA_TOKEN=""
ENV QWEN_URL=http://localhost:11434
ENV DB_PATH=/data/ha_memory.db
ENV CHROMA_PATH=/data/chroma

VOLUME /data

EXPOSE 8100

CMD ["python", "main.py"]
