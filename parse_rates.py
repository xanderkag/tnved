"""
Ставки ввозной пошлины ЕТТ ЕАЭС из PDF ЕЭК (PAY-1) → data/eec_rates.json — сверка со ставками TWS.BY.

PDF те же, что у parse_notes.py: ru.NN_2022_<дата>.pdf со страницы «ТН ВЭД ЕАЭС и ЕТТ ЕАЭС».
Таблица в PDF без линий, поэтому колонки — по координатам шапки: «Доп. ед. изм.» и правее неё ставка.
Строка товара начинается с кода в левой колонке; ставка может переноситься на следующие строки —
слова колонки ставки до следующего кода относятся к последнему коду.

Сноски ЕТТ стоят у ставки верхним индексом («5⁶³ᶜ⁾»): льготная ставка на срок или своя ставка при ввозе
в Россию. Их номера — в поле notes, тексты — из PDF «Примечания к ЕТТ_<дата>.pdf» (берётся самый свежий).

    python parse_rates.py data/raw/eec_pdf data/eec_rates.json

eec_rates.json: {"rates": {"0106410001": {"unit": "шт", "rate": "5", "notes": ["63С"], "source": "ru.01_….pdf"}},
                 "notes": {"63С": "Ставка … 0 (ноль) % … применяется с 28.03.2022 по 30.09.2022 включительно."},
                 "notes_source": "Примечания к ЕТТ_01.11.2026.pdf", "fetched": "30.10.2026"}
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sys

import pdfplumber
import pypdf

CODE_PART = re.compile(r"^\d{1,4}$")
ROW_TOL = 3  # слова одной строки PDF расходятся по высоте на пару пунктов
SUPERSCRIPT = 10  # кегль сноски — 8,5 против 13 у текста
NOTE_MARK = re.compile(r"(\d+)[СC]\)")  # в части PDF «C» латинская
NOTE_ITEM = re.compile(r"^\s*(\d+)[СC]\)\s*", re.M)


def _columns(words: list[dict]) -> tuple[float, float, float, float] | None:
    """Шапка таблицы на странице: конец колонки кода, начало ед. изм., начало ставки, низ шапки."""
    for dop in (w for w in words if w["text"] == "Доп."):
        izm = next((w for w in words if w["text"] == "изм." and abs(w["x0"] - dop["x0"]) < 15
                    and 0 < w["top"] - dop["top"] < 60), None)
        vad = next((w for w in words if w["text"] == "ВЭД" and w["x0"] < 200
                    and abs(w["top"] - dop["top"]) < 40), None)
        if izm and vad:
            bottom = max(w["bottom"] for w in words if dop["top"] - 50 < w["top"] <= izm["top"] + 45)
            return vad["x1"] + 15, dop["x0"] - 5, izm["x1"] + 5, bottom
    return None


def _lines(words: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if out and abs(out[-1][0]["top"] - w["top"]) <= ROW_TOL + 3:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(ln, key=lambda w: w["x0"]) for ln in out]


def parse_pdf(path: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    cols = None
    current = None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(extra_attrs=["size"])
            head = _columns(words)
            if head is None and cols is None:  # примечания до первой шапки
                continue
            cols = head or cols
            code_x, unit_x, rate_x, _ = cols
            top = head[3] if head else 0  # шапка повторяется на каждой странице таблицы
            for ln in _lines([w for w in words if w["top"] > top]):
                code = "".join(w["text"] for w in ln if w["x1"] <= code_x and CODE_PART.match(w["text"]))
                if code and len(code) in (4, 6, 8, 10) and ln[0]["x1"] <= code_x:
                    current = code if len(code) == 10 else None
                    if current:
                        rows[current] = {"unit": "", "rate": "", "notes": [], "source": os.path.basename(path)}
                if not current:
                    continue
                r = rows[current]
                unit = " ".join(w["text"] for w in ln if unit_x <= w["x0"] < rate_x)
                r["unit"] = (r["unit"] + " " + unit).strip()
                for w in ln:
                    if w["x0"] < rate_x:
                        continue
                    if w["size"] < SUPERSCRIPT and NOTE_MARK.search(w["text"]):
                        r["notes"] += [n + "С" for n in NOTE_MARK.findall(w["text"])]
                    elif w["size"] < SUPERSCRIPT:  # степень в «см³», «м²» — к единице без пробела
                        r["rate"] += w["text"]
                    else:
                        r["rate"] = (r["rate"] + " " + w["text"]).strip()
    return rows


def ett_notes(pdf_dir: str) -> tuple[dict[str, str], str | None]:
    """Тексты сносок ЕТТ из самого свежего «Примечания к ЕТТ_ДД.ММ.ГГГГ.pdf»."""
    def date(p: str) -> str:
        d = re.search(r"(\d\d)\.(\d\d)\.(\d{4})", p)
        return f"{d.group(3)}{d.group(2)}{d.group(1)}" if d else ""
    files = sorted(glob.glob(os.path.join(pdf_dir, "Примечания к ЕТТ_*.pdf")), key=date)
    if not files:
        return {}, None
    text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(files[-1]).pages)
    parts = NOTE_ITEM.split(text)
    notes = {parts[i] + "С": re.sub(r"\s+", " ", parts[i + 1]).strip() for i in range(1, len(parts) - 1, 2)}
    return notes, os.path.basename(files[-1])


def parse(pdf_dir: str) -> dict:
    rates: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(pdf_dir, "ru.[0-9][0-9]_*.pdf"))):
        rates.update(parse_pdf(path))
    notes, source = ett_notes(pdf_dir)
    # Когда скачан набор PDF: страница ЕЭК ett.html лежит рядом (fetch — вручную, см. BACKLOG PAY-1).
    page = os.path.join(pdf_dir, "ett.html")
    stamp = os.path.getmtime(page) if os.path.exists(page) else max(os.path.getmtime(p) for p in glob.glob(
        os.path.join(pdf_dir, "*.pdf")))
    fetched = datetime.date.fromtimestamp(stamp).strftime("%d.%m.%Y")
    return {"rates": rates, "notes": notes, "notes_source": source, "fetched": fetched}


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "data/raw/eec_pdf"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/eec_rates.json"
    out = parse(src)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    rates = out["rates"]
    print(f"{dst}: кодов {len(rates)}, без ставки {sum(not r['rate'] for r in rates.values())}, "
          f"со сносками {sum(bool(r['notes']) for r in rates.values())}, сносок ЕТТ {len(out['notes'])}")
