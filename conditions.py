"""
Числовые условия узлов (ALG-1) и величины из описания (ALG-2): ветку по числу выбирает код.

Тариф делит ветки диапазонами: «мощностью не более 30 кВт» / «более 30 кВт», «с содержанием
шерсти 85 мас.% или более». Если все ветки узла (кроме «прочих») — диапазоны одной величины,
а в описании ровно одно её значение, ветку выбирает правило, без модели и без вопроса.

Осторожно, потому что ошибка правила хуже вопроса:
- величины с одним смыслом (кВт, см³, кВА, г/м², дтекс) — по единице;
- масса, длина, объём, проценты — ещё и по признаку: слово перед числом в описании и в
  подписи ветки должны совпасть по началу («толщиной» ↔ «толщина», «шерсти» ↔ «шерсть»);
  слова самой величины («масса», «содержание») признаком не считаются, если в подписи
  есть точнее («массой пустого снаряженного аппарата» ≠ «взлетная масса»);
- два разных значения с одним признаком, пересечение диапазонов, ни одна ветка не подошла
  и «прочих» нет — правило молчит, решает модель.

Замер 29.10 (eval_conditions.py): самопроверка по дереву — 745 узлов с диапазонами,
929 срабатываний, 0 не в ту ветку; на путях 197 решений ЕЭК 13 узлов с условиями,
правило сработало на одном, верно (в длинных описаниях ЕЭК обычно несколько значений).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

# единица → (величина, множитель к базовой)
UNITS = {
    "квт": ("power", 1.0), "вт": ("power", 0.001), "мвт": ("power", 1000.0),
    "kw": ("power", 1.0), "w": ("power", 0.001),
    "л.с.": ("hp", 1.0), "л.с": ("hp", 1.0), "hp": ("hp", 1.0),
    "см³": ("disp", 1.0), "см3": ("disp", 1.0), "куб.см": ("disp", 1.0), "cm3": ("disp", 1.0), "cc": ("disp", 1.0),
    "мг": ("mass", 1e-6), "г": ("mass", 0.001), "гр": ("mass", 0.001), "кг": ("mass", 1.0), "т": ("mass", 1000.0),
    "g": ("mass", 0.001), "kg": ("mass", 1.0),
    "нм": ("length", 1e-6), "мкм": ("length", 0.001), "мм": ("length", 1.0), "см": ("length", 10.0),
    "м": ("length", 1000.0), "mm": ("length", 1.0), "cm": ("length", 10.0),
    "мл": ("volume", 0.001), "л": ("volume", 1.0), "м³": ("volume", 1000.0), "м3": ("volume", 1000.0),
    "ml": ("volume", 0.001),
    "г/м²": ("density", 1.0), "г/м2": ("density", 1.0), "гр/м2": ("density", 1.0), "гр/м²": ("density", 1.0),
    "gsm": ("density", 1.0),
    "мас.%": ("pct", 1.0), "%": ("pct", 1.0), "об.%": ("volpct", 1.0),
    "в": ("voltage", 1.0), "кв": ("voltage", 1000.0), "v": ("voltage", 1.0),
    "ква": ("kva", 1.0), "kva": ("kva", 1.0),
    "дтекс": ("dtex", 1.0), "текс": ("dtex", 10.0),
    "мпа": ("pressure", 1.0), "кпа": ("pressure", 0.001), "бар": ("pressure", 0.1),
}
# Величины, у которых одна единица — много смыслов: нужен ещё признак.
AMBIGUOUS = {"mass", "length", "volume", "pct", "volpct"}
# Однобуквенные и латинские единицы ловим только в описании, где они после числа.
SYNONYMS = {"вес": "масс", "весо": "масс", "толщ": "толщ", "диам": "диам"}

_NUM = r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,](\d+))?"
_UNIT = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
_U = rf"\s*({_UNIT})(?![а-яa-z0-9])"
# Слова самой величины: признаком служат, только если точнее в подписи ничего нет. Иначе
# «взлетная масса» описания совпала бы с «массой пустого снаряженного аппарата» подписи.
GENERIC = {"масс", "длин", "объе", "емко", "соде", "нетт", "брут", "разм"}
_STOPWORD = {"не", "более", "менее", "до", "от", "свыше", "около", "но", "и", "или", "с", "в", "на", "по", "x", "х"}


def _num(whole: str, frac: str | None) -> float:
    return float(whole.replace(" ", "").replace("\u00a0", "") + ("." + frac if frac else ""))


def _feature(text: str, end: int, n: int) -> tuple[str, ...]:
    """До n слов перед позицией end в пределах фразы (до запятой), без служебных: признак
    величины. «с массой пустого снаряженного аппарата более…» → аппарата, снаряженного, пустого, массой."""
    clause = re.split(r"[,;:]", re.sub(r"\([^()]*\)", " ", text[:end].lower()))[-1]
    words = [w.replace("ё", "е") for w in re.findall(r"[а-яёa-z][а-яёa-z-]*", clause)]
    return tuple(w for w in reversed(words) if w not in _STOPWORD and len(w) > 2)[:n]


def _stem(word: str) -> str:
    w = word.split("-")[-1]
    s = w[: min(4, max(len(w) - 1, 3))]
    return SYNONYMS.get(s, s)


@dataclass
class Cond:
    dim: str
    feature: tuple[str, ...]
    lo: float = -math.inf
    lo_inc: bool = False
    hi: float = math.inf
    hi_inc: bool = False

    def holds(self, v: float) -> bool:
        lo_ok = v > self.lo or (self.lo_inc and v == self.lo)
        hi_ok = v < self.hi or (self.hi_inc and v == self.hi)
        return lo_ok and hi_ok


_PATTERNS = [
    # более A u, но не более B u / более A, но менее B u / не менее A u, но не более B u
    (re.compile(rf"(не менее|более|свыше)\s+{_NUM}(?:{_U})?\s*,?\s*но\s+(не более|менее)\s+{_NUM}{_U}"), "range"),
    (re.compile(rf"{_NUM}{_U}\s+(?:или|и)\s+более"), "ge"),
    (re.compile(rf"{_NUM}{_U}\s+(?:или|и)\s+менее"), "le"),
    (re.compile(rf"(не более|не менее|более|менее|свыше)\s+{_NUM}{_U}"), "one"),
]


def parse_label(label: str) -> list[Cond]:
    """Числовые условия подписи ветки. Подпись с двумя условиями одной величины — не разбираем."""
    text = label.lower().replace("ё", "е")
    density = "1 м²" in text or "1 м2" in text  # «массой 1 м² более 150 г» — это г/м²
    out: list[Cond] = []
    taken: list[tuple[int, int]] = []
    for rx, kind in _PATTERNS:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            g = m.groups()
            k1 = None
            if kind == "range":
                op1, a, af, u1, op2, b, bf, u2 = g
                unit = u2
                k1 = UNITS[u1][1] if u1 and UNITS[u1][0] == UNITS[u2][0] else None
                c = Cond("", (), _num(a, af), op1 == "не менее", _num(b, bf), op2 == "не более")
            elif kind in ("ge", "le"):
                a, af, unit = g
                v = _num(a, af)
                c = Cond("", (), v, True) if kind == "ge" else Cond("", (), hi=v, hi_inc=True)
            else:
                op, a, af, unit = g
                v = _num(a, af)
                c = {"не более": Cond("", (), hi=v, hi_inc=True), "менее": Cond("", (), hi=v),
                     "более": Cond("", (), v), "свыше": Cond("", (), v),
                     "не менее": Cond("", (), v, True)}[op]
            dim, k = UNITS[unit]
            if density and dim == "mass":  # граммы на 1 м² — это г/м²
                dim, k, k1 = "density", k * 1000, None if k1 is None else k1 * 1000
            c.dim, c.feature = dim, _feature(text, m.start(), 4)
            c.lo, c.hi = c.lo * (k if k1 is None else k1), c.hi * k
            out.append(c)
            taken.append((m.start(), m.end()))
    dims = [c.dim for c in out]
    return [] if len(dims) != len(set(dims)) else out


_QTY = re.compile(rf"(?<![\d.,]){_NUM}\s*({_UNIT})(?![а-яa-z0-9])")


def quantities(text: str) -> list[tuple[str, float, tuple[str, ...]]]:
    """Величины описания: (величина, значение в базовой единице, слова перед числом)."""
    t = text.lower().replace("ё", "е")
    out = []
    for m in _QTY.finditer(t):
        whole, frac, unit = m.groups()
        v = _num(whole, frac)
        if unit == "г" and not frac and 1900 <= v <= 2100 and re.match(r"\.|од", t[m.end():]):
            continue  # «2020 г.», «2020 год» — год, а не граммы
        dim, k = UNITS[unit]
        out.append((dim, v * k, _feature(t, m.start(), 3)))
    return out


_OTHER = re.compile(r"^\s*проч", re.I)


def choose(labels: list[str], description: str) -> tuple[int, str] | None:
    """Индекс ветки по числу из описания и пояснение; None — правило не применимо."""
    conds = [None if _OTHER.match(l) else parse_label(l) for l in labels]
    named = [c for c in conds if c is not None]
    if not named or any(not c for c in named):
        return None
    common = set.intersection(*({x.dim for x in c} for c in named))
    qty = quantities(description)
    for dim in sorted(common):
        mine = [next(x for x in c if x.dim == dim) for c in named]
        if dim in AMBIGUOUS:  # «толщиной … мм» берёт из описания только значение при «толщина»
            feats = set.intersection(*({_stem(w) for w in x.feature} for x in mine))
            feats = feats - GENERIC or feats
            values = {round(v, 9) for d, v, f in qty if d == dim and feats & {_stem(w) for w in f}}
        else:
            values = {round(v, 9) for d, v, _ in qty if d == dim}
        if len(values) != 1:
            continue
        v = values.pop()
        hits = [i for i, c in enumerate(conds) if c is not None and next(x for x in c if x.dim == dim).holds(v)]
        others = [i for i, c in enumerate(conds) if c is None]
        if len(hits) == 1:
            return hits[0], f"{dim} = {v:g}"
        if not hits and len(others) == 1:
            return others[0], f"{dim} = {v:g}: ни один диапазон, значит «прочие»"
    return None
