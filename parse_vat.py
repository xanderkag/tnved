"""
Перечни кодов ТН ВЭД ЕАЭС, ввоз которых в Россию облагается НДС 10 % (PAY-1) → data/vat10.json.

Ставка НДС при ввозе — 22 % (НК РФ ст. 164 п. 3, с 01.01.2026 по закону от 28.11.2025 № 425-ФЗ),
10 % — по трём перечням Правительства (ст. 164 п. 2):

- продовольственные товары — ПП РФ от 31.12.2004 № 908, перечень «при ввозе»;
- товары для детей — то же постановление, второй перечень «при ввозе»;
- медицинские товары — ПП РФ от 15.09.2008 № 688, перечень «при их ввозе».

Код в перечне — ещё не ставка: в продовольственном перечне коды действуют только для пищевых и
кормовых целей, детские позиции все «из …» — нужен детский размер или документ по ТР ТС 007/2011,
медицинские — регистрация лекарства или медизделия, а позиции со «*» — ещё и по наименованию.
Поэтому сервис говорит «10 %, если …», с текстом позиции и названием перечня, а не ставит 10 % сам.

Источник — сводные редакции постановлений со справочника alta.ru/tamdoc (04ps0908, 08ps0688):
официальный сайт сводных текстов не даёт. Страницы сохраняются в data/raw/vat/ вручную, как PDF ЕЭК;
блоки «См. пред. ред.» (прежние редакции) и комментарии на странице отбрасываются.

    python parse_vat.py data/raw/vat data/vat10.json

vat10.json: {"lists": {"food": {"title", "act", "edition", "notes"}, …},
             "entries": [{"list": "food", "from": "0201100000", "to": "0201100000", "spec": "0201 10 000 (кроме телятины)",
                          "name": "Мясо крупного рогатого скота, свежее или охлажденное",
                          "group": "Мясо и мясопродукты (за исключением деликатесных: …)", "partial": true, "except": []}],
             "fetched": "30.10.2026"}
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from html.parser import HTMLParser

# Перечень: файл, где начинается (заголовок), где кончается (первая строка примечаний), порядок колонок.
LISTS = {
    "food": {"file": "04ps0908.html", "title": "Продовольственные товары",
             "act": "ПП РФ от 31.12.2004 № 908, перечень продовольственных товаров при ввозе",
             "start": "ПЕРЕЧЕНЬ\nКОДОВ ВИДОВ ПРОДОВОЛЬСТВЕННЫХ ТОВАРОВ В СООТВЕТСТВИИ\nС ЕДИНОЙ",
             "name_first": True},
    "kids": {"file": "04ps0908.html", "title": "Товары для детей",
             "act": "ПП РФ от 31.12.2004 № 908, перечень товаров для детей при ввозе",
             "start": "ПЕРЕЧЕНЬ\nКОДОВ ВИДОВ ТОВАРОВ ДЛЯ ДЕТЕЙ В СООТВЕТСТВИИ С ЕДИНОЙ",
             "name_first": False},
    "med": {"file": "08ps0688.html", "title": "Медицинские товары",
            "act": "ПП РФ от 15.09.2008 № 688, перечень медицинских товаров при ввозе",
            "start": "ПЕРЕЧЕНЬ\nКОДОВ МЕДИЦИНСКИХ ТОВАРОВ В СООТВЕТСТВИИ\nС ЕДИНОЙ",
            "name_first": True},
}

_HIDE = {"old", "content-old", "new", "ordw-comment"}  # прежние редакции, ярлыки редакций, комментарии
_CODE = r"\d{4}(?:\s\d{2})?(?:\s\d{3})?(?:\s\d)?"
ROW = re.compile(rf"^\s*(из\s+)?({_CODE})(?:\s*-\s*({_CODE}))?\s*(.*)$")
_SERVICE = re.compile(r"^\s*\((?:в ред|введен|утратил)|^\s*(?:позиция|код|абзац) (?:исключен|утратил)"
                      r"|^\s*(?:редакция )?распространяется на|^\s*\d\d\.\d\d\.\d{4} N \d+\s*$", re.I)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.divs: list[bool] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            hide = dict(attrs).get("class", "") in _HIDE
            self.divs.append(hide)
            self.hidden += hide
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "tr", "br", "td", "li", "pre"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "div" and self.divs:
            self.hidden -= self.divs.pop()
        if tag in ("script", "style"):
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.out.append(data)


def text(path: str) -> str:
    """Текст страницы; отступ в начале строки сохраняется — в <pre> им отличают заголовок раздела."""
    p = _Text()
    with open(path, encoding="utf-8") as f:
        p.feed(f.read())
    lines = []
    for ln in "".join(p.out).replace("\xa0", " ").replace("\t", "    ").split("\n"):
        if ln.strip():
            lines.append(" " * (len(ln) - len(ln.lstrip(" "))) + re.sub(r"\s+", " ", ln.strip()))
    return "\n".join(lines)


def _digits(code: str, fill: str) -> str:
    d = code.replace(" ", "")
    return d + fill * (10 - len(d))


def _section(t: str, start: str) -> tuple[str, str, str]:
    """Тело перечня, его примечания и дата последней редакции из шапки «(в ред. … от ДД.ММ.ГГГГ N …)»."""
    i = t.index(start)
    head = t[i:t.index("\n", t.index(")", t.index("(в ред.", i)))]
    edition = re.findall(r"от (\d\d\.\d\d\.\d{4}) N", head)
    body = t[i + len(head):]
    nxt = body.find("\nПЕРЕЧЕНЬ")
    body = body[:nxt] if nxt >= 0 else body
    j = body.find("Примечани")
    notes = body[j:] if j >= 0 else ""
    notes = notes[:notes.find("Комментарии")] if "Комментарии" in notes else notes
    return (body[:j] if j >= 0 else body), notes.strip(), max(edition, key=lambda d: d[6:] + d[3:5] + d[:2])


def _split_codes(ln: str) -> list[str]:
    """«0303 11 000 0, 0303 12 000 0» → два кода; запятые в скобках («(кроме 6402 12, …)») не трогаются."""
    if not ROW.match(ln):
        return [ln]
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(ln):
        depth += (ch in "([") - (ch in ")]")
        if ch == "," and depth == 0 and re.match(r"\s*(?:из\s+)?\d{4}(?:\s\d|\s*$|\s*,)", ln[i + 1:]):
            parts.append(ln[start:i].strip())
            start = i + 1
    return parts + [ln[start:].strip()]


def entries(body: str, key: str, name_first: bool) -> list[dict]:
    """Позиции перечня. В продовольственном и медицинском перечнях наименование стоит перед кодами,
    в детском — после кода. Строка с отступом в <pre> продовольственного перечня — заголовок раздела
    («Мясо и мясопродукты (за исключением деликатесных: …)») или перенос описания кода («(кроме белуги,»)."""
    lines = []
    for ln in body.split("\n"):
        if ln.strip() and not _SERVICE.match(ln):
            # «0303 11 000 0, 0303 12 000 0» — несколько кодов в строке
            for part in _split_codes(ln.strip()):
                lines.append((len(ln) - len(ln.lstrip()), part))
    out: list[dict] = []
    group, name = "", ""
    pending: list[dict] = []  # детский перечень: сначала код, потом наименование
    prev = "code"  # что было строкой выше: code, group, name
    i = 0
    while i < len(lines):
        ind, ln = lines[i]
        m = ROW.match(ln)
        if not m:
            cont = ln[0].islower() or ln[0] in "([" or (prev != "code" and (group if prev == "group" else name)
                                                         .endswith((",", "-", "(", ":")))
            if prev == "code" and cont and out:  # перенос описания кода, «(кроме 6402 12, …)»
                out[-1]["spec"] += " " + ln
                i += 1
                continue
            if pending:
                for e in pending:
                    e["name"] = ln
                pending = []
            elif re.match(r"[IVX]+\. ", ln) or (ind >= 2 and not cont):
                group, name, prev = ln, "", "group"
            elif cont and prev in ("group", "name"):
                if prev == "group":
                    group += " " + ln
                else:
                    name += " " + ln
            else:
                name, prev = ln, "name"
            i += 1
            continue
        tail = m.group(4)
        while tail.count("(") > tail.count(")") and i + 1 < len(lines):  # «(кроме белуги, бестера,» + след. строка
            i += 1
            tail += " " + lines[i][1]
        spec = f"{m.group(1) or ''}{m.group(2)}{' - ' + m.group(3) if m.group(3) else ''} {tail}"
        e = {"list": key, "from": _digits(m.group(2), "0"), "to": _digits(m.group(3) or m.group(2), "9"),
             "spec": re.sub(r"\s+", " ", spec).strip(" ,"), "name": name if name_first else "",
             "group": group if name_first else "",
             "partial": bool(m.group(1)) or "*" in tail or key == "kids"}
        out.append(e)
        if not name_first:
            pending.append(e)
        prev = "code"
        i += 1
    for e in out:
        # «из 6402 (кроме 6402 12, 6402 19 000 0)» — исключённые подсубпозиции: код под них не подпадает.
        # «(кроме телятины)» — исключение словами: решает товар, а не код.
        ex = re.search(r"\(кроме\s+((?:\d{4}(?:\s\d{1,3})*(?:,\s*)?)+)\)", e["spec"])
        e["except"] = [c.replace(" ", "") for c in re.split(r",\s*", ex.group(1).strip(", "))] if ex else []
        e["partial"] = e["partial"] or ("кроме" in e["spec"] and not ex)
    return out


def parse(src: str) -> dict:
    texts: dict[str, str] = {}
    lists, rows = {}, []
    for key, cfg in LISTS.items():
        t = texts.setdefault(cfg["file"], text(os.path.join(src, cfg["file"])))
        body, notes, edition = _section(t, cfg["start"])
        rows += entries(body, key, cfg["name_first"])
        lists[key] = {"title": cfg["title"], "act": cfg["act"], "edition": edition, "notes": notes}
    stamp = max(os.path.getmtime(os.path.join(src, c["file"])) for c in LISTS.values())
    return {"lists": lists, "entries": rows,
            "fetched": datetime.date.fromtimestamp(stamp).strftime("%d.%m.%Y")}


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "data/raw/vat"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/vat10.json"
    out = parse(src)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    for k, v in out["lists"].items():
        n = [e for e in out["entries"] if e["list"] == k]
        print(f"{k}: позиций {len(n)}, без наименования {sum(not e['name'] for e in n)}, ред. {v['edition']}")
