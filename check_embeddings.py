"""
Сверка сервера векторов с индексом: те же ли векторы считает EMBEDDINGS_BASE_URL,
которыми собран data/tnved.faiss.

Паспорт индекса (data/tnved_index_info.json) хранит только имя модели, а
«api:bge-m3» одинаково и у Ollama, и у vLLM — подмену сервера, сборки или
квантизации сверка паспорта на старте не заметит. Здесь сравниваются сами
векторы: N текстов индекса заново векторизуются активным бэкендом (настройки
те же, что у сервиса: EMBEDDER_BACKEND, EMBEDDINGS_*) и сравниваются
с записанными в индексе.

Запуск:  python check_embeddings.py [N]       N текстов, по умолчанию 200
         docker compose exec app python check_embeddings.py
Выход 0 — векторы те же (минимальный косинус не ниже 0,995), 1 — нет.

Сверка 25.09.2026: vLLM 10.10.33.10:11434 против индекса, собранного на Ollama
10.10.28.10:11434, — косинус мин 0,9991, медиана 0,99998; первый кандидат
поиска совпал в 30 из 30 синтетических запросов.
"""

import json
import os
import sys

import faiss
import numpy as np

from embedder import get_embedder
from tnved_data import FAISS_PATH, INFO_PATH, META_PATH

# Та же модель на другом движке — 0,999 и выше. Ниже 0,995 — уже другая модель
# или грубая квантизация: кандидаты поиска поедут.
MIN_COS = 0.995


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    index = faiss.read_index(str(FAISS_PATH))
    with open(META_PATH, encoding="utf-8") as f:
        meta = json.load(f)
    if "index_text" not in meta[0]:
        print("ОТКАЗ: в tnved_meta.json нет index_text — индекс собран старой версией "
              "build_index.py, сверять не с чем. Пересоберите индекс.")
        return 1
    passport = {}
    if INFO_PATH.exists():
        with open(INFO_PATH, encoding="utf-8") as f:
            passport = json.load(f)

    try:
        emb = get_embedder()
    except RuntimeError as exc:
        print(f"ОТКАЗ: {exc}")
        return 1
    where = os.environ.get("EMBEDDINGS_BASE_URL", "").strip()
    print(f"Индекс: {passport.get('embedder', 'паспорта нет')}, {index.ntotal:,} векторов, dim {index.d}")
    print(f"Сервер: {emb.name}" + (f" @ {where}" if emb.name.startswith("api:") else ""))
    if passport.get("embedder") and passport["embedder"] != emb.name:
        print("ВНИМАНИЕ: имя модели не как в паспорте — с этими настройками сервис не стартует.")

    ids = np.random.default_rng(0).choice(len(meta), size=min(n, len(meta)), replace=False)
    try:
        fresh = emb.encode([meta[int(i)]["index_text"] for i in ids], is_query=False)
    except RuntimeError as exc:
        print(f"ОТКАЗ: {exc}")
        return 1
    if fresh.shape[1] != index.d:
        print(f"НЕ СОВПАДАЕТ: размерность {fresh.shape[1]}, а в индексе {index.d} — другая модель.")
        return 1

    stored = np.vstack([index.reconstruct(int(i)) for i in ids])
    cos = (fresh * stored).sum(axis=1)
    print(f"Косинус с векторами индекса, {len(ids)} текстов: мин {cos.min():.5f}, "
          f"медиана {np.median(cos):.5f}; ниже {MIN_COS} — {int((cos < MIN_COS).sum())}")
    if cos.min() < MIN_COS:
        worst = meta[int(ids[int(cos.argmin())])]["code"]
        print(f"НЕ СОВПАДАЕТ: сервер считает не те векторы, которыми собран индекс "
              f"(хуже всего — код {worst}). Верните прежний сервер или пересоберите "
              f"индекс на этом: python build_index.py")
        return 1
    print("СОВПАДАЕТ: индекс годен для этого сервера векторов.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
