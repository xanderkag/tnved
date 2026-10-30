"""
Примечания к разделам и группам ТН ВЭД ЕАЭС из PDF ЕЭК (DATA-3) → data/notes.json.

PDF — по одному на группу, со страницы «ТН ВЭД ЕАЭС и ЕТТ ЕАЭС» (eec.eaeunion.org, catr/ett):
ru.NN_2022_<дата поправки>.pdf. Примечания стоят в начале PDF, до шапки таблицы «Код ТН ВЭД
Наименование позиции». В PDF первой группы раздела перед ними — «РАЗДЕЛ …» со своими примечаниями.

    python parse_notes.py data/raw/eec_pdf            # → data/notes.json

notes.json: {"sections": {"VI": {"title", "groups": ["28", …], "text"}},
             "groups": {"38": {"title", "section", "text", "source"}}}
Текст — как в PDF, переносы строк склеены. Разбор на пункты и правила — notes.py.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

import pypdf

HEADER = re.compile(r"Код\s*ТН\s*ВЭД\s+Наименование\s+позиции")
SECTION = re.compile(r"^\s*РАЗДЕЛ\s+([IVXL]+)\s*$", re.M)
GROUP = re.compile(r"^\s*ГРУППА\s+(\d{2})\s*$", re.M)


def _clean(text: str) -> str:
    """Склеить строки PDF: перенос внутри фразы — пробел, новый пункт — с новой строки."""
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln:
            continue
        starts_item = re.match(r"(\d{1,2}\.|\([а-яА-ЯёЁ0-9]{1,2}\)|Примечани|Дополнительн)", ln)
        if out and not starts_item:
            out[-1] += " " + ln
        else:
            out.append(ln)
    text = "\n".join(out)
    text = re.sub(r"(\d)\s{2,}", r"\1 ", text)  # «примечания 3  (а)» → «3 (а)»
    return re.sub(r"[ \t]+", " ", text).strip()


def head_text(path: str) -> str:
    """Текст PDF до шапки таблицы: заголовки и примечания."""
    parts = []
    for page in pypdf.PdfReader(path).pages:
        t = page.extract_text() or ""
        m = HEADER.search(t)
        parts.append(t[:m.start()] if m else t)
        if m:
            break
    return "\n".join(parts)


def parse(pdf_dir: str) -> dict:
    sections: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    current = None
    files = sorted(glob.glob(os.path.join(pdf_dir, "ru.[0-9][0-9]_*.pdf")))
    for path in files:
        g = os.path.basename(path)[3:5]
        raw = head_text(path)
        gm = GROUP.search(raw)
        if gm is None or gm.group(1) != g:
            raise SystemExit(f"{path}: нет заголовка «ГРУППА {g}»")
        sm = SECTION.search(raw[:gm.start()])
        if sm:
            current = sm.group(1)
            body = raw[sm.end():gm.start()]
            title, _, text = body.partition("Примечани")
            sections[current] = {"title": _clean(title).replace("\n", " "), "groups": [],
                                 "text": _clean("Примечани" + text) if text else ""}
        if current is None:
            raise SystemExit(f"{path}: группа до первого раздела")
        sections[current]["groups"].append(g)
        body = raw[gm.end():]
        title, _, text = body.partition("Примечани")
        groups[g] = {"title": _clean(title).replace("\n", " "), "section": current,
                     "text": _clean("Примечани" + text) if text else "", "source": os.path.basename(path)}
    return {"sections": sections, "groups": groups}


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "data/raw/eec_pdf"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/notes.json"
    notes = parse(src)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=1)
    with_notes = sum(bool(g["text"]) for g in notes["groups"].values())
    print(f"{dst}: разделов {len(notes['sections'])}, групп {len(notes['groups'])}, "
          f"с примечаниями {with_notes}, символов {sum(len(g['text']) for g in notes['groups'].values()):,}")
