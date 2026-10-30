"""
Обязательная маркировка «Честный знак» для карточки кода (PAY-1).

Данные — data/marking.json, его собирает parse_mark.py из перечня по распоряжению Правительства РФ
от 28.04.2018 № 792-р. Код в перечне — ещё не маркировка: по сноске <*> товар определяют одновременно
кодом ТН ВЭД и кодом ОКПД 2, у части позиций — ещё наименованием, регистрацией или оговоркой
«в части …», «за исключением …». Поэтому карточка отдаёт позиции перечня, под которые код подпадает,
с наименованием, сроком и оговоркой: «подлежит, если товар — …». Решает участник оборота.

Лекарства маркируются по другому акту (закон № 61-ФЗ, ПП № 1556) — здесь их нет; эксперименты
(товары до введения обязательной маркировки) — тоже.

MARKING_PATH — путь к marking.json (по умолчанию data/marking.json); нет файла — модуль молчит.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

SOURCE = "Перечень товаров, подлежащих обязательной маркировке средствами идентификации (распоряжение " \
         "Правительства РФ от 28.04.2018 № 792-р)"


class Marking:
    def __init__(self, data: dict):
        self.entries: list[dict] = data["entries"]
        self.edition: str = data.get("edition", "")
        self.general: str = data.get("general", "")
        self.fetched: str | None = data.get("fetched")

    def matches(self, code: str) -> list[dict]:
        return [e for e in self.entries
                if e["from"] <= code.ljust(10, "0") and code.ljust(10, "9") <= e["to"]
                and not any(code.startswith(x) for x in e["except"])]

    def marking(self, code: str, today: dt.date | None = None) -> dict | None:
        today = (today or dt.date.today()).isoformat()
        items, seen = [], set()
        for e in self.matches(code):
            key = (e["item"], e["spec"], e["start"])
            if key in seen:  # позиция в две строки с одними кодами — разные ОКПД 2, тот же срок
                continue
            seen.add(key)
            items.append({"item": e["item"], "name": e["name"],
                          "group": " / ".join(x for x in (e["section"], e["subsection"]) if x),
                          "position": e["spec"], "partial": e["partial"], "okpd": e["okpd"],
                          "start": e["start"], "in_force": e["start"] <= today, "condition": e["note"]})
        if not items:
            return None
        return {"items": items, "source": SOURCE, "edition": self.edition, "general": self.general,
                "as_of": self.fetched}


def load_from_env() -> Marking | None:
    path = os.environ.get("MARKING_PATH", str(Path(__file__).parent / "data" / "marking.json"))
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return Marking(json.load(f))
