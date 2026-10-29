"""
Замер навигатора на решениях ЕЭК (data/raw/eec/eec_eval.jsonl): спуск от товарной позиции
до кода, ветку на узле выбирает канал решений (DECISIONS_MODEL).

--start gold: позиция из эталона, меряется сам спуск; --start top: от корня (группа, позиция).
Два счёта на один прогон:
  - без человека: код верен на стольких знаках, сколько у эталона, без единого вопроса;
  - с человеком: где навигатор спросил, отвечает «знающий пользователь» — ветка эталона.
    Число вопросов на товар — цена этой точности для пользователя.
Ещё — калибровка: верность выбора ветки по корзинам уверенности.

  python eval_navigator.py data/raw/eec/eec_eval.jsonl --out eval_results/nav.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
from pathlib import Path

from classifier import llm_config_from_env
from decisions import DecisionsUnavailable, choose, decisions_config_from_env
import precedents
from navigator import NAV_THRESHOLD, Tree, candidate_headings, navigate
from tnved_data import TNVEDStore

MAX_QUESTIONS = 6


def _contains(tree: Tree, path: list[str], gold: str) -> bool:
    return any(c.startswith(gold) for c in tree.walk(path).codes)


def _leads(tree: Tree, option: dict, gold: str) -> bool:
    """Ведёт ли вариант к эталону. «Другая позиция» (path []) — когда эталона среди
    кандидатов нет: тогда знающий пользователь выберет её."""
    if option["path"] == []:
        return False
    return _contains(tree, option["path"], gold)


async def run_case(tree: Tree, case: dict, decide, threshold: float, sem: asyncio.Semaphore,
                   start: str, candidates: list[str] | None) -> dict:
    gold = case["gold"][: case["gold_digits"]]
    state = f"{case['product']}. {case['description']}"
    async with sem:
        try:
            return await _run_case(tree, case, gold, state, decide, threshold, start, candidates)
        except DecisionsUnavailable as exc:
            return {"id": case["id"], "gold": gold, "error": str(exc)}


async def _run_case(tree: Tree, case: dict, gold: str, state: str, decide, threshold: float,
                    start: str, candidates: list[str] | None) -> dict:
    begin = {"gold": tree.heading_path[gold[:4]], "top": [], "cand": None}[start]
    first = res = await navigate(tree, state, decide, path=begin, threshold=threshold,
                                 candidates=candidates)
    asked, steps, cost = 0, list(first["steps"]), first["cost_usd"]
    while res["question"] and len(res["code"]) < len(gold) and asked < MAX_QUESTIONS:
        opts = res["question"]["options"]
        pick = next((o for o in opts if _leads(tree, o, gold)), None)
        if pick is None:
            pick = next((o for o in opts if o["path"] == []), None)
        if pick is None:
            break
        asked += 1
        res = await navigate(tree, state, decide, path=pick["path"], threshold=threshold)
        steps += res["steps"]
        cost += res["cost_usd"]
    for st in steps:
        inside = [_leads(tree, o, gold) for o in st["options"]]
        chosen = [o["label"] for o in st["options"]].index(st["choice"])
        st["gold_ok"] = None if all(inside) else inside[chosen]
    auto = first["code"]
    agree = _correct_digits(auto, gold)
    return {"id": case["id"], "gold": gold, "auto_code": auto,
            "gold_in_candidates": None if candidates is None else gold[:4] in candidates,
            "auto_ok": agree >= len(gold),
            "auto_wrong": agree < min(len(auto), len(gold)),
            "final_code": res["code"], "final_ok": _correct_digits(res["code"], gold) >= len(gold),
            "questions": asked, "steps": steps, "cost_usd": round(cost, 6)}


def _correct_digits(code: str, gold: str) -> int:
    n = 0
    while n < min(len(code), len(gold)) and code[n] == gold[n]:
        n += 1
    return n


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    ap.add_argument("--out", default="eval_results/nav.jsonl")
    ap.add_argument("--threshold", type=float, default=NAV_THRESHOLD)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--start", choices=("gold", "top", "cand"), default="gold",
                    help="gold — от позиции эталона; top — от корня: группа, позиция, дальше; "
                         "cand — выбор среди позиций-кандидатов (прецеденты ФСА + векторный поиск)")
    args = ap.parse_args()

    dcfg = decisions_config_from_env(llm_config_from_env())
    if dcfg is None:
        raise SystemExit("DECISIONS_MODEL не задан — канал решений выключен")
    store = TNVEDStore()
    tree = Tree(store)
    cases = [json.loads(line) for line in open(args.cases, encoding="utf-8")]
    cases = [c for c in cases if c["gold_digits"] > 4 and c["gold"][:4] in tree.roots
             and any(code.startswith(c["gold"]) for code in tree.roots[c["gold"][:4]].codes)]
    if args.limit:
        cases = cases[: args.limit]

    cands: dict[str, list[str] | None] = {c["id"]: None for c in cases}
    if args.start == "cand":
        pidx = precedents.load_from_env()
        valid = set(tree.roots)
        for c in cases:
            cands[c["id"]] = candidate_headings(store, pidx, f"{c['product']}. {c['description']}", valid)

    async def decide(state, instructions, criteria):
        return await choose(dcfg, state, instructions, criteria, retry_budget=180)

    sem = asyncio.Semaphore(args.parallel)
    results = await asyncio.gather(*(run_case(tree, c, decide, args.threshold, sem, args.start, cands[c["id"]])
                                     for c in cases))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    failed = [r for r in results if "error" in r]
    if failed:
        print(f"канал решений отказал на {len(failed)} случаях, первая ошибка: {failed[0]['error']}")
    results = [r for r in results if "error" not in r]
    n = len(results)
    if not n:
        return
    print(f"случаев {n} (эталон точнее 4 знаков и есть в действующем дереве), "
          f"порог {args.threshold}, старт {args.start}")
    ok, wrong = sum(r["auto_ok"] for r in results), sum(r["auto_wrong"] for r in results)
    if args.start == "cand":
        print(f"верная позиция среди кандидатов: {sum(r['gold_in_candidates'] for r in results)}/{n}")
    print(f"без человека: верно {ok}/{n}, ошибка {wrong}, остановились раньше с вопросом {n - ok - wrong}")
    print(f"с человеком: верно {sum(r['final_ok'] for r in results)}/{n}, "
          f"вопросов на товар {sum(r['questions'] for r in results) / n:.2f}, "
          f"товаров с вопросом {sum(r['questions'] > 0 for r in results)}, "
          f"стоимость ${sum(r['cost_usd'] for r in results):.4f}")
    bins = collections.defaultdict(list)
    for r in results:
        for st in r["steps"]:
            if st["gold_ok"] is not None:
                bins[min(int(st["confidence"] * 10), 9)].append(st["gold_ok"])
    print("калибровка узлов: уверенность — узлов — верно")
    for k in sorted(bins):
        print(f"  {k / 10:.1f}–{(k + 1) / 10:.1f}  {len(bins[k]):3d}  {sum(bins[k]) / len(bins[k]):.0%}")


if __name__ == "__main__":
    asyncio.run(main())
