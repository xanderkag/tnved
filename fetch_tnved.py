"""
Скачивает данные ТН ВЭД ЕАЭС из открытых источников.

Стратегия:
1. Иерархия (группы / позиции / субпозиции) — берём из infoculture/opencustoms.
   Сохраняется как data/raw/tnved.csv.
2. Свежие коды + актуальные ставки пошлин — берём с TWS.BY (обновляется ежедневно).
   Сохраняется как data/raw/tws_tnved.xlsx.

parse_tnved.py сливает оба источника: иерархия из (1), свежие листья + duty_rate из (2).

Если что-то одно недоступно — работает в режиме fallback на оставшееся.
"""

from __future__ import annotations

import os
import sys
import requests

RAW_DIR = os.path.join(os.path.dirname(__file__), "data", "raw")

# Источник иерархии: пробуем по порядку, останавливаемся на первом успешном.
HIERARCHY_SOURCES = [
    {
        "url": "https://raw.githubusercontent.com/infoculture/opencustoms/master/data/tnved.csv",
        "filename": "tnved.csv",
        "description": "GitHub infoculture/opencustoms (ФТС открытые данные)",
    },
    {
        "url": "https://raw.githubusercontent.com/nicothin/TNVED/master/tnved.csv",
        "filename": "tnved.csv",
        "description": "GitHub nicothin/TNVED (резерв)",
    },
]

# Свежие листья + ставки. Качаем всегда, даже если иерархия уже есть.
TWS_SOURCE = {
    "url": "https://www.tws.by/tws/tnved/download/excel",
    "filename": "tws_tnved.xlsx",
    "description": "TWS.BY Excel (актуальные коды + duty rate, обновляется ежедневно)",
}

MANUAL_INSTRUCTIONS = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
АВТОМАТИЧЕСКАЯ ЗАГРУЗКА НЕ УДАЛАСЬ

Скачайте данные ТН ВЭД вручную одним из способов:

Вариант 1 (рекомендуется) — Excel от ФТС России:
  1. Перейдите на https://customs.ru/tnved
  2. Скачайте актуальный справочник в формате Excel (.xlsx)
  3. Сохраните файл как: data/raw/tnved.xlsx

Вариант 2 — XML от ЕЭК:
  1. Перейдите на https://eec.eaeunion.org/comission/department/dep_tamoj_infr/classif_goods/
  2. Скачайте XML-файл ТН ВЭД ЕАЭС
  3. Сохраните файл как: data/raw/tnved.xml

Вариант 3 — данные с alta.ru:
  1. Перейдите на https://www.alta.ru/tnved/
  2. Используйте экспорт данных (если доступен)
  3. Сохраните как: data/raw/tnved.csv

После сохранения файла запустите: python parse_tnved.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def try_download(source: dict) -> bool:
    url = source["url"]
    filename = source["filename"]
    description = source["description"]
    dest = os.path.join(RAW_DIR, filename)

    print(f"Попытка: {description} ...")
    try:
        resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        if len(resp.content) < 10_000:
            print(f"  Файл слишком маленький ({len(resp.content)} байт), пропускаем.")
            return False
        os.makedirs(RAW_DIR, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(resp.content)
        print(f"  Сохранено: {dest} ({len(resp.content):,} байт)")
        return True
    except Exception as e:
        print(f"  Ошибка: {e}")
        return False


def try_tnved_package() -> bool:
    """Если установлен пакет tnved — экспортируем данные из него."""
    try:
        import tnved  # type: ignore
        import csv

        dest = os.path.join(RAW_DIR, "tnved.csv")
        os.makedirs(RAW_DIR, exist_ok=True)

        # Пробуем разные атрибуты пакета
        data = None
        for attr in ("CODES", "codes", "DATA", "data", "ALL", "all_codes"):
            if hasattr(tnved, attr):
                data = getattr(tnved, attr)
                break

        if data is None:
            return False

        with open(dest, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["code", "description"])
            if isinstance(data, dict):
                for code, desc in data.items():
                    writer.writerow([code, desc])
            elif isinstance(data, (list, tuple)):
                for item in data:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        writer.writerow([item[0], item[1]])

        print(f"  Данные из пакета tnved сохранены: {dest}")
        return True
    except ImportError:
        return False
    except Exception as e:
        print(f"  Пакет tnved: ошибка {e}")
        return False


def hierarchy_exists() -> str | None:
    """Иерархия уже скачана?"""
    for name in ("tnved.csv", "tnved.xml", "tnved.xlsx"):
        path = os.path.join(RAW_DIR, name)
        if os.path.exists(path) and os.path.getsize(path) > 10_000:
            return path
    return None


def tws_exists() -> str | None:
    path = os.path.join(RAW_DIR, TWS_SOURCE["filename"])
    if os.path.exists(path) and os.path.getsize(path) > 10_000:
        return path
    return None


def fetch_hierarchy() -> bool:
    existing = hierarchy_exists()
    if existing:
        print(f"Иерархия уже скачана: {existing}")
        return True

    for source in HIERARCHY_SOURCES:
        if try_download(source):
            return True

    print("Пробуем пакет tnved ...")
    if try_tnved_package():
        return True

    return False


def fetch_tws() -> bool:
    existing = tws_exists()
    if existing:
        print(f"TWS.BY уже скачан: {existing}")
        return True
    return try_download(TWS_SOURCE)


def main():
    ok_hier = fetch_hierarchy()
    ok_tws = fetch_tws()

    if not ok_hier and not ok_tws:
        print(MANUAL_INSTRUCTIONS)
        sys.exit(1)

    if not ok_hier:
        print("\nWARN: иерархия не скачана — parse_tnved.py соберёт что сможет из TWS.BY.")
    if not ok_tws:
        print("\nWARN: TWS.BY не скачан — у кодов не будет свежих ставок.")

    print("\nГотово. Запустите: python parse_tnved.py")


if __name__ == "__main__":
    main()
