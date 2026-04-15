FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    mem0ai \
    kuzu \
    rank-bm25 \
    chromadb-client \
    fastapi \
    uvicorn[standard]

COPY server.py .

EXPOSE 8200

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8200", "--log-level", "info"]
