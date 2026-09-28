"""Уровень 1 — таможенные декларации холдинга (Д0).

Декларация — самый точный источник кода: код по ней уже оформлен и выпущен. Поэтому до
модели: если артикул строки есть в декларациях и у него один действующий код — отдаём этот
код и ссылку на ДТ, модель не зовём.

Данные — выгрузка SLAI (Q-VYGRUZKA-DT-ARTIKUL-KOD-1): CSV «;», UTF-8 (можно .gz), колонки
article, description, hs_code, dt_number, dt_date (item_no — если есть). Код взят у товара
ДТ (гр. 33), артикул — у строки товара: так их разводит разбор SLAI. Файл в git и образ не
входит (incoming/, data/).

Путь — DECLARATIONS_PATH. Не задан — уровень выключен. Задан, но файл не читается или в нём
нет нужных колонок — сервис не стартует: явно переданное значение, которое нельзя применить,
молча не игнорируем.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("tnved.declarations")

REQUIRED = ("article", "description", "hs_code", "dt_number", "dt_date")

# Кириллица, похожая на латиницу: в артикулах их путают при наборе («С» и «C»).
_LOOKALIKE = str.maketrans("АВЕКМНОРСТХУ", "ABEKMHOPCTXY")
_SEPARATORS = re.compile(r"[\s\-‐‑–—._/\\]+")
# Не артикулы (уже без разделителей и с латиницей вместо двойников): «б/н», «нет», «n/a», нули.
_NOT_ARTICLE = re.compile(r"^(?:БH|BH|HET|NONE|NA|0+)$")
MIN_ARTICLE = 3


def norm_article(raw: object) -> str:
    """Артикул для сравнения: верхний регистр, кириллица-двойник → латиница, без разделителей.

    «ab-12.34 с» и «AB1234C» — один артикул. Короче MIN_ARTICLE знаков — не артикул
    (пустая строка): «1» или «A» совпадут с чем угодно.
    """
    s = _SEPARATORS.sub("", str(raw or "").upper().translate(_LOOKALIKE))
    if len(s) < MIN_ARTICLE or _NOT_ARTICLE.match(s):
        return ""
    return s


def _digits(raw: object) -> str:
    return re.sub(r"\D", "", str(raw or ""))


@dataclass
class _Article:
    codes: Counter = field(default_factory=Counter)
    refs: dict = field(default_factory=lambda: defaultdict(list))  # код → [(номер ДТ, дата)]


class DeclarationIndex:
    """Свод «артикул → коды из ДТ» по выгрузке."""

    def __init__(self, rows: list[dict]):
        self.by_article: dict[str, _Article] = defaultdict(_Article)
        self.rows = 0
        self.skipped = Counter()
        for row in rows:
            code = _digits(row.get("hs_code"))
            if len(code) != 10:
                self.skipped["код не 10 знаков"] += 1
                continue
            art = norm_article(row.get("article"))
            if not art:
                self.skipped["нет артикула"] += 1
                continue
            entry = self.by_article[art]
            entry.codes[code] += 1
            ref = (str(row.get("dt_number") or "").strip(), str(row.get("dt_date") or "").strip())
            if ref not in entry.refs[code]:
                entry.refs[code].append(ref)
            self.rows += 1

    @classmethod
    def load(cls, path: str | Path) -> "DeclarationIndex":
        path = Path(path)
        raw = path.read_bytes()
        if path.suffix == ".gz":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        header = [h.strip() for h in reader.fieldnames or []]
        missing = [c for c in REQUIRED if c not in header]
        if missing:
            raise ValueError(f"{path.name}: нет колонок {', '.join(missing)} (есть: {', '.join(header)})")
        rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
        index = cls(rows)
        log.info("[decl] %s: строк %d, артикулов %d, пропущено %s",
                 path.name, index.rows, len(index.by_article), dict(index.skipped) or 0)
        return index

    def lookup(self, article: object, store) -> dict | None:
        """Код по артикулу, если декларации однозначны; иначе None и причина в "miss".

        Возвращает {"code", "article", "rows", "dt_refs", "retired_codes"} — или
        {"miss": причина, "codes": [...]} (артикул есть, но код не отдаём), или None (артикула нет).
        Отдаём только действующий код; снятые коды из старых ДТ — в retired_codes, не мешают.
        Два и больше действующих кода — не выбираем сами: это решает модель или человек.
        """
        art = norm_article(article)
        entry = self.by_article.get(art) if art else None
        if entry is None:
            return None
        current, retired = [], []
        for code in entry.codes:
            status, _ = store.code_status(code)
            (current if status == "current" else retired).append(code)
        if len(current) != 1:
            reason = "в декларациях несколько действующих кодов" if current else "код из декларации снят"
            return {"miss": reason, "article": art,
                    "codes": sorted(entry.codes, key=lambda c: -entry.codes[c])}
        code = current[0]
        return {
            "code": code,
            "article": art,
            "rows": entry.codes[code],
            "dt_refs": entry.refs[code][:3],
            "retired_codes": sorted(retired),
        }


def load_from_env() -> DeclarationIndex | None:
    """DECLARATIONS_PATH не задан — None (уровень выключен); задан и не читается — исключение."""
    path = os.environ.get("DECLARATIONS_PATH", "").strip()
    if not path:
        log.info("[decl] DECLARATIONS_PATH не задан — уровень деклараций выключен")
        return None
    return DeclarationIndex.load(path)
