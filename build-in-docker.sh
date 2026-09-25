#!/bin/sh
# Считает FAISS-индекс ТН ВЭД в одноразовом контейнере — там, где нет своего
# python с зависимостями (например, на прод-хосте).
#
# По умолчанию векторы считает bge-m3 на нашем сервере, адрес обязателен:
#   EMBEDDINGS_BASE_URL=http://10.10.28.10:11434/v1 ./build-in-docker.sh
# Локальная e5 на CPU — только явно: ставит torch, качает 1,1 ГБ модели
# с HuggingFace, на слабом CPU считает часами:
#   EMBEDDER_BACKEND=local ./build-in-docker.sh
# Раньше e5 включалась сама, если адрес не задан, — теперь это отказ.
#
# Кэши pip и HuggingFace живут в именованных volume — повторный запуск
# не качает заново. Векторы кэшируются в data/tnved_vecs.npy,
# промежуточные куски — в data/vecs_parts/, так что прогон возобновляемый.
set -e
cd "$(dirname "$0")"

case "${EMBEDDER_BACKEND:-api}" in
  api)
    if [ -z "$EMBEDDINGS_BASE_URL" ]; then
      echo "задайте EMBEDDINGS_BASE_URL — адрес сервера векторов (e5 на CPU — только EMBEDDER_BACKEND=local)" >&2
      exit 1
    fi
    INSTALL="pip install -q --no-input -r requirements.txt -c constraints.txt" ;;
  local)
    # torch — CPU-сборкой, иначе pip потянет CUDA на гигабайты
    INSTALL="pip install -q --no-input torch --index-url https://download.pytorch.org/whl/cpu && pip install -q --no-input -r requirements-e5.txt" ;;
  *)
    echo "EMBEDDER_BACKEND=$EMBEDDER_BACKEND не понимаю: api или local" >&2
    exit 1 ;;
esac

# -e VAR без значения передаёт переменную, только если она задана здесь.
docker run --rm \
  -v "$PWD:/work" -w /work \
  -v tnved-pip:/root/.cache/pip \
  -v tnved-hf:/root/.cache/huggingface \
  -e HF_HOME=/root/.cache/huggingface \
  -e EMBEDDER_BACKEND \
  -e EMBEDDINGS_BASE_URL \
  -e EMBEDDINGS_MODEL \
  -e EMBEDDINGS_API_KEY \
  -e EMBEDDINGS_BATCH \
  -e EMBEDDINGS_TIMEOUT \
  -e EMBED_CHUNK="${EMBED_CHUNK:-2048}" \
  -e EMBED_THREADS="${EMBED_THREADS:-4}" \
  -e TOKENIZERS_PARALLELISM=false \
  -e INSTALL="$INSTALL" \
  python:3.11-slim sh -c '
    set -e
    eval "$INSTALL"
    python -u build_index.py
  '
