"""
НДС при ввозе в Россию для карточки кода (PAY-1): 22 % или 10 % по перечням Правительства.

Данные — data/vat10.json, его собирает parse_vat.py. Код в перечне — ещё не ставка 10 %: позиция
бывает «из …» или «(кроме телятины)», продовольственный перечень действует только для пищевых и
кормовых целей, детский — для товаров с детскими размерами или документом по ТР ТС 007/2011,
медицинский — для зарегистрированных лекарств и медизделий. Поэтому карточка отдаёт базовую
ставку 22 % и рядом — позиции перечней, под которые код подпадает, с их текстом и условием:
«10 %, если …». Решает декларант, сервис ставку 10 % сам не ставит.

Освобождение от НДС (НК ст. 150: часть медизделий, лекарств-сырья и пр.) здесь не учитывается.

VAT_LISTS_PATH — путь к vat10.json (по умолчанию data/vat10.json); нет файла — модуль молчит.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

RATE = "22%"
SOURCE = "НК РФ, ст. 164 п. 3 (ставка 22 % с 01.01.2026 — Федеральный закон от 28.11.2025 № 425-ФЗ)"
REDUCED_SOURCE = "НК РФ, ст. 164 п. 2"

# Условие перечня своими словами — по примечаниям к перечню (полный текст — в vat10.json, lists.*.notes).
CONDITIONS = {
    "food": "только для пищевых и кормовых целей (примечание 4 к перечню; кроме кодов 0407 11 000 0, 0407 19, "
            "0602, 1209 — для них цель не важна)",
    "kids": "только товары для детей: размеры из перечня (при наличии) и документ о соответствии по ТР ТС "
            "007/2011, 008/2011, 025/2012 или 018/2011 (примечание 1 к перечню)",
    "med": "лекарственные средства из госреестра или реестра ЕАЭС, медицинские изделия с регистрационным "
           "удостоверением (примечания 1 и 1(1) к перечню); ввоз части медизделий освобождён от НДС — ст. 150 НК",
}
_ANY_PURPOSE = ("0407110000", "040719", "0602", "1209")


class Vat:
    def __init__(self, data: dict):
        self.lists: dict[str, dict] = data["lists"]
        self.entries: list[dict] = data["entries"]
        self.fetched: str | None = data.get("fetched")

    def matches(self, code: str) -> list[dict]:
        hits = []
        for e in self.entries:
            if not (e["from"] <= code.ljust(10, "0") and code.ljust(10, "9") <= e["to"]):
                continue
            if any(code.startswith(x) for x in e["except"]):
                continue
            hits.append(e)
        return hits

    def vat(self, code: str) -> dict:
        out = {"rate": RATE, "source": SOURCE}
        reduced = []
        for e in self.matches(code):
            lst = self.lists[e["list"]]
            condition = CONDITIONS[e["list"]]
            if e["list"] == "food" and code.startswith(_ANY_PURPOSE):
                condition = "цель ввоза не важна (примечание 4 к перечню)"
            reduced.append({"rate": "10%", "list": lst["title"], "act": lst["act"], "edition": lst["edition"],
                            "position": e["spec"], "name": e["name"] or e["group"], "group": e["group"],
                            "partial": e["partial"], "condition": condition})
        if reduced:
            out["reduced"] = reduced
            out["reduced_source"] = REDUCED_SOURCE
            out["as_of"] = self.fetched
        return out


def load_from_env() -> Vat | None:
    path = os.environ.get("VAT_LISTS_PATH", str(Path(__file__).parent / "data" / "vat10.json"))
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return Vat(json.load(f))
