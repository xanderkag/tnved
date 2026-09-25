#!/bin/sh
# Считает FAISS-индекс ТН ВЭД в одноразовом контейнере на прод-хосте.
#
# Зачем через Docker: на рабочей машине не хватает памяти под модель
# (OSError 1455, «paging file is too small»), а на сервере нет torch и
# ставить его в системный python не нужно.
#
# Кэши pip и HuggingFace живут в именованных volume — повторный запуск
# не качает 1,1 ГБ модели заново. Векторы кэшируются в data/tnved_vecs.npy,
# промежуточные куски — в data/vecs_parts/, так что прогон возобновляемый.
#
# Запуск:  ./build-in-docker.sh
set -e
cd "$(dirname "$0")"

docker run --rm \
  -v "$PWD:/work" -w /work \
  -v tnved-pip:/root/.cache/pip \
  -v tnved-hf:/root/.cache/huggingface \
  -e HF_HOME=/root/.cache/huggingface \
  -e EMBED_BATCH="${EMBED_BATCH:-64}" \
  -e EMBED_CHUNK="${EMBED_CHUNK:-2048}" \
  -e EMBED_THREADS="${EMBED_THREADS:-4}" \
  -e TOKENIZERS_PARALLELISM=false \
  python:3.11-slim sh -c '
    set -e
    pip install -q --no-input torch --index-url https://download.pytorch.org/whl/cpu
    pip install -q --no-input sentence-transformers faiss-cpu numpy
    python -u build_index.py
  '
