"""
Ставка ввозной пошлины по официальному тексту ЕТТ ЕАЭС (PAY-1) — для карточки кода.

Данные — data/eec_rates.json, его собирает parse_rates.py из PDF ЕЭК. Ставка в PDF — базовая, а сноски
ЕТТ («5⁹ᶜ⁾») её меняют: льготная ставка на срок («0 % … с 01.07.2026 по 30.06.2027») или своя ставка при
ввозе в Россию («5 % … в отношении товаров, ввозимых в Российскую Федерацию»). Здесь сноска разбирается на
отрезки «ставка, с какого числа, по какое, только для России» и считается ставка на сегодня:

- rate — действующая для всего ЕАЭС: базовая или льготная по сноске, у которой сегодня внутри срока;
- rate_ru — своя ставка при ввозе в Россию, если сноска такую задаёт и она действует;
- base — базовая ставка из таблицы, если отличается от rate;
- notes — тексты сносок как есть: на странице их показывать рядом со ставкой.

Сверка 30.10.2026 с TWS.BY, 13 292 общих кода: действующая ставка расходится у 40. Из них 30 — редакции
групп, что вступают 01.11.2026 (их здесь пока не берём, см. ниже), 6 — легковые 8703: у TWS.BY нет
«либо 12,5%, но не менее … за 1 см³», 4 — у TWS.BY ставки нет. Ставку для России (сноски 9С, 19С …,
185 кодов) TWS.BY не показывает. Нет кода в PDF или редакция ещё не вступила — карточка берёт ставку
TWS.BY, как раньше (tnved_data._duty).

ETT_RATES_PATH — путь к eec_rates.json (по умолчанию data/eec_rates.json); нет файла — модуль молчит.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

SOURCE = "ЕТТ ЕАЭС (Решение Совета ЕЭК от 14.09.2021 № 80, с изменениями), PDF с сайта ЕЭК"

_MONTHS = {m: i for i, m in enumerate(("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                                       "августа", "сентября", "октября", "ноября", "декабря"), 1)}
_DATE = r"(\d\d\.\d\d\.\d{4}|\d{1,2} [а-я]+ \d{4} г\.)"
_SEGMENT = re.compile(r",\s*(?=с \d\d\.\d\d\.\d{4}\s*[–-])")  # «…, с 01.10.2022 – в размере 8 % …»


def _date(s: str) -> dt.date | None:
    if m := re.fullmatch(r"(\d\d)\.(\d\d)\.(\d{4})", s):
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if (m := re.fullmatch(r"(\d{1,2}) ([а-я]+) (\d{4}) г\.", s)) and m.group(2) in _MONTHS:
        return dt.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    return None


def percent(rate: str) -> str:
    """«25, но не менее 0,2 евро за 1 кг» → «25%, но не менее…»: голое число в ЕТТ — проценты."""
    return re.sub(r"(^|либо )(\d+(?:,\d+)?)(?=,\s|$)", r"\1\2%", rate.strip())


def segments(text: str) -> list[dict]:
    """Отрезки сноски: {rate, start, end, ru}. start None — «с даты вступления в силу решения»,
    end None — бессрочно. Отрезок без ставки (сноска утратила силу) пропускается."""
    out = []
    for seg in _SEGMENT.split(text):
        m = re.search(r"в размере (.+?)(?:,? применяется| в отношении|$)", seg)
        if not m or "утратил" in seg:
            continue
        rate = re.sub(r"\s*\(ноль\)\s*", "", m.group(1))
        rate = re.sub(r"\s*%\s*от\s+таможенн\s*ой\s+стоимости", "%", rate).strip(" ,.")
        start = re.search(r"\bс " + _DATE, seg)
        end = re.search(r"\bпо " + _DATE, seg)
        out.append({"rate": rate, "start": _date(start.group(1)) if start else None,
                    "end": _date(end.group(1)) if end else None, "ru": "Российск" in seg})
    return out


class Rates:
    def __init__(self, data: dict):
        self.rates: dict[str, dict] = data["rates"]
        self.notes: dict[str, str] = data["notes"]
        self.fetched: str | None = data.get("fetched")

    def duty(self, code: str, today: dt.date | None = None) -> dict | None:
        r = self.rates.get(code)
        if r is None or not r["rate"]:
            return None
        today = today or dt.date.today()
        # Дата в имени PDF — с какого дня действует редакция группы. Ещё не вступила — ставки в ней
        # будущие, а прежней редакции у нас нет: пусть карточка возьмёт TWS.BY.
        edition = re.search(r"_(\d\d\.\d\d\.\d{4})\.pdf$", r["source"])
        if edition and _date(edition.group(1)) > today:
            return None
        base = percent(r["rate"])
        rate, rate_ru = base, None
        for mark in r["notes"]:
            for s in segments(self.notes.get(mark, "")):
                if (s["start"] or today) <= today <= (s["end"] or today):
                    if s["ru"]:
                        rate_ru = s["rate"]
                    else:
                        rate = s["rate"]
        out = {"rate": rate, "source": SOURCE, "as_of": self.fetched,
               "edition": edition.group(1) if edition else None}
        if rate != base:
            out["base"] = base
        if rate_ru and rate_ru != rate:
            out["rate_ru"] = rate_ru
        if r["notes"]:
            out["notes"] = [{"mark": m, "text": self.notes.get(m, "")} for m in r["notes"]]
        return out


def load_from_env() -> Rates | None:
    path = os.environ.get("ETT_RATES_PATH", str(Path(__file__).parent / "data" / "eec_rates.json"))
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return Rates(json.load(f))
