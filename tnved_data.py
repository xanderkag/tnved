"""
Загрузка ресурсов ТН ВЭД: SQLite, FAISS, embedding-модель, список групп.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "tnved.db"
FAISS_PATH = BASE / "data" / "tnved.faiss"
META_PATH = BASE / "data" / "tnved_meta.json"

EMBEDDER_MODEL = "intfloat/multilingual-e5-base"


class TNVEDStore:
    embedder: SentenceTransformer
    index: Any
    meta: list[dict]
    groups: list[dict]
    code_to_meta: dict[str, dict]

    def __init__(self):
        self.embedder = SentenceTransformer(EMBEDDER_MODEL)
        self.index = faiss.read_index(str(FAISS_PATH))
        with open(META_PATH, encoding="utf-8") as f:
            self.meta = json.load(f)
        self.code_to_meta = {m["code"]: m for m in self.meta}

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT code, description FROM codes WHERE level=1 ORDER BY code"
            ).fetchall()
            self.groups = [{"code": r["code"], "description": r["description"]} for r in rows]

    # ─── векторный поиск ──────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 12, group_code: str | None = None) -> list[dict]:
        # При фильтре по группе берём с запасом, чтобы после отсева осталось достаточно
        oversample = top_k * 8 if group_code else top_k
        vec = self.embedder.encode(
            [f"query: {query}"],
            normalize_embeddings=True,
            convert_to_numpy=True,
        ).astype("float32")
        scores, ids = self.index.search(vec, oversample)
        results: list[dict] = []
        for score, idx in zip(scores[0], ids[0]):
            if idx < 0:
                continue
            item = self.meta[idx]
            if group_code and not item["code"].startswith(group_code):
                continue
            results.append({**item, "score": float(score)})
            if len(results) >= top_k:
                break
        return results

    # ─── работа со справочником ───────────────────────────────────────────

    def hierarchy(self, code: str) -> list[dict]:
        """Возвращает цепочку от группы до самого кода: [{code, description}, ...]."""
        code = code.strip()
        if not code:
            return []
        chain_codes = []
        for length in (2, 4, 6, 8, 10):
            if len(code) >= length:
                chain_codes.append(code[:length])
        # Уникализируем по коду
        seen = set()
        chain = []
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            for c in chain_codes:
                if c in seen:
                    continue
                seen.add(c)
                row = conn.execute(
                    "SELECT code, description FROM codes WHERE code = ?", (c,)
                ).fetchone()
                if row:
                    chain.append({"code": row["code"], "description": row["description"]})
        return chain

    def group_info(self, group_code: str) -> dict | None:
        """Информация о группе (2 знака)."""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT code, description FROM codes WHERE code = ?", (group_code,)
            ).fetchone()
            if row:
                return {"code": row["code"], "description": row["description"]}
        return None

    def groups_list_for_prompt(self) -> str:
        """Форматированный список всех групп для подсказки LLM."""
        return "\n".join(f"  {g['code']}: {g['description']}" for g in self.groups)
