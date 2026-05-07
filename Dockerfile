FROM python:3.11-slim

WORKDIR /app

# LITE_MODE=1 — пропускаем дорогие шаги (build_index.py + HF model preload).
# Контейнер стартует с SQLite-only поиском кандидатов. Подходит для слабых
# виртуалок без KVM, где build_index.py может крутиться час+.
ARG LITE_MODE=0
ENV LITE_MODE=${LITE_MODE}

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

# Шаг 1: SQLite со справочником ТН ВЭД — нужен ВСЕГДА (и в lite, и в full).
# Быстрый: ~30 секунд. Если data/tnved.db уже едет в build context — пропустим.
RUN if [ ! -f data/tnved.db ]; then \
        echo "[build] data/tnved.db отсутствует — собираю SQLite" \
        && python fetch_tnved.py \
        && python parse_tnved.py ; \
    else \
        echo "[build] data/tnved.db найден в build context, пропускаю fetch+parse" ; \
    fi

# Шаг 2: FAISS-индекс + HF preload — только в полном режиме.
# В lite-режиме поиск кандидатов идёт через SQLite, эмбеддер не нужен.
RUN if [ "$LITE_MODE" = "1" ]; then \
        echo "[build] LITE_MODE=1 — пропускаю build_index.py и HF preload" ; \
    else \
        if [ ! -f data/tnved.faiss ] || [ ! -f data/tnved_meta.json ]; then \
            echo "[build] FAISS отсутствует — собираю (медленно на слабом CPU)" \
            && python build_index.py ; \
        else \
            echo "[build] FAISS уже в build context, пропускаю build_index.py" ; \
        fi \
        && echo "[build] прогреваю HF-кэш модели intfloat/multilingual-e5-base" \
        && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-base')" ; \
    fi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
