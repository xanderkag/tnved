FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/app/.hf-cache \
    TRANSFORMERS_OFFLINE=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Сборка данных и FAISS-индекса в момент билда:
#   1) fetch_tnved.py — качает CSV из GitHub infoculture/opencustoms
#   2) parse_tnved.py — конвертирует в SQLite
#   3) build_index.py — считает эмбеддинги и пишет FAISS + meta.json
# Параллельно тянется HF-модель intfloat/multilingual-e5-base в HF_HOME,
# она же используется при рантайме (поиск кандидатов).
RUN python fetch_tnved.py \
    && python parse_tnved.py \
    && python build_index.py

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/models || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
