"""
Скачивает данные ТН ВЭД ЕАЭС из открытых источников.

Порядок попыток:
1. GitHub-репозиторий с CSV-данными
2. PyPI-пакет tnved (если установлен)

Если ничего не получилось — выводит инструкции для ручного скачивания.

Результат: data/raw/tnved.csv или data/raw/tnved.xml
"""

from __future__ import annotations

import os
import sys
import requests

RAW_DIR = os.path.join(os.path.dirname(__file__), "data", "raw")

# Известные открытые источники CSV с ТН ВЭД (проверено)
SOURCES = [
    {
        # Открытые данные ФТС России — проверенный источник, ~11k строк
        # Столбцы: КОД, SIMPLE_NAM
        "url": "https://raw.githubusercontent.com/infoculture/opencustoms/master/data/tnved.csv",
        "filename": "tnved.csv",
        "description": "GitHub infoculture/opencustoms (ФТС открытые данные)",
    },
    {
        # Excel со всеми кодами + ставками — обновляется ежедневно
        "url": "https://www.tws.by/tws/tnved/download/excel",
        "filename": "tnved.xlsx",
        "description": "TWS.BY Excel (актуальные коды + ставки)",
    },
    {
        "url": "https://raw.githubusercontent.com/nicothin/TNVED/master/tnved.csv",
        "filename": "tnved.csv",
        "description": "GitHub nicothin/TNVED",
    },
]

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
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
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


def already_exists() -> str | None:
    """Проверяет, есть ли уже скачанный файл."""
    for name in ("tnved.csv", "tnved.xml", "tnved.xlsx"):
        path = os.path.join(RAW_DIR, name)
        if os.path.exists(path) and os.path.getsize(path) > 10_000:
            return path
    return None


def main():
    existing = already_exists()
    if existing:
        print(f"Файл уже существует: {existing}")
        print("Удалите его и запустите повторно, чтобы обновить данные.")
        return

    for source in SOURCES:
        if try_download(source):
            print("\nГотово. Запустите: python parse_tnved.py")
            return

    print("Пробуем пакет tnved ...")
    if try_tnved_package():
        print("\nГотово. Запустите: python parse_tnved.py")
        return

    print(MANUAL_INSTRUCTIONS)
    sys.exit(1)


if __name__ == "__main__":
    main()
