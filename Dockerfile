FROM python:3.11-slim

WORKDIR /app

# LITE_MODE=1 — без FAISS: кандидаты из SQLite. Только проверить, что стек
# поднимается; для прогонов не годится (см. .env.example).
ARG LITE_MODE=0
ENV LITE_MODE=${LITE_MODE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

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

# Шаг 2 (полный режим): FAISS-индекс — готовыми файлами из build context.
# Внутри сборки его не строим: векторы считает bge-m3 на нашем сервере
# (python build_index.py с EMBEDDINGS_BASE_URL), а у docker build этих
# переменных нет. Локальную e5 в образ не кладём — ни модели, ни torch:
# запросы векторизует тот же сервер, адрес — EMBEDDINGS_BASE_URL в .env.
# Индекса нет — сборка падает с причиной, а не собирает его часами на CPU.
RUN if [ "$LITE_MODE" = "1" ]; then \
        echo "[build] LITE_MODE=1 — индекс не нужен" ; \
    elif [ -f data/tnved.faiss ] && [ -f data/tnved_meta.json ] && [ -f data/tnved_index_info.json ]; then \
        echo "[build] индекс из build context: $(cat data/tnved_index_info.json | tr -d '\n ')" ; \
    else \
        echo "[build] ОШИБКА: нет data/tnved.faiss, tnved_meta.json или tnved_index_info.json." \
             "Соберите индекс заранее (python build_index.py с EMBEDDINGS_BASE_URL)" \
             "и положите файлы в data/ — или LITE_MODE=1 для проверки стека." >&2 ; \
        exit 1 ; \
    fi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
