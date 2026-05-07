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

# Данные и FAISS-индекс ожидаются ГОТОВЫЕ в data/ (через build context).
# Если их нет — пытаемся собрать (fallback на старое поведение). На слабых
# виртуалках без KVM build_index.py может занимать час+, поэтому штатно
# собираем локально и кладём в репозиторий рабочей копии перед docker build.
RUN if [ ! -f data/tnved.faiss ] || [ ! -f data/tnved_meta.json ] || [ ! -f data/tnved.db ]; then \
        echo "data/ пуста — собираю с нуля (это медленно на слабом CPU)" \
        && python fetch_tnved.py \
        && python parse_tnved.py \
        && python build_index.py ; \
    else \
        echo "data/ уже собрана, пропускаю fetch+parse+build_index" ; \
    fi

# В рантайме classifier ходит в HF за моделью эмбеддера. Чтобы избежать
# повторной выкачки на каждом старте контейнера — прогреваем HF-кэш в образе.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-base')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
