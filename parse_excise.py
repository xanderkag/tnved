"""
Ставки акциза по НК РФ ст. 193 п. 1 (PAY-1) → data/excise.json.

При ввозе в Россию акциз платят по тем же ставкам ст. 193, что и при производстве (ст. 187, 194 НК).
Источник — сводный текст статьи с pravo.ppt.ru («Действующая редакция от …»): официальный сводный
текст в открытом доступе в разметке, пригодной для разбора, не нашёлся. Страница сохраняется
в data/raw/excise/ вручную, как перечни НДС и маркировки.

Пункт 1 статьи — список «вид товара: … с 1 января по 31 декабря 2026 года включительно - ставка».
Вид бывает с подвидами («для алкогольной продукции с объемной долей … до 18 процентов включительно:»),
ставка бывает в той же строке («авиационный керосин с 1 января 2026 года по … - 2800 рублей за 1 тонну»).
Здесь сохраняются все виды и все периоды; какой период действует сегодня — решает excise.py.

    python parse_excise.py data/raw/excise data/excise.json

excise.json: {"categories": [{"name": "сигары", "rates": [{"from": "2026-01-01", "to": "2026-12-31",
                                                          "rate": "351 рубль за 1 штуку"}]}],
              "edition": "1 октября 2026", "fetched": "30.10.2026"}
"""

from __future__ import annotations

import datetime
import html
import json
import os
import re
import sys

FILE = "nk193.html"
_MONTHS = {m: i for i, m in enumerate(("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                                       "августа", "сентября", "октября", "ноября", "декабря"), 1)}
_D = r"(\d{1,2}) ([а-я]+)(?: (\d{4}))?"
# «с 1 января по 30 июня 2026 года включительно - 824 рубля …», «с 1 января 2026 года - 11 рублей …»,
# «… включительно определяется как отношение 165 рублей к объемной доле …» — без тире
PERIOD = re.compile(rf"\bс {_D}(?: года)?(?: по {_D} года включительно)?(?: - | (?=определяется ))(.+?);?$")


def _iso(day: str, month: str, year: str | None) -> str | None:
    if not year or month not in _MONTHS:
        return None
    return datetime.date(int(year), _MONTHS[month], int(day)).isoformat()


def paragraphs(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        s = f.read()
    i = s.index("kodeks-text-container")
    body = s[i:s.index("article-activity-line", i)]
    out = (html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", p))).strip()
           for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S))
    return [p for p in out if p]


def parse(src: str) -> dict:
    path = os.path.join(src, FILE)
    paras = paragraphs(path)
    cats: list[dict] = []
    parent = ""
    for p in paras[1:]:
        if p.startswith("Ставка акциза, указанная"):  # пункт 1 кончился, дальше формулы для нефти, стали
            break
        if re.match(r"(?i)утратил", p):
            continue
        m = PERIOD.search(p)
        head = p[:m.start()].strip(" :") if m else p.rstrip(":")
        if head:
            name = f"{parent} — {head}" if head.startswith("для ") and parent else head
            if not head.startswith("для "):
                parent = head
            cats.append({"name": name, "rates": []})
        if m and cats:
            d1, m1, y1, d2, m2, y2, rate = m.groups()
            cats[-1]["rates"].append({"from": _iso(d1, m1, y1 or y2), "to": _iso(d2, m2, y2) if d2 else None,
                                      "rate": rate.strip()})
    cats = [c for c in cats if c["rates"]]
    with open(path, encoding="utf-8") as f:
        edition = re.search(r"Действующая редакция[^<]*?от (\d{1,2} [а-я]+ \d{4})", html.unescape(
            re.sub(r"<[^>]+>", " ", f.read())))
    return {"categories": cats, "edition": edition.group(1) if edition else "",
            "fetched": datetime.date.fromtimestamp(os.path.getmtime(path)).strftime("%d.%m.%Y")}


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "data/raw/excise"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/excise.json"
    out = parse(src)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"{dst}: видов {len(out['categories'])}, ред. {out['edition']}")
