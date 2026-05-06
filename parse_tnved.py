"""
Парсит сырые данные ТН ВЭД (CSV / XML / Excel) и сохраняет в SQLite.

Запуск: python parse_tnved.py
Результат: data/tnved.db
  - таблица codes(code, description, level, parent_code, full_path)
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

RAW_DIR = Path(__file__).parent / "data" / "raw"
DB_PATH = Path(__file__).parent / "data" / "tnved.db"


# ─── helpers ──────────────────────────────────────────────────────────────────

def code_to_level(code: str) -> int:
    """Определяет уровень иерархии по длине кода."""
    length = len(code.strip())
    if length <= 2:
        return 1   # группа
    if length == 4:
        return 2   # позиция
    if length <= 6:
        return 3   # субпозиция
    if length <= 8:
        return 3   # промежуточный уровень (8 знаков) — тоже субпозиция
    return 4       # подсубпозиция (10 знаков) — листовой узел


def parent_code(code: str) -> str | None:
    """Возвращает код родителя."""
    code = code.strip()
    if len(code) <= 2:
        return None
    if len(code) == 4:
        return code[:2]
    if len(code) == 6:
        return code[:4]
    if len(code) >= 7:
        return code[:6]
    return None


def build_full_paths(rows: list[dict]) -> dict[str, str]:
    """Строит словарь code → full_path вида 'Группа 01 > Позиция 0101 > ...'"""
    by_code = {r["code"]: r["description"] for r in rows}
    paths: dict[str, str] = {}

    def get_path(code: str) -> str:
        if code in paths:
            return paths[code]
        p = parent_code(code)
        desc = by_code.get(code, code)
        if p and p in by_code:
            paths[code] = get_path(p) + " → " + desc
        else:
            paths[code] = desc
        return paths[code]

    for r in rows:
        get_path(r["code"])

    return paths


# ─── parsers ──────────────────────────────────────────────────────────────────

CODE_COLS = {"code", "kod", "код", "tnved", "code_tnved", "id"}
DESC_COLS = {"description", "name", "наименование", "название", "desc", "descr", "simple_nam", "nam", "наим"}


def parse_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        # Пропускаем строки до настоящего заголовка (с именами столбцов)
        lines = f.readlines()

    header_idx = 0
    for i, line in enumerate(lines[:20]):
        cols = [c.strip().strip('"').lower() for c in line.split(",")]
        if any(c in CODE_COLS for c in cols) and any(c in DESC_COLS for c in cols):
            header_idx = i
            break

    content = "".join(lines[header_idx:])
    delimiter = ";" if content.count(";") > content.count(",") else ","

    import io
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    fieldnames = reader.fieldnames or []

    code_col = next((h for h in fieldnames if h.strip().lower() in CODE_COLS), None)
    desc_col = next((h for h in fieldnames if h.strip().lower() in DESC_COLS), None)

    if not code_col or not desc_col:
        if len(fieldnames) >= 2:
            code_col, desc_col = fieldnames[0], fieldnames[1]
        else:
            raise ValueError(f"Не удалось определить столбцы в {path}. Заголовки: {fieldnames}")

    for row in reader:
        code = str(row.get(code_col, "")).strip().lstrip("'\"")
        desc = str(row.get(desc_col, "")).strip()
        if code and desc and code.isdigit():
            rows.append({"code": code, "description": desc})

    return rows


def parse_xml(path: Path) -> list[dict]:
    from lxml import etree

    tree = etree.parse(str(path))
    root = tree.getroot()
    rows = []

    # Пробуем несколько возможных структур XML
    # Структура 1: <TNVED><RAZDEL><GRUPPA><POZICIYA kod="..." naim="...">
    for elem in root.iter():
        tag = elem.tag.split("}")[-1].upper() if "}" in elem.tag else elem.tag.upper()
        code = elem.get("kod") or elem.get("code") or elem.get("KOD") or elem.get("CODE")
        desc = elem.get("naim") or elem.get("name") or elem.get("NAIM") or elem.get("NAME") or elem.text
        if code and desc and str(code).strip().isdigit():
            rows.append({"code": str(code).strip(), "description": str(desc).strip()})

    return rows


def parse_excel(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.active
    rows_raw = list(ws.iter_rows(values_only=True))

    if not rows_raw:
        raise ValueError("Excel-файл пустой")

    # Ищем строку с заголовками
    header_row = 0
    for i, row in enumerate(rows_raw[:10]):
        row_str = [str(c).lower() if c else "" for c in row]
        if any(k in " ".join(row_str) for k in ("код", "code", "наим", "name")):
            header_row = i
            break

    headers = [str(c).lower().strip() if c else "" for c in rows_raw[header_row]]
    code_idx = next((i for i, h in enumerate(headers) if any(k in h for k in ("код", "code", "tnved"))), 0)
    desc_idx = next((i for i, h in enumerate(headers) if any(k in h for k in ("наим", "name", "описан", "descr"))), 1)

    rows = []
    for row in rows_raw[header_row + 1:]:
        if len(row) <= max(code_idx, desc_idx):
            continue
        code = str(row[code_idx]).strip() if row[code_idx] is not None else ""
        desc = str(row[desc_idx]).strip() if row[desc_idx] is not None else ""
        code = code.lstrip("'\"").rstrip(".0")  # убираем лишние символы Excel
        if code and desc and code.isdigit():
            rows.append({"code": code, "description": desc})

    wb.close()
    return rows


def load_raw_data() -> list[dict]:
    """Находит и парсит первый доступный файл в data/raw/."""
    candidates = [
        (RAW_DIR / "tnved.csv",   parse_csv),
        (RAW_DIR / "tnved.xml",   parse_xml),
        (RAW_DIR / "tnved.xlsx",  parse_excel),
    ]

    for path, parser in candidates:
        if path.exists() and path.stat().st_size > 1000:
            print(f"Парсим: {path}")
            return parser(path)

    print("Файл данных не найден в data/raw/")
    print("Запустите: python fetch_tnved.py")
    sys.exit(1)


# ─── DB ───────────────────────────────────────────────────────────────────────

def save_to_db(rows: list[dict]):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE codes (
            code        TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            level       INTEGER NOT NULL,
            parent_code TEXT,
            full_path   TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX idx_level ON codes(level)
    """)

    print("Строим полные пути ...")
    paths = build_full_paths(rows)

    records = [
        (
            r["code"],
            r["description"],
            code_to_level(r["code"]),
            parent_code(r["code"]),
            paths.get(r["code"], r["description"]),
        )
        for r in rows
    ]

    cur.executemany(
        "INSERT OR IGNORE INTO codes VALUES (?,?,?,?,?)",
        records,
    )

    conn.commit()

    total = cur.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
    leaves = cur.execute("SELECT COUNT(*) FROM codes WHERE level=4").fetchone()[0]
    conn.close()

    print(f"Сохранено записей: {total:,}  (листовых кодов: {leaves:,})")


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    rows = load_raw_data()
    print(f"Прочитано строк: {len(rows):,}")

    if len(rows) < 100:
        print("Слишком мало записей — возможно, файл повреждён или неверный формат.")
        sys.exit(1)

    # Нормализуем коды (убираем незначащие нули в начале, если они лишние)
    # Оставляем как есть — parse_* уже возвращают нормализованные коды

    save_to_db(rows)
    print(f"\nГотово: {DB_PATH}")
    print("Следующий шаг: python build_index.py")


if __name__ == "__main__":
    main()
