"""
Загрузка ресурсов ТН ВЭД: SQLite (всегда) + опционально FAISS + embedding-модель.

LITE_MODE=1 → пропускаем FAISS/embedder. search() в этом режиме отдаёт
первые LITE_TOP_K действующих 10-значных листьев группы по порядку кода,
напрямую из SQLite. Только проверить, что стек поднимается: для прогонов
и пилота не годится — в большой группе нужный код в эти 120 не попадает.

LITE_MODE=0 (или не задан) → нормальный режим с векторным поиском.

В обоих режимах кандидаты — только действующие 10-значные коды (is_current_leaf).
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "tnved.db"
FAISS_PATH = BASE / "data" / "tnved.faiss"
META_PATH = BASE / "data" / "tnved_meta.json"
INFO_PATH = BASE / "data" / "tnved_index_info.json"

LITE_MODE = os.environ.get("LITE_MODE", "0") == "1"
LITE_TOP_K = int(os.environ.get("LITE_TOP_K", "120"))

# Ответом может быть только действующий 10-значный код — тот, что есть в тарифе TWS.
# 6- и 8-значные в индексе остаются, но это путь, а не ответ. Коды только из дерева
# 2017 (data_source='hierarchy') сняты: модель уверенно выбирала 9401300001 вместо
# действующих 9401310000/9401390000 — с таким кодом декларацию не примут.
CURRENT_SOURCES = ("hierarchy+tws", "tws")


def is_current_leaf(item: dict) -> bool:
    return len(item["code"]) == 10 and item.get("data_source") in CURRENT_SOURCES


class TNVEDStore:
    lite: bool
    embedder: Any  # embedder.Embedder | None
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
            # Полный режим — векторный поиск по FAISS
            import faiss  # noqa: WPS433
            import numpy as np  # noqa: WPS433

            from embedder import expected_name, get_embedder  # noqa: WPS433
            self._np = np
            self._faiss = faiss

            self.index = faiss.read_index(str(FAISS_PATH))
            with open(META_PATH, encoding="utf-8") as f:
                self.meta = json.load(f)
            # Путь — из базы: в tnved_meta.json он такой, каким был при сборке индекса,
            # а parse_tnved.py с тех пор мог его уточнить. Векторы по пути не считаются.
            with sqlite3.connect(DB_PATH) as conn:
                paths = dict(conn.execute("SELECT code, full_path FROM codes"))
            for m in self.meta:
                m["full_path"] = paths.get(m["code"], m.get("full_path"))

            self._check_index_passport(expected_name())
            self.embedder = get_embedder()
            # classify ищет одним описанием в группе и в каждой позиции от triage —
            # вектор запроса считаем один раз.
            self._query_vec = functools.lru_cache(maxsize=256)(
                lambda query: self.embedder.encode([query], is_query=True))
            print(f"[tnved] векторный поиск: {self.embedder.name}, "
                  f"{self.index.ntotal:,} векторов")

        self.code_to_meta = {m["code"]: m for m in self.meta}
        # Позиции (4 знака), у которых есть действующие коды: только такие от triage берём в поиск.
        self.current_headings = {m["code"][:4] for m in self.meta if is_current_leaf(m)}

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT code, description FROM codes WHERE level=1 ORDER BY code"
            ).fetchall()
            self.groups = [{"code": r["code"], "description": r["description"]} for r in rows]

    # ─── проверка совместимости индекса ───────────────────────────────────

    def _check_index_passport(self, active: str) -> None:
        """Индекс, собранный одной моделью, нельзя искать другой.

        У bge-m3 и e5-base разная размерность (1024 против 768) и разное
        векторное пространство. При несовпадении лучше упасть на старте
        с понятным сообщением, чем молча выдавать бессмысленные результаты.
        """
        if not INFO_PATH.exists():
            print(f"[tnved] ВНИМАНИЕ: нет паспорта индекса ({INFO_PATH.name}). "
                  f"Индекс собран старой версией build_index.py — совместимость "
                  f"с активной моделью «{active}» не проверена. "
                  f"Надёжнее пересобрать: python build_index.py")
            return

        with open(INFO_PATH, encoding="utf-8") as f:
            info = json.load(f)

        if info.get("embedder") != active:
            raise RuntimeError(
                f"Индекс собран моделью «{info.get('embedder')}», "
                f"а сейчас активна «{active}». Векторы несовместимы — поиск "
                f"выдавал бы случайные коды. Либо верните прежние настройки "
                f"векторизации, либо пересоберите индекс: python build_index.py"
            )

        if info.get("dim") and int(info["dim"]) != int(self.index.d):
            raise RuntimeError(
                f"Размерность индекса {self.index.d} не совпадает с паспортом "
                f"{info['dim']} — файлы data/ рассинхронизированы, пересоберите индекс."
            )

    # ─── свойство для совместимости с существующими print'ами ─────────────

    @property
    def index_ntotal(self) -> int:
        if self.index is None:
            return len(self.meta)
        return int(self.index.ntotal)

    # ─── поиск ────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 12, group_code: str | None = None) -> list[dict]:
        """Кандидаты — только действующие 10-значные коды (is_current_leaf).

        group_code — начало кода: группа (2 знака) или позиция (4).
        В нормальном режиме — векторный поиск; в LITE — sql-фильтр по началу кода.
        """
        if self.lite:
            return self._search_lite(group_code)

        # Vector path (full mode). Префикс запроса, если нужен, добавит бэкенд.
        # Кандидатов в индексе меньше половины (остальное — 6/8-значные и снятые),
        # и их длинные тексты вытесняют листья из верха выдачи. Индекс точный
        # (IndexFlatIP), поэтому берём выдачу целиком: 30 тыс. оценок — миллисекунды.
        vec = self._query_vec(query)
        scores, ids = self.index.search(vec, int(self.index.ntotal))
        results: list[dict] = []
        for score, idx in zip(scores[0], ids[0]):
            if idx < 0:
                continue
            item = self.meta[idx]
            if not is_current_leaf(item):
                continue
            if group_code and not item["code"].startswith(group_code):
                continue
            results.append({**item, "score": float(score)})
            if len(results) >= top_k:
                break

        # Группа задана, а в топе её кодов почти нет — так бывает, когда группа
        # маленькая или описание товара непохоже на формулировки классификатора
        # (например наименование — артикул). Отдавать пустой список нельзя:
        # группу уже определил triage, кандидаты обязаны быть. Дополняем
        # листьями группы из SQLite.
        if group_code and len(results) < top_k:
            have = {r["code"] for r in results}
            for row in self._search_lite(group_code):
                if row["code"] in have:
                    continue
                results.append({**row, "score": 0.0})
                if len(results) >= top_k:
                    break
        return results

    def _search_lite(self, group_code: str | None) -> list[dict]:
        """SQLite-only fallback. Действующие листы группы (≤ LITE_TOP_K), без векторного скоринга."""
        current = ", ".join("?" * len(CURRENT_SOURCES))
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if group_code:
                rows = conn.execute(
                    f"""
                    SELECT code, description, full_path, duty_rate, data_source
                    FROM codes
                    WHERE level = 4 AND length(code) = 10 AND data_source IN ({current})
                      AND code LIKE ?
                    ORDER BY code
                    LIMIT ?
                    """,
                    (*CURRENT_SOURCES, f"{group_code}%", LITE_TOP_K),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT code, description, full_path, duty_rate, data_source
                    FROM codes
                    WHERE level = 4 AND length(code) = 10 AND data_source IN ({current})
                    ORDER BY code
                    LIMIT ?
                    """,
                    (*CURRENT_SOURCES, LITE_TOP_K),
                ).fetchall()
        return [
            {
                "code": r["code"],
                "description": r["description"],
                "full_path": r["full_path"],
                "duty_rate": r["duty_rate"],
                "data_source": r["data_source"],
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

    def code_status(self, code: str) -> tuple[str, dict | None]:
        """Код по справочнику: current | retired | not_leaf | unknown — и его строка из codes.

        current — действующий 10-значный (можно отдавать как ответ); retired — только
        в дереве 2017; not_leaf — 2–8 знаков, уровень пути; unknown — кода нет вовсе.
        """
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT code, description, full_path, duty_rate, data_source FROM codes WHERE code = ?",
                (code,),
            ).fetchone()
        if row is None:
            return "unknown", None
        item = dict(row)
        if len(code) != 10:
            return "not_leaf", item
        return ("current" if is_current_leaf(item) else "retired"), item

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
