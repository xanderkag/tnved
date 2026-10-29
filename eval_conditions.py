"""
Замер правила числовых условий (conditions.choose) — без модели, на любой квоте шлюза.

1. Самопроверка по дереву: для каждого узла, где все ветки — диапазоны одной величины,
   описание «признак значение единица» из середины диапазона ветки должно вести в эту ветку.
   Расхождение — ошибка разбора подписи или дыра/нахлёст диапазонов в данных.
2. Решения ЕЭК: спуск по пути эталона; на каждом узле, где правило сработало, — совпал ли
   его выбор с веткой эталона.

  python eval_conditions.py data/raw/eec/eec_eval.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter

import conditions
from navigator import Tree
from tnved_data import TNVEDStore

BASE_UNIT = {"power": "кВт", "hp": "л.с.", "disp": "см³", "mass": "кг", "length": "мм", "volume": "л",
             "density": "г/м²", "pct": "мас.%", "volpct": "об.%", "voltage": "В", "kva": "кВА",
             "dtex": "дтекс", "pressure": "МПа"}


def inside(c: conditions.Cond) -> float:
    lo, hi = c.lo, c.hi
    if math.isinf(lo) and math.isinf(hi):
        return 1.0
    if math.isinf(lo):
        return hi / 2 if hi > 0 else hi - 1
    if math.isinf(hi):
        return lo * 1.5 + 1
    return (lo + hi) / 2


def self_check(tree: Tree) -> None:
    nodes = fires = wrong = 0
    bad, miss = [], []
    stack = [tree.top]
    while stack:
        node = stack.pop()
        opts = node.options()
        stack.extend(opts)
        if len(opts) < 2:
            continue
        labels = [o.label for o in opts]
        conds = [None if conditions._OTHER.match(l) else conditions.parse_label(l) for l in labels]
        named = [c for c in conds if c is not None]
        if not named or any(not c for c in named):
            continue
        common = set.intersection(*({x.dim for x in c} for c in named))
        if not common:
            continue
        nodes += 1
        dim = sorted(common)[0]
        for i, c in enumerate(conds):
            if c is None:
                continue
            x = next(x for x in c if x.dim == dim)
            feat = " ".join(reversed(x.feature))
            desc = f"товар {feat} {inside(x):g} {BASE_UNIT[dim]}"
            got = conditions.choose(labels, desc)
            fires += got is not None
            if got is None and len(miss) < 12:
                miss.append((node.prefix, labels[i][:70], desc))
            if got is not None and got[0] != i:
                wrong += 1
                if len(bad) < 15:
                    bad.append((node.prefix, labels[i][:70], labels[got[0]][:70], desc))
    print(f"самопроверка: узлов с диапазонами {nodes}, срабатываний {fires}, не в ту ветку {wrong}")
    for b in bad:
        print("   ", b)
    for m in miss:
        print("   не сработало", m)


def eec(tree: Tree, path: str) -> None:
    cases = [json.loads(line) for line in open(path, encoding="utf-8")]
    stat = Counter()
    errs, nocond = [], []
    for c in cases:
        gold = c["gold"]
        h = gold[:4]
        if h not in tree.roots:
            continue
        stat["случаев"] += 1
        text = f"{c['product']}. {c['description']}"
        node = tree.roots[h]
        while True:
            opts = node.options()
            if not opts:
                break
            nxt = next((i for i, o in enumerate(opts) if gold in o.codes), None)
            if nxt is None:
                break
            if len(opts) > 1:
                stat["узлов"] += 1
                labels = [o.label for o in opts]
                if any(conditions.parse_label(l) for l in labels):
                    stat["с условиями"] += 1
                    if len(nocond) < 15:
                        nocond.append((gold, labels[nxt][:60], [q[:2] for q in conditions.quantities(text)][:6]))
                got = conditions.choose([o.label for o in opts], text)
                if got is not None:
                    stat["сработало"] += 1
                    if got[0] == nxt:
                        stat["верно"] += 1
                    else:
                        errs.append((gold, c["product"][:50], got[1], opts[got[0]].label[:60], opts[nxt].label[:60]))
            node = opts[nxt]
    print(f"ЕЭК: {dict(stat)}")
    for e in errs[:20]:
        print("   ошибка", e)
    for e in nocond:
        print("   узел с условием", e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    args = ap.parse_args()
    tree = Tree(TNVEDStore())
    self_check(tree)
    eec(tree, args.cases)


if __name__ == "__main__":
    main()
