"""
Навигатор (D-8, ALG-3): код ведёт товар по дереву ТН ВЭД, модель только выбирает ветку.

Дерево — ступени тарифа из full_path действующих кодов («насосы центробежные прочие →
насосы погружные → одноступенчатые»), а не пары цифр: развилки тарифа идут по тире и
часто режут одну 8-значную подсубпозицию. Корень — товарная позиция (4 знака).

На каждом узле с несколькими ветками канал решений (decisions.choose) выбирает ветку
и даёт вероятности. Уверенность не ниже порога — спускаемся сами; ниже — останавливаемся
и отдаём вопрос: ветки узла с вероятностями, человек выбирает кнопкой. Ответ — путь
(подписи веток по порядку), с него спуск продолжается. Код, на котором остановились,
честный: 4, 6 или 8 знаков, если дальше без человека не пройти.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from tnved_data import DB_PATH, TNVEDStore, is_current_leaf

SEP = "→"
NAV_THRESHOLD = float(os.environ.get("NAV_THRESHOLD", "0.8"))
_OTHER = re.compile(r"^\s*проч", re.I)
# Подпись ветки «прочие» сама по себе пуста: модель видит, от чего она «прочая».
OTHER_HINT = "прочие — не подходящие ни под один другой вариант этого уровня"
LABEL_LIMIT = 300


@dataclass
class Node:
    label: str
    codes: list[str] = field(default_factory=list)
    children: dict[str, "Node"] = field(default_factory=dict)
    own: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        """Общее начало кодов узла, до чётной длины: столько знаков известно."""
        p = os.path.commonprefix(self.codes)
        return p[: len(p) // 2 * 2]

    def options(self) -> list["Node"]:
        """Ветки узла. Код, путь которого кончается на узле с ветками («прочие» без
        своего «прочие» ниже), становится отдельной веткой «прочие»."""
        opts = list(self.children.values())
        if opts and self.own:
            label = "прочие" if "прочие" not in self.children else "прочие (остальные)"
            opts.append(Node(label=label, codes=list(self.own), own=list(self.own)))
        return opts


class Tree:
    """Корень → группы → позиции → ступени тарифа; только действующие 10-значные коды.
    Путь к узлу — подписи от корня: [группа, позиция, ступень, …]."""

    def __init__(self, store: TNVEDStore):
        with sqlite3.connect(DB_PATH) as conn:
            names = dict(conn.execute("SELECT code, description FROM codes WHERE length(code) IN (2, 4)"))
        self.top = Node(label="ТН ВЭД ЕАЭС")
        self.roots: dict[str, Node] = {}           # позиция → её узел
        self.heading_path: dict[str, list[str]] = {}
        for item in sorted(store.meta, key=lambda m: m["code"]):
            if not is_current_leaf(item):
                continue
            code = item["code"]
            h = code[:4]
            if h not in self.roots:
                glabel = f"{h[:2]} {(names.get(h[:2]) or '').strip()}".strip()
                hlabel = f"{h} {(names.get(h) or '').strip()}".strip()
                group = self.top.children.setdefault(glabel, Node(label=glabel))
                self.roots[h] = group.children[hlabel] = Node(label=hlabel)
                self.heading_path[h] = [glabel, hlabel]
            self.top.codes.append(code)
            self.top.children[self.heading_path[h][0]].codes.append(code)
            node = self.roots[h]
            node.codes.append(code)
            for seg in _levels(item)[2:]:
                node = node.children.setdefault(seg, Node(label=seg))
                node.codes.append(code)
            node.own.append(code)

    def walk(self, path: list[str]) -> Node:
        """Узел по пути из подписей от корня; KeyError — пути в дереве нет."""
        node = self.top
        for label in path:
            found = next((o for o in node.options() if o.label == label), None)
            if found is None:
                raise KeyError(label)
            node = found
        return node


def _levels(item: dict) -> list[str]:
    return [s.strip() for s in (item.get("full_path") or "").split(SEP) if s.strip()]


Decide = Callable[[str, str, dict[str, str]], Awaitable["object"]]

CANDIDATES_K = int(os.environ.get("NAV_CANDIDATES", "8"))
ELSEWHERE = "другая позиция — товар не подходит ни под одну из перечисленных"


def candidate_headings(store: TNVEDStore, precedents, description: str, valid: set[str],
                       k: int = CANDIDATES_K) -> list[str]:
    """Позиции-кандидаты: первые k от прецедентов ФСА и первые k от векторного поиска.
    Замер 29.10 (ЕЭК, 88 случаев): верная позиция среди них в 94% при k=8 (≈13 позиций),
    90% при k=5; поодиночке — 74% и 83%."""
    pre = [h for h, _ in precedents.headings(description, valid, limit=k)] if precedents else []
    vec: list[str] = []
    for item in store.search(description, top_k=8 * k):
        h = item["code"][:4]
        if h in valid and h not in vec:
            vec.append(h)
            if len(vec) >= k:
                break
    return list(dict.fromkeys(pre + vec))


def _criteria(options: list[Node]) -> dict[str, str]:
    crit: dict[str, str] = {}
    for o in options:
        key = o.label[:120]
        while key in crit:
            key += "·"
        crit[key] = OTHER_HINT if _OTHER.match(o.label) and len(o.label) < 12 else o.label[:LABEL_LIMIT]
    return crit


def _instructions(trail: list[str]) -> str:
    if not trail:
        return "Товар классифицируется по ТН ВЭД ЕАЭС. В какую группу ТН ВЭД он входит?"
    return ("Товар классифицируется по ТН ВЭД ЕАЭС; уже выбрано: "
            + " → ".join(t[:150] for t in trail)
            + ". Какой вариант следующего уровня подходит этому товару?")


async def navigate(tree: Tree, description: str, decide: Decide, path: list[str] | None = None,
                   threshold: float = NAV_THRESHOLD, candidates: list[str] | None = None) -> dict:
    """Спуск от узла по path до листа или до первого неуверенного узла.

    path — подписи от корня ([] — от корня; от позиции — tree.heading_path[h]). Если path
    не задан (None) и есть candidates (позиции, candidate_headings), первый шаг — выбор
    среди них или «другая позиция» (тогда спуск от корня по группам).

    decide(state, instructions, criteria) → объект с choice, confidence, probabilities
    (decisions.Choice). Возвращает: путь, шаги с вероятностями, код (сколько знаков
    известно), коды-кандидаты под узлом и вопрос, если остановились до листа. У каждого
    варианта вопроса и шага — path: с него продолжать спуск, если человек выбрал его."""
    steps: list[dict] = []
    cost = 0.0
    question = None
    trail = list(path or [])
    if path is None and candidates:
        heads = [h for h in candidates if h in tree.roots]
        crit = _criteria([tree.roots[h] for h in heads]) | {"другая позиция": ELSEWHERE}
        target = {k: tree.heading_path[h] for k, h in zip(crit, heads)} | {"другая позиция": []}
        ans = await decide(description, "Товар классифицируется по ТН ВЭД ЕАЭС. "
                           "В какую из этих товарных позиций он входит?", crit)
        cost += getattr(ans, "cost_usd", 0.0)
        ranked = sorted(({"label": k if k == "другая позиция" else tree.roots[heads[i]].label,
                          "code": "" if k == "другая позиция" else heads[i], "path": target[k],
                          "p": round(ans.probabilities.get(k, 0.0), 3)} for i, k in enumerate(crit)),
                        key=lambda o: -o["p"])
        steps.append({"path": None, "options": ranked,
                      "choice": next(o["label"] for o in ranked if o["path"] == target[ans.choice]),
                      "confidence": round(ans.confidence, 3)})
        if ans.confidence < threshold:
            question = {"path": None, "text": "Уточните, к какой товарной позиции ближе товар:",
                        "options": ranked}
            return _result(tree.top, [], steps, question, cost)
        trail = list(target[ans.choice])
    node = tree.walk(trail)
    while True:
        options = node.options()
        if not options:
            break
        if len(options) == 1:
            node = options[0]
            trail.append(node.label)
            continue
        crit = _criteria(options)
        by_key = dict(zip(crit, options))
        ans = await decide(description, _instructions(trail), crit)
        cost += getattr(ans, "cost_usd", 0.0)
        ranked = sorted(({"label": by_key[k].label, "code": by_key[k].prefix,
                          "path": trail + [by_key[k].label],
                          "p": round(ans.probabilities.get(k, 0.0), 3)} for k in crit),
                        key=lambda o: -o["p"])
        steps.append({"path": list(trail), "options": ranked,
                      "choice": by_key[ans.choice].label, "confidence": round(ans.confidence, 3)})
        if ans.confidence < threshold:
            question = {"path": list(trail), "text": _question_text(node), "options": ranked}
            break
        node = by_key[ans.choice]
        trail.append(node.label)
    return _result(node, trail, steps, question, cost)


def _result(node: Node, trail: list[str], steps: list[dict], question: dict | None, cost: float) -> dict:
    return {"path": trail, "code": node.prefix, "digits": len(node.prefix),
            "codes": node.codes[:20], "n_codes": len(node.codes), "steps": steps,
            "question": question, "done": question is None and len(node.codes) == 1,
            "cost_usd": round(cost, 6)}


def _question_text(node: Node) -> str:
    if node.label == "ТН ВЭД ЕАЭС":
        return "Уточните, к чему ближе товар:"
    return f"Уточните, что из этого подходит товару ({node.label[:160]}):"
