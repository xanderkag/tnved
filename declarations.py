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

Второй ход уровня — по описанию: артикула нет или по нему код не однозначен, а описание
совпадает с описанием из ДТ дословно (после нормализации) или близко по векторам bge-m3 (наш
сервер, как и весь поиск). Код отдаём, только если все описания ДТ выше порога
DECLARATIONS_NEAR_MIN (по умолчанию 0.93, уточнить замером) оформлены одним действующим
кодом. Разошлись — код не выбираем, строка идёт в модель с проверкой. Описание из одного
слова или только обозначение — по описанию не ищем: слишком общее.

Третий ход — код не выбран: ближайшие описания ДТ (выше DECLARATIONS_EXAMPLE_MIN, до
DECLARATIONS_EXAMPLES штук) с действующими кодами уходят модели примерами, а их коды — в
кандидаты: модель видит, как похожие товары уже оформляли, но решает сама.
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

import numpy as np

log = logging.getLogger("tnved.declarations")

REQUIRED = ("article", "description", "hs_code", "dt_number", "dt_date")

# Кириллица, похожая на латиницу: в артикулах их путают при наборе («С» и «C»).
_LOOKALIKE = str.maketrans("АВЕКМНОРСТХУ", "ABEKMHOPCTXY")
_SEPARATORS = re.compile(r"[\s\-‐‑–—._/\\]+")
# Не артикулы (уже без разделителей и с латиницей вместо двойников): «б/н», «нет», «n/a», нули.
_NOT_ARTICLE = re.compile(r"^(?:БH|BH|HET|NONE|NA|0+)$")
MIN_ARTICLE = 3
NEAR_MIN_DEFAULT = 0.93
NEAR_TOP_K = 5
EXAMPLES_DEFAULT = 5
EXAMPLE_MIN_DEFAULT = 0.80
MIN_DESCRIPTION_WORDS = 2
_WORDS = re.compile(r"\w+")


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


def norm_description(raw: object) -> str:
    """Описание для сравнения: регистр и пробелы не важны; из одного слова — не ищем ("")."""
    s = " ".join(str(raw or "").split()).casefold()
    return s if len(_WORDS.findall(s)) >= MIN_DESCRIPTION_WORDS else ""


def _threshold_from_env(name: str, default: float) -> float:
    """Порог сходства из env; задан и не число из (0, 1] — отказ, а не тихое значение по умолчанию."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if not 0 < value <= 1:
        raise ValueError(f"{name}={raw}: нужно число из (0, 1]")
    return value


def near_min_from_env() -> float:
    return _threshold_from_env("DECLARATIONS_NEAR_MIN", NEAR_MIN_DEFAULT)


def settings_from_env() -> dict:
    """Пороги и число примеров; неприменимое значение — ValueError, сервис не стартует."""
    raw = os.environ.get("DECLARATIONS_EXAMPLES", "").strip()
    examples_k = int(raw) if raw else EXAMPLES_DEFAULT
    if not 0 <= examples_k <= 20:
        raise ValueError(f"DECLARATIONS_EXAMPLES={raw}: нужно целое от 0 до 20")
    return {"near_min": near_min_from_env(), "examples_k": examples_k,
            "example_min": _threshold_from_env("DECLARATIONS_EXAMPLE_MIN", EXAMPLE_MIN_DEFAULT)}


@dataclass
class _Article:
    codes: Counter = field(default_factory=Counter)
    refs: dict = field(default_factory=lambda: defaultdict(list))  # код → [(номер ДТ, дата)]


class DeclarationIndex:
    """Свод «артикул → коды из ДТ» по выгрузке."""

    def __init__(self, rows: list[dict], near_min: float = NEAR_MIN_DEFAULT,
                 examples_k: int = EXAMPLES_DEFAULT, example_min: float = EXAMPLE_MIN_DEFAULT):
        self.by_article: dict[str, _Article] = defaultdict(_Article)
        self.by_description: dict[str, _Article] = defaultdict(_Article)
        self.rows = 0
        self.skipped = Counter()
        self.near_min = near_min
        self.examples_k = examples_k
        self.example_min = example_min
        self._last_query: tuple[str, np.ndarray] | None = None  # near и examples по одной строке — один запрос
        self.desc_keys: list[str] = []
        self.desc_vecs: np.ndarray | None = None  # build_vectors; None — по описанию только дословно
        for row in rows:
            code = _digits(row.get("hs_code"))
            if len(code) != 10:
                self.skipped["код не 10 знаков"] += 1
                continue
            ref = (str(row.get("dt_number") or "").strip(), str(row.get("dt_date") or "").strip())
            desc = norm_description(row.get("description"))
            if desc:  # по описанию ищем и строки без артикула
                _add(self.by_description[desc], code, ref)
            art = norm_article(row.get("article"))
            if not art:
                self.skipped["нет артикула"] += 1
                continue
            _add(self.by_article[art], code, ref)
            self.rows += 1
        self.desc_keys = list(self.by_description)

    @classmethod
    def load(cls, path: str | Path, **settings) -> "DeclarationIndex":
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
        index = cls(rows, **settings)
        log.info("[decl] %s: строк %d, артикулов %d, описаний %d, пропущено %s",
                 path.name, index.rows, len(index.by_article), len(index.desc_keys), dict(index.skipped) or 0)
        return index

    def build_vectors(self, embedder) -> None:
        """Векторы описаний ДТ — тем же сервером, что поиск по тарифу (внутренний bge-m3)."""
        if embedder is None or not self.desc_keys:
            return
        self.desc_vecs = embedder.encode(self.desc_keys)
        log.info("[decl] векторы описаний: %d", len(self.desc_keys))

    def near(self, description: object, store, embedder=None) -> dict | None:
        """Код по описанию: дословно или по векторам выше порога, если все такие описания — один код.

        {"code", "similar", "sim", "rows", "dt_refs", "retired_codes"} — код; {"miss", "codes",
        "similar", "sim"} — похожие описания в ДТ есть, но коды разные или сняты; None — похожих нет.
        """
        desc = norm_description(description)
        if not desc:
            return None
        if desc in self.by_description:
            hits = [(desc, 1.0)]
        else:
            hits = self._similar(desc, embedder, self.near_min, NEAR_TOP_K)
        if not hits:
            return None
        merged = _Article()
        for key, _ in hits:
            entry = self.by_description[key]
            for code, n in entry.codes.items():
                merged.codes[code] += n
                for ref in entry.refs[code]:
                    if ref not in merged.refs[code]:
                        merged.refs[code].append(ref)
        found = _pick(merged, store)
        found.update(similar=hits[0][0], sim=round(hits[0][1], 3))
        return found

    def examples(self, description: object, store, embedder=None) -> list[dict]:
        """Ближайшие описания ДТ с действующими кодами — примеры для модели.

        [{"description", "code", "rows", "sim"}]: такое же описание первым, дальше по сходству
        не ниже example_min; не больше examples_k. Снятые коды не показываем: их выбирать нельзя.
        """
        desc = norm_description(description)
        if not desc or not self.examples_k:
            return []
        hits = [(desc, 1.0)] if desc in self.by_description else []
        hits += [h for h in self._similar(desc, embedder, self.example_min, self.examples_k + 1) if h[0] != desc]
        out = []
        for key, sim in hits:
            for code, n in self.by_description[key].codes.most_common():
                if store.code_status(code)[0] == "current":
                    out.append({"description": key, "code": code, "rows": n, "sim": round(sim, 3)})
        return out[:self.examples_k]

    def _similar(self, desc: str, embedder, floor: float, k: int) -> list[tuple[str, float]]:
        """k ближайших описаний ДТ со сходством не ниже floor; векторов нет — пусто."""
        if self.desc_vecs is None or embedder is None:
            return []
        last = self._last_query
        if last is not None and last[0] == desc:
            q = last[1]
        else:
            q = embedder.encode([desc])[0]
            self._last_query = (desc, q)
        sims = self.desc_vecs @ q
        top = np.argsort(-sims)[:k]
        return [(self.desc_keys[i], float(sims[i])) for i in top if sims[i] >= floor]

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
        found = _pick(entry, store)
        found["article"] = art
        return found


def _add(entry: _Article, code: str, ref: tuple) -> None:
    entry.codes[code] += 1
    if ref not in entry.refs[code]:
        entry.refs[code].append(ref)


def _pick(entry: _Article, store) -> dict:
    """Один действующий код — {"code", "rows", "dt_refs", "retired_codes"}; иначе {"miss", "codes"}."""
    current, retired = [], []
    for code in entry.codes:
        status, _ = store.code_status(code)
        (current if status == "current" else retired).append(code)
    if len(current) != 1:
        reason = "в декларациях несколько действующих кодов" if current else "код из декларации снят"
        return {"miss": reason, "codes": sorted(entry.codes, key=lambda c: -entry.codes[c])}
    code = current[0]
    return {
        "code": code,
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
    return DeclarationIndex.load(path, **settings_from_env())


def batch_lookup(index: DeclarationIndex | None, store, article: str, description: str,
                 description_is_designation: bool, embedder=None) -> dict | None:
    """Уровень 1 для строки пакета: результат в форме пакета, проверки для модели или None.

    Сначала артикул (в колонке может быть несколько значений через «; »; если описание — одно
    обозначение, то и оно: в таких строках артикул стоит вместо названия). Однозначного кода
    по артикулу нет — описание (near). {"result": {...}} — код из ДТ, модель не нужна;
    {"checks": [...], "examples": [...]} — кода нет: решает модель, в промпт — ближайшие строки
    ДТ примерами, человек в «Проверить» видит, что было в ДТ.
    С embedder ходит в сервер векторов — из async-кода только через to_thread.
    """
    if index is None:
        return None
    keys = [a for a in (article or "").split("; ") if a.strip()]
    if description_is_designation:
        keys.append(description)
    miss = None
    for key in keys:
        found = index.lookup(key, store)
        if found is None:
            continue
        if "code" in found:
            return {"result": _result(found, store)}
        miss = miss or found
    checks = []
    if miss is not None:
        codes = ", ".join(miss["codes"][:5])
        if miss["miss"] == "код из декларации снят":
            checks.append(f"В декларациях по артикулу {miss['article']} — код {codes}, он снят из тарифа: нужен новый код.")
        else:
            checks.append(f"В декларациях по артикулу {miss['article']} — разные коды: {codes}; выбрать по описанию.")
    near = None if description_is_designation else index.near(description, store, embedder)
    if near is not None and "code" in near:
        result = _result(near, store)
        result["checks_required"][1:1] = checks
        return {"result": result}
    if near is not None:
        codes = ", ".join(near["codes"][:5])
        checks.append(f"Похожее описание в декларациях («{near['similar']}») оформлено кодами {codes}: сверить.")
    examples = [] if description_is_designation else index.examples(description, store, embedder)
    return {"checks": checks, "examples": examples} if checks or examples else None


def _result(found: dict, store) -> dict:
    code = found["code"]
    meta = store.code_to_meta.get(code, {})
    group = store.group_info(code[:2])
    refs = "; ".join(f"ДТ {n} от {d}" if d else f"ДТ {n}" for n, d in found["dt_refs"])
    if "article" in found:
        why = f"Артикул {found['article']} оформлен по этому коду"
        checks = ["Код из декларации по совпавшему артикулу: убедиться, что товар тот же."]
        confidence = "high"
    else:
        exact = found["sim"] >= 1.0
        why = f"Описание «{found['similar']}» " + ("" if exact else f"(сходство {found['sim']}) ") + "оформлено по этому коду"
        checks = ["Код из декларации по " + ("такому же" if exact else "похожему") + " описанию: сверить, что товар тот же."]
        # одна строка ДТ по похожему, а не такому же описанию — ещё не практика оформления
        confidence = "high" if exact or found["rows"] >= 2 else "medium"
    if found["retired_codes"]:
        checks.append(f"В прежних декларациях был и код {', '.join(found['retired_codes'])} — он снят из тарифа.")
    return {
        "group_code": code[:2],
        "group_name": group["description"] if group else "",
        "primary": {
            "code": code,
            "confidence": confidence,
            "reasoning": f"{why}: {refs} (строк в ДТ: {found['rows']}).",
            "full_path": meta.get("full_path"),
            "duty_rate": meta.get("duty_rate"),
        },
        "alternatives": [],
        "checks_required": checks,
        "source": f"декларации: {refs}",
    }
