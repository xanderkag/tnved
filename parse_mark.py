"""
Перечень товаров, подлежащих обязательной маркировке «Честный знак» (PAY-1) → data/marking.json.

Источник — распоряжение Правительства РФ от 28.04.2018 № 792-р, сводная редакция со справочника
alta.ru/tamdoc (18rs0792), как у перечней НДС 10 %: страница сохраняется в data/raw/mark/ вручную.
Блоки прежних редакций и комментарии на странице отбрасываются.

Таблица: № позиции, наименование, коды ТН ВЭД, коды ОКПД 2, срок введения маркировки, примечание.
Позиция бывает в несколько строк (разные коды — разные сроки), ячейки склеены rowspan, а строки
во всю ширину — заголовки разделов, «(п. … введен …)» и общие оговорки к позиции.

Код в перечне — ещё не маркировка: по сноске <*> товар определяют одновременно кодом ТН ВЭД и
кодом ОКПД 2, у части позиций — ещё наименованием или регистрацией. Поэтому сервис говорит
«подлежит, если …», с текстом позиции и сроком, а не «маркируется» сам.

    python parse_mark.py data/raw/mark data/marking.json

marking.json: {"act", "edition", "general", "entries": [{"item": "9", "name", "section", "subsection",
               "from": "0406000000", "to": "0406999999", "spec": "0406", "except": [], "partial": false,
               "okpd": ["10.51"], "start": "2021-06-01", "note": "за исключением …"}], "fetched"}
"""

from __future__ import annotations

import datetime
import html
import json
import os
import re
import sys
from html.parser import HTMLParser

FILE = "18rs0792.html"
ACT = "Распоряжение Правительства РФ от 28.04.2018 № 792-р"
_HIDE = {"old", "content-old", "new", "ordw-comment"}  # прежние редакции, ярлыки редакций, комментарии
# «2202 99 910 0», «2104 20 00» (восьмизначный в перечне тоже встречается), «2203 00», «0406»
_CODE = re.compile(r"\d{4}(?: \d{2}(?: \d{3}(?: \d)?| \d{2}(?!\d))?)?(?!\d)")
_MONTHS = {m: i for i, m in enumerate(("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                                       "августа", "сентября", "октября", "ноября", "декабря"), 1)}
_ADDED = re.compile(r"^\(п\. [\d.]+ введен|^;? ?Действует")
_EXCEPT_WORDS = re.compile(r"кроме|за исключением|кодов|кода|\bи\b|[,\s]")


class _Table(HTMLParser):
    """Строки таблицы перечня: список ячеек {t, rs, cs}; <br> — перевод строки."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[dict]] = []
        self.cell: dict | None = None
        self.divs: list[bool] = []
        self.spans: list[bool] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            hide = a.get("class", "") in _HIDE
            self.divs.append(hide)
            self.hidden += hide
        if self.hidden:
            return
        if tag == "tr":
            self.rows.append([])
        if tag == "td" and self.rows:
            self.cell = {"t": "", "rs": int(a.get("rowspan", 1)), "cs": int(a.get("colspan", 1))}
            self.rows[-1].append(self.cell)
        if tag == "br" and self.cell is not None:
            self.cell["t"] += "\n"
        if tag == "span":  # номер позиции «11¹» — в тексте «11(1)», как пишет КонсультантПлюс
            sup = "super" in a.get("style", "")
            self.spans.append(sup)
            if sup and self.cell is not None:
                self.cell["t"] += "("

    def handle_endtag(self, tag):
        if tag == "div" and self.divs:
            self.hidden -= self.divs.pop()
        if tag == "span" and self.spans and self.spans.pop() and self.cell is not None and not self.hidden:
            self.cell["t"] += ")"

    def handle_data(self, data):
        if self.cell is not None and not self.hidden:
            self.cell["t"] += data


def _clean(t: str) -> str:
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in t.replace("\xa0", " ").split("\n"))
    return "\n".join(ln for ln in lines if ln)


def grid(path: str) -> list[list[str]]:
    """Строки по 6 колонок с раскрытым rowspan; строка во всю ширину — список из одной ячейки."""
    p = _Table()
    with open(path, encoding="utf-8") as f:
        p.feed(f.read())
    out: list[list[str]] = []
    carry: dict[int, list] = {}
    for r in p.rows:
        if not r:
            continue
        if len(r) == 1:
            for v in carry.values():  # строка во всю ширину — тоже строка под rowspan соседних ячеек
                v[1] -= 1
            out.append([_clean(r[0]["t"]).replace("\n", " ")])
            continue
        row, cells = [""] * 6, list(r)
        for col in range(6):
            if carry.get(col, [None, 0])[1] > 0:
                row[col] = carry[col][0]
                carry[col][1] -= 1
            elif cells:
                c = cells.pop(0)
                row[col] = _clean(c["t"])
                if c["rs"] > 1:
                    carry[col] = [row[col], c["rs"] - 1]
        out.append(row)
    return out


def _date(s: str) -> str | None:
    m = re.search(r"(\d{1,2}) ([а-я]+) (\d{4})", s)
    if not m or m.group(2) not in _MONTHS:
        return None
    return datetime.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))).isoformat()


def _digits(code: str, fill: str) -> str:
    d = code.replace(" ", "")
    return d + fill * (10 - len(d))


def codes(cell: str) -> list[dict]:
    """«2007 (кроме компотов …)», «1905 (за исключением 1905 10 000 0, 1905 40) 2006 00» → коды.
    Скобка после кода относится к нему: одни коды внутри — исключённые подсубпозиции, слова —
    оговорка «в части …», и код тогда частичный: решает товар, а не код."""
    s = cell.replace("\n", " ")
    out: list[dict] = []
    i = 0
    while i < len(s):
        if s[i] == "(":
            depth, j = 0, i
            while j < len(s):
                depth += (s[j] == "(") - (s[j] == ")")
                j += 1
                if depth == 0:
                    break
            inner = s[i + 1:j - 1]
            if out:
                found = _CODE.findall(inner)
                if found and not _EXCEPT_WORDS.sub("", _CODE.sub("", inner)):
                    out[-1]["except"] += [c.replace(" ", "") for c in found]
                else:
                    out[-1]["partial"] = True
                out[-1]["spec"] += f" ({inner.strip()})"
            i = j
            continue
        m = _CODE.match(s, i)
        if m and (i == 0 or not s[i - 1].isdigit()):
            out.append({"from": _digits(m.group(), "0"), "to": _digits(m.group(), "9"), "spec": m.group(),
                        "except": [], "partial": False})
            i = m.end()
            continue
        i += 1
    return out


def parse(src: str) -> dict:
    path = os.path.join(src, FILE)
    rows = grid(path)
    start = next(i for i, r in enumerate(rows) if "Наименование товара" in r)
    section = subsection = ""
    item = name = date = ""
    last_codes: list[dict] = []
    entries: list[dict] = []
    for r in rows[start + 1:]:
        if len(r) == 1:
            t = r[0]
            if not t or _ADDED.match(t) or t.startswith("<*>"):
                continue
            if re.match(r"[IVX]+\. ", t):
                section, subsection = t, ""
            elif re.match(r"(?:за исключением|кроме|Код ТН ВЭД)", t, re.I):
                for e in entries:  # оговорка во всю ширину — к позиции над ней
                    if e["item"] == item:
                        e["note"] = (e["note"] + "\n" + t).strip()
            else:
                subsection = re.sub(r"\s*\((?:подраздел )?введен[оа]? .*\)$", "", t)
            continue
        num, nm, cd, okpd, srok, note = r
        if num or nm:  # новая позиция; пустые — продолжение прежней строкой ниже
            item, name = num.rstrip("."), nm
            last_codes = []
        last_codes = codes(cd) if cd else last_codes  # пустая ячейка кодов — те же коды, другой срок
        date = _date(srok) or date
        for c in last_codes:
            entries.append({"item": item, "name": name, "section": section, "subsection": subsection, **c,
                            "okpd": okpd.split("\n") if okpd else [], "start": date,
                            "note": note if not re.fullmatch(r"[\d.]*", note) else ""})
    text = html.unescape(re.sub(r"<[^>]+>", "", open(path, encoding="utf-8").read()))
    head = re.search(r"\(в ред\. распоряжений Правительства РФ от ([^)]*)\)", text.split("ОТДЕЛЬНЫХ ТОВАРОВ")[1])
    edition = re.findall(r"от (\d\d\.\d\d\.\d{4}) N (\S+)", "от " + head.group(1)) if head else []
    general = re.search(r"<\*>\s*(При определении товаров[^.]*\.)", text)
    return {"act": ACT, "edition": " ".join(f"{d} № {n}" for d, n in edition[-1:]),
            "general": re.sub(r"\s+", " ", general.group(1)).strip() if general else "",
            "entries": entries,
            "fetched": datetime.date.fromtimestamp(os.path.getmtime(path)).strftime("%d.%m.%Y")}


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "data/raw/mark"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/marking.json"
    out = parse(src)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    e = out["entries"]
    print(f"{dst}: позиций {len({x['item'] for x in e})}, строк кодов {len(e)}, частичных "
          f"{sum(x['partial'] for x in e)}, без срока {sum(not x['start'] for x in e)}, ред. {out['edition']}")
