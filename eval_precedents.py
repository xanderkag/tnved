"""Замер: помогают ли прецеденты ФСА первому шагу (triage) найти позицию.

Набор — eval_sets/eec/eec_decisions.jsonl (решения ЕЭК). Для каждого случая:
  A — triage как есть;
  B — позиции A плюс до --extra позиций из прецедентов (без второго вызова модели);
  C — triage с блоком прецедентов в промпте.
Попадание — позиция эталона (4 знака) среди позиций варианта. Считаем и группу.

Запуск (LITE_MODE=1 — для triage нужна только data/tnved.db):
  LITE_MODE=1 python eval_precedents.py --precedents precedents.tsv.gz --field product \\
      --base-url https://…/v1 --model <алиас> --key-file <путь к файлу ключа> --out out.jsonl

Ключ читается из файла и никуда не пишется. --reasoning-effort none — для шлюза Ванги:
chat_template_kwargs.enable_thinking он пропускает, но OpenRouter его не знает
(Q-TN-VED-DOSTUP-1, ход 2).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

import classifier
import precedents
from tnved_data import TNVEDStore


def _with_reasoning_effort(effort: str) -> None:
    """Добавляет reasoning_effort в каждый вызов модели: у шлюза он выключает рассуждения."""
    original = classifier.AsyncOpenAI

    def factory(*args, **kwargs):
        client = original(*args, **kwargs)
        create = client.chat.completions.create

        async def create_with_effort(*a, **kw):
            kw["extra_body"] = {**(kw.get("extra_body") or {}), "reasoning_effort": effort}
            return await create(*a, **kw)

        client.chat.completions.create = create_with_effort
        return client

    classifier.AsyncOpenAI = factory


async def run(args) -> None:
    store = TNVEDStore()
    index = precedents.load(args.precedents)
    key = Path(args.key_file).read_text(encoding="utf-8").strip() if args.key_file else ""
    cfg = classifier.LLMConfig(base_url=args.base_url, model=args.model, api_key=key)
    if args.reasoning_effort:
        _with_reasoning_effort(args.reasoning_effort)
    cases = [json.loads(line) for line in open(args.cases, encoding="utf-8")]
    if args.limit:
        cases = cases[: args.limit]
    done: dict[str, dict] = {}
    if args.resume and Path(args.resume).exists():  # готовые случаи не повторяем — только отказы
        for line in open(args.resume, encoding="utf-8"):
            r = json.loads(line)
            if "headings" in r.get("A", {}) and "headings" in r.get("C", {}):
                done[r["id"]] = r
    print(f"готовых из прошлого прогона: {len(done)}, к запуску: {sum(c['id'] not in done for c in cases)}")
    sem = asyncio.Semaphore(args.concurrency)

    async def one(case: dict) -> dict:
        if case["id"] in done:
            return done[case["id"]]
        text = case[args.field]
        prec = index.headings(text, valid=store.current_headings)
        out = {"id": case["id"], "gold": case["gold"], "text": text[:200],
               "prec": [h for h, _ in prec[:5]]}
        async with sem:
            for variant, hints in (("A", None), ("C", prec)):
                try:
                    res = await classifier.triage(store, text, cfg, precedents=hints)
                    out[variant] = {"group": res["group_code"], "headings": res["headings"]}
                except Exception as exc:  # отказ модели — в отчёт, прогон не рвём
                    out[variant] = {"error": str(exc)[:200]}
        return out

    results = await asyncio.gather(*(one(c) for c in cases))
    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    report(results, args.extra)


def report(results: list[dict], extra: int) -> None:
    hits, n = Counter(), Counter()
    for r in results:
        g4, g2 = r["gold"][:4], r["gold"][:2]
        if "headings" not in r.get("A", {}) or "headings" not in r.get("C", {}):
            n["error"] += 1
            continue
        n["ok"] += 1
        a = r["A"]["headings"]
        b = a + [h for h in r["prec"] if h not in a][:extra]
        c = r["C"]["headings"]
        for name, heads in (("A", a), ("B", b), ("C", c), ("prec@3", r["prec"][:3])):
            hits[name] += g4 in heads
            n[name + "_len"] += len(heads)
        hits["A_first"] += bool(a) and a[0] == g4
        hits["C_first"] += bool(c) and c[0] == g4
        hits["A_group"] += r["A"]["group"] == g2
        hits["C_group"] += r["C"]["group"] == g2
    ok = n["ok"] or 1
    print(f"случаев {n['ok']}, отказов модели {n['error']}")
    for name in ("A", "B", "C", "prec@3"):
        print(f"  {name:7s} позиция эталона среди названных: {hits[name] / ok:.0%}"
              f"  (в среднем позиций {n[name + '_len'] / ok:.1f})")
    print(f"  первая позиция верна: A {hits['A_first'] / ok:.0%}, C {hits['C_first'] / ok:.0%}")
    print(f"  группа верна:         A {hits['A_group'] / ok:.0%}, C {hits['C_group'] / ok:.0%}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", default="eval_sets/eec/eec_decisions.jsonl")
    p.add_argument("--precedents", required=True)
    p.add_argument("--field", choices=("product", "description"), default="product")
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--key-file", default="")
    p.add_argument("--reasoning-effort", default="")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--extra", type=int, default=2, help="сколько позиций прецедентов добавить в варианте B")
    p.add_argument("--out", default="eval_precedents.jsonl")
    p.add_argument("--resume", default="", help="взять готовые случаи из прошлого --out, повторить отказы")
    p.add_argument("--report", default="", help="только отчёт по готовому --out")
    args = p.parse_args()
    if args.report:
        report([json.loads(line) for line in open(args.report, encoding="utf-8")], args.extra)
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
