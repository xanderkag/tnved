"""
Строит FAISS-индекс по описаниям листовых кодов ТН ВЭД.

Запуск: python build_index.py
Результат:
  data/tnved.faiss   — векторный индекс
  data/tnved_meta.json — список {code, description, full_path}

Модель: intfloat/multilingual-e5-base
  Нужна инструкция-префикс "passage: " при индексировании.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

DB_PATH = Path(__file__).parent / "data" / "tnved.db"
FAISS_PATH = Path(__file__).parent / "data" / "tnved.faiss"
META_PATH = Path(__file__).parent / "data" / "tnved_meta.json"

MODEL_NAME = "intfloat/multilingual-e5-base"
BATCH_SIZE = 256


def load_leaves() -> list[dict]:
    if not DB_PATH.exists():
        print(f"База данных не найдена: {DB_PATH}")
        print("Запустите: python parse_tnved.py")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # Берём все коды, не только level=4, чтобы не зависеть от качества данных
    cur.execute("""
        SELECT code, description, full_path
        FROM codes
        WHERE level >= 3
        ORDER BY code
    """)
    rows = [{"code": r[0], "description": r[1], "full_path": r[2]} for r in cur.fetchall()]
    conn.close()
    return rows


def embed(model: SentenceTransformer, texts: list[str]) -> np.ndarray:
    # E5 требует префикс "passage: " для индексируемых текстов
    prefixed = [f"passage: {t}" for t in texts]
    vecs = model.encode(
        prefixed,
        batch_size=BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,  # нормализуем для косинусного сходства через inner product
        convert_to_numpy=True,
    )
    return vecs.astype("float32")


def main():
    print(f"Загружаем листовые коды из {DB_PATH} ...")
    rows = load_leaves()
    print(f"Кодов для индексирования: {len(rows):,}")

    if not rows:
        print("Нет данных для индексирования.")
        sys.exit(1)

    print(f"\nЗагружаем модель {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME)

    texts = [r["full_path"] or r["description"] for r in rows]

    print("\nВычисляем embeddings ...")
    vecs = embed(model, texts)

    print(f"\nСтроим FAISS-индекс (IndexFlatIP, dim={vecs.shape[1]}) ...")
    index = faiss.IndexFlatIP(vecs.shape[1])
    index.add(vecs)

    faiss.write_index(index, str(FAISS_PATH))
    print(f"Индекс сохранён: {FAISS_PATH}  ({index.ntotal:,} векторов)")

    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"Метаданные сохранены: {META_PATH}")

    print("\nГотово. Запустите: streamlit run app.py")


if __name__ == "__main__":
    main()
