"""
Примечания к разделам и группам (DATA-3) — для шага выбора позиции навигатора (ALG-6).

Брокер, выбирая между позициями из разных групп, первым делом читает примечания: «в данную
группу не включаются: … смеси с пищевыми продуктами (как правило, товарная позиция 2106)».
Модели весь текст не нужен — 337 тыс. символов. Здесь примечания режутся на пункты, а для
набора позиций-кандидатов отбираются пункты из примечаний их групп и разделов, которые
называют кандидата из другой группы, другую группу или раздел из того же набора. Это и есть
развилка между группами, которую пункт решает. Пункты про субпозиции и дополнительные
примечания ЕАЭС здесь не нужны — они про ступени внутри позиции.

Пункт — подпункт исключения с его вводной фразой («Группа 38, примечание 1 (б): В данную группу
не включаются: смеси…») или нумерованное примечание целиком. Упоминания — номера позиций
(4 цифры, если в пункте есть «позици»), групп («группа 39», «группы 84 и 85») и разделов
(«раздел XV»). Диапазон «8401 – 8479» разворачивается.

NAV_NOTES_PATH — data/notes.json (собирает parse_notes.py); нет файла — примечания не подаются.
NAV_NOTES_CHARS — сколько символов пунктов отдавать модели (2500).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NOTES_CHARS = int(os.environ.get("NAV_NOTES_CHARS", "2500"))

_ITEM = re.compile(r"^(\d{1,2})\.\s", re.M)
_SUB = re.compile(r"^\(([а-яёА-ЯЁ])\)\s", re.M)
_HEADING = re.compile(r"(?<![\d.,])(\d{4})(?:\s*[–-]\s*(\d{4}))?(?![\d.,]\d)")
_GROUPS = re.compile(r"групп[а-яё]*\s+((?:\d{2}(?:\s*(?:,|и|или|–|-)\s*)?)+)", re.I)
_SECTIONS = re.compile(r"раздел[а-яё]*\s+([IVXL]+)\b")


@dataclass
class Clause:
    scope: str  # «38» — группа, «XV» — раздел
    ref: str  # «Группа 38, примечание 1 (б)»
    text: str
    headings: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    sections: set[str] = field(default_factory=set)


def _mentions(c: Clause, valid: set[str]) -> Clause:
    if "позици" in c.text:
        for a, b in _HEADING.findall(c.text):
            if b and a <= b and a[:2] == b[:2]:
                c.headings |= {h for h in valid if a <= h <= b}
            elif a in valid:
                c.headings.add(a)
    for m in _GROUPS.finditer(c.text):
        nums = re.findall(r"\d{2}", m.group(1))
        span = re.search(r"(\d{2})\s*[–-]\s*(\d{2})", m.group(1))
        if span:
            nums += [f"{i:02d}" for i in range(int(span.group(1)), int(span.group(2)) + 1)]
        c.groups |= set(nums)
    c.sections |= set(_SECTIONS.findall(c.text))
    return c


def split(scope: str, label: str, text: str, valid: set[str]) -> list[Clause]:
    """Пункты примечаний одной группы или раздела."""
    out: list[Clause] = []
    # Дополнительные примечания ЕАЭС и примечания к субпозициям — про ступени внутри позиции,
    # на выборе позиции они не нужны; нумерация в них начинается заново.
    text = re.split(r"\n(?:Дополнительн\w*\s+примечани|Примечани[ея]\s+к\s+субпозици)", text)[0]
    starts = [m.start() for m in _ITEM.finditer(text)]
    for i, s in enumerate(starts):
        item = text[s:starts[i + 1] if i + 1 < len(starts) else len(text)].strip()
        num = item.split(".", 1)[0]
        subs = [m for m in _SUB.finditer(item)]
        lead = item[:subs[0].start()].strip() if subs else item
        if subs and lead.rstrip().endswith(":"):
            for j, m in enumerate(subs):
                body = item[m.start():subs[j + 1].start() if j + 1 < len(subs) else len(item)].strip()
                out.append(_mentions(Clause(scope, f"{label}, примечание {num} ({m.group(1)})",
                                            f"{lead[len(num) + 1:].strip()} {body}"), valid))
        else:
            out.append(_mentions(Clause(scope, f"{label}, примечание {num}", item[len(num) + 1:].strip()), valid))
    return out


class Notes:
    def __init__(self, data: dict, valid: set[str]):
        self.section_of = {g: v["section"] for g, v in data["groups"].items()}
        self.clauses: list[Clause] = []
        for s, v in data["sections"].items():
            if v["text"]:
                self.clauses += split(s, f"Раздел {s}", v["text"], valid)
        for g, v in data["groups"].items():
            if v["text"]:
                self.clauses += split(g, f"Группа {g}", v["text"], valid)

    def relevant(self, headings: list[str], limit: int = NOTES_CHARS) -> list[Clause]:
        """Пункты из примечаний групп и разделов кандидатов, называющие другого кандидата:
        позицию, его группу или раздел. Сначала — называющие больше кандидатов."""
        heads = set(headings)
        groups = {h[:2] for h in heads}
        sections = {self.section_of.get(g) for g in groups} - {None}
        scored = []
        for c in self.clauses:
            own = {c.scope} if c.scope in groups else {g for g in groups if self.section_of.get(g) == c.scope}
            if not own:
                continue
            if "субпозици" in c.text:  # определения для ступеней внутри позиции
                continue
            others = groups - own
            hit = len({h for h in c.headings & heads if h[:2] in others}) + len(c.groups & others) \
                + len(c.sections & ({self.section_of[g] for g in others} - {c.scope}))
            if hit:
                scored.append((-hit, len(c.text), c))
        out, used = [], 0
        for _, n, c in sorted(scored, key=lambda t: (t[0], t[1])):
            if used + n > limit:
                continue
            out.append(c)
            used += n
        return out


def text(clauses: list[Clause]) -> str:
    return "\n".join(f"— {c.ref}: {c.text}" for c in clauses)


def load_from_env(valid: set[str]) -> Notes | None:
    path = os.environ.get("NAV_NOTES_PATH", str(Path(__file__).parent / "data" / "notes.json"))
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return Notes(json.load(f), valid)
