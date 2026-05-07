"""
Загрузка ресурсов ТН ВЭД: SQLite (всегда) + опционально FAISS + embedding-модель.

LITE_MODE=1 → пропускаем FAISS/embedder. search() в этом режиме отдаёт
все 10-значные листья из выбранной группы напрямую из SQLite.
Это приемлемо для пилота / слабого сервера, где собрать FAISS-индекс
или прогрузить sentence-transformers нет возможности.

LITE_MODE=0 (или не задан) → нормальный режим с векторным поиском.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "tnved.db"
FAISS_PATH = BASE / "data" / "tnved.faiss"
META_PATH = BASE / "data" / "tnved_meta.json"

EMBEDDER_MODEL = "intfloat/multilingual-e5-base"

LITE_MODE = os.environ.get("LITE_MODE", "0") == "1"
LITE_TOP_K = int(os.environ.get("LITE_TOP_K", "120"))


class TNVEDStore:
    lite: bool
    embedder: Any  # SentenceTransformer | None
    index: Any     # faiss.Index | None
    meta: list[dict]
    groups: list[dict]
    code_to_meta: dict[str, dict]

    def __init__(self):
        self.lite = LITE_MODE

        if self.lite:
            print("[tnved] LITE_MODE=1 — без FAISS/embedder. search() работает на чистом SQLite.")
            self.embedder = None
            self.index = None
            # meta берём из SQLite — нужен duty_rate, full_path и пр. в результатах.
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT code, description, full_path, duty_rate, data_source
                    FROM codes
                    WHERE level >= 3
                    ORDER BY code
                """).fetchall()
            self.meta = [
                {
                    "code": r["code"],
                    "description": r["description"],
                    "full_path": r["full_path"],
                    "duty_rate": r["duty_rate"],
                    "data_source": r["data_source"],
                }
                for r in rows
            ]
        else:
            # Полный режим — sentence-transformers + FAISS
            from sentence_transformers import SentenceTransformer  # noqa: WPS433
            import faiss  # noqa: WPS433
            import numpy as np  # noqa: WPS433
            self._np = np
            self._faiss = faiss

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

    # ─── свойство для совместимости с существующими print'ами ─────────────

    @property
    def index_ntotal(self) -> int:
        if self.index is None:
            return len(self.meta)
        return int(self.index.ntotal)

    # ─── поиск ────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 12, group_code: str | None = None) -> list[dict]:
        """В нормальном режиме — векторный поиск; в LITE — sql-фильтр по группе."""
        if self.lite:
            return self._search_lite(group_code)

        # Vector path (full mode)
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

    def _search_lite(self, group_code: str | None) -> list[dict]:
        """SQLite-only fallback. Все листы группы (≤ LITE_TOP_K), без векторного скоринга."""
        sql_args: tuple
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if group_code:
                rows = conn.execute(
                    """
                    SELECT code, description, full_path, duty_rate
                    FROM codes
                    WHERE level = 4 AND code LIKE ?
                    ORDER BY code
                    LIMIT ?
                    """,
                    (f"{group_code}%", LITE_TOP_K),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT code, description, full_path, duty_rate
                    FROM codes
                    WHERE level = 4
                    ORDER BY code
                    LIMIT ?
                    """,
                    (LITE_TOP_K,),
                ).fetchall()
        return [
            {
                "code": r["code"],
                "description": r["description"],
                "full_path": r["full_path"],
                "duty_rate": r["duty_rate"],
                "score": 1.0,
            }
            for r in rows
        ]

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
