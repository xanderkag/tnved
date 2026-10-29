"""
Замер первого шага навигатора: есть ли верная позиция (4 знака) среди кандидатов. Модель не
зовёт — гоняется на любой квоте шлюза. Источники: прецеденты ФСА и векторный поиск, как в
navigator.candidate_headings, и расширения: длиннее списки, группы первых кандидатов целиком.

Два запроса на случай: «название. описание», как в eval_navigator, и одно название — так
пишут на открытой странице. В конце — промахи по названию: где верная позиция не нашлась.

Замер 29.10 (197 решений ЕЭК): сейчас 87% по описанию и 80% по названию, ≈14 позиций; по 20
от каждого — 90% и 85% при ≈30; поиск по словам (BM25 по текстам позиций) добавлял 1 пункт.
Промахи — в основном случаи, ради которых ЕЭК и выносила решение: верная позиция далека от
очевидной по смыслу (светодиодная лампа — 8543, фруктовое пюре — 2106). Их лечат примечания
к группам (ALG-6), а не длина списка.

  python eval_candidates.py data/raw/eec/eec_eval.jsonl
"""

from __future__ import annotations

import argparse
import json

import precedents
from navigator import Tree
from tnved_data import TNVEDStore


def ranked_vec(store: TNVEDStore, text: str, valid: set[str], k: int) -> list[str]:
    out: list[str] = []
    for item in store.search(text, top_k=8 * k):
        h = item["code"][:4]
        if h in valid and h not in out:
            out.append(h)
            if len(out) >= k:
                break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    args = ap.parse_args()

    store = TNVEDStore()
    tree = Tree(store)
    pidx = precedents.load_from_env()
    valid = set(tree.roots)
    by_group: dict[str, list[str]] = {}
    for h in sorted(valid):
        by_group.setdefault(h[:2], []).append(h)
    cases = [json.loads(line) for line in open(args.cases, encoding="utf-8")]
    cases = [c for c in cases if c["gold"][:4] in valid]
    print(f"случаев {len(cases)} (позиция эталона есть в действующем дереве)")

    for mode in ("название. описание", "только название"):
        rows = []
        for c in cases:
            text = f"{c['product']}. {c['description']}" if mode != "только название" else c["product"]
            pre = [h for h, _ in pidx.headings(text, valid, limit=20)] if pidx else []
            vec = ranked_vec(store, text, valid, 20)
            rows.append((c["gold"][:4], pre, vec))

        def rate(pick) -> str:
            hit = size = 0
            for gold, pre, vec in rows:
                cand = pick(pre, vec)
                hit += gold in cand
                size += len(cand)
            return f"{hit / len(rows):5.1%}  ≈{size / len(rows):4.1f} поз."

        union = lambda *ls: list(dict.fromkeys(h for lst in ls for h in lst))
        groups = lambda hs: [h for g in dict.fromkeys(x[:2] for x in hs) for h in by_group[g]]
        print(f"\n— {mode}")
        for name, pick in [
            ("прецеденты 8", lambda p, v: p[:8]),
            ("векторы 8", lambda p, v: v[:8]),
            ("сейчас: прец 8 + вект 8", lambda p, v: union(p[:8], v[:8])),
            ("прец 10 + вект 10", lambda p, v: union(p[:10], v[:10])),
            ("по 20", lambda p, v: union(p, v)),
            ("группы прец 3 + вект 3 целиком", lambda p, v: groups(union(p[:3], v[:3]))),
            ("сейчас + группы прец 2 + вект 2", lambda p, v: union(p[:8], v[:8], groups(union(p[:2], v[:2])))),
            ("сейчас + группы первых 1+1", lambda p, v: union(p[:8], v[:8], groups(union(p[:1], v[:1])))),
        ]:
            print(f"  {name:28s} {rate(pick)}")
        gr = sum(gold[:2] in {h[:2] for h in union(p[:8], v[:8])} for gold, p, v in rows)
        print(f"  группа эталона среди групп «сейчас»: {gr / len(rows):.1%}")
        if mode == "только название":
            print("  промахи «сейчас»:")
            for c, (gold, p, v) in zip(cases, rows):
                if gold not in union(p[:8], v[:8]):
                    print(f"    {gold} {c['product'][:60]!r:64s} прец {p[:3]} вект {v[:3]}")


if __name__ == "__main__":
    main()
