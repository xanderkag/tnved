"""
Замер уровня 1 (декларации) на отложенной части выгрузки ДТ — без модели.

Выгрузка делится по номерам ДТ, а не по строкам: строки одной декларации не попадают по обе
стороны. По умолчанию отложены самые поздние ДТ (--holdout, доля деклараций) — как в жизни:
свод из прошлых ДТ, новые товары приходят потом. --split hash — случайно по номеру ДТ.

По отложенным строкам считается то, что делает сервис до модели (declarations.batch_lookup):
  - покрытие — доля строк, где уровень 1 выдал код, по пути: артикул, такое же описание,
    похожее описание (с --vectors);
  - точность выданного кода на 10/6/4 знаках против кода в ДТ;
  - сколько строк ушло бы в модель с проверкой «в ДТ разные коды» и с примерами.
С --vectors — ещё таблица порогов DECLARATIONS_NEAR_MIN: сколько строк получает код по
похожему описанию и сколько из них верно при каждом пороге.

Для замера модели (с примерами из ДТ и без) пишет в --out:
  - decl_train_*.csv.gz — свод только из обучающей части: его монтируют сервису как
    DECLARATIONS_PATH, иначе отложенные строки найдутся в своде сами;
  - decl_model_*.xlsx — отложенные строки без кода из ДТ (описание, код) — для eval.py:
      python eval.py eval_results/decl_model_*.xlsx --desc Описание --code "Код ТН ВЭД" --base ...
    дважды: сервис со сводом из decl_train и без DECLARATIONS_PATH.

Векторы (--vectors) — сервер из env (EMBEDDINGS_*), как у сервиса: только внутренняя сеть.
Выгрузка и результаты в git не входят (incoming/, eval_results/).

  python eval_declarations.py incoming/dt_export.csv.gz
  python eval_declarations.py incoming/dt_export.csv.gz --vectors --holdout 0.2
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

os.environ.setdefault("LITE_MODE", "1")  # справочнику нужен только статус кода, не FAISS

import declarations  # noqa: E402
from classifier import designation_only  # noqa: E402

THRESHOLDS = (0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97)
PATHS = ("article", "description", "similar")
PATH_NAMES = {"article": "артикул", "description": "такое же описание", "similar": "похожее описание"}


def parse_date(raw: str) -> datetime | None:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime((raw or "").strip()[:10], fmt)
        except ValueError:
            pass
    return None


def split(rows: list[dict], holdout: float, mode: str, seed: int) -> tuple[list[dict], list[dict]]:
    """Делим по номерам ДТ: date — самые поздние ДТ в отложенную часть, hash — случайно."""
    by_dt: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_dt[(r.get("dt_number") or "").strip()].append(r)
    dts = list(by_dt)
    if mode == "date":
        # дата ДТ — самая поздняя из её строк; без даты — в обучающую часть (в начало)
        def when(dt: str) -> datetime:
            dates = [d for d in (parse_date(r.get("dt_date")) for r in by_dt[dt]) if d]
            return max(dates) if dates else datetime.min
        dts.sort(key=lambda dt: (when(dt), dt))
        cut = len(dts) - round(len(dts) * holdout)
        held = set(dts[cut:])
    else:
        held = {dt for dt in dts
                if int(hashlib.sha1(f"{seed}:{dt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < holdout}
    train = [r for dt in dts if dt not in held for r in by_dt[dt]]
    test = [r for dt in dts if dt in held for r in by_dt[dt]]
    return train, test


def measure(train: list[dict], test: list[dict], store, embedder=None,
            thresholds: tuple[float, ...] = ()) -> tuple[dict, list[dict]]:
    """Уровень 1 по отложенным строкам: сводка и построчные исходы."""
    index = declarations.DeclarationIndex(train)
    if embedder is not None:
        index.build_vectors(embedder)
    out_rows = []
    for r in test:
        expected = declarations._digits(r.get("hs_code"))
        if len(expected) != 10:
            continue
        desc = (r.get("description") or "").strip()
        dsg = designation_only(desc)
        got = declarations.batch_lookup(index, store, r.get("article") or "", desc, dsg, embedder)
        row = {"dt_number": r.get("dt_number"), "item_no": r.get("item_no") or "",
               "article": r.get("article") or "", "description": desc,
               "expected": expected, "designation": dsg, "path": "", "code": "",
               "checks": 0, "examples": 0,
               "parts": declarations.parts_only(declarations.norm_description(desc))}
        if got and "result" in got:
            row["path"] = got["result"]["match"]
            row["code"] = got["result"]["primary"]["code"]
        elif got:
            row["checks"] = len(got["checks"])
            row["examples"] = len(got["examples"])
        out_rows.append(row)

    n = len(out_rows)
    summary: dict = {"rows_train": len(train), "rows_test": n,
                     "dt_train": len({r.get("dt_number") for r in train}),
                     "dt_test": len({r.get("dt_number") for r in test}),
                     "vectors": embedder is not None, "paths": {}}
    answered = [r for r in out_rows if r["code"]]
    for name, subset in [("всего", answered)] + [(p, [r for r in answered if r["path"] == p]) for p in PATHS]:
        summary["paths"][name] = {
            "rows": len(subset),
            "coverage": len(subset) / n if n else 0.0,
            **{f"acc{k}": (sum(r["code"][:k] == r["expected"][:k] for r in subset) / len(subset) if subset else None)
               for k in (10, 6, 4)},
        }
    # По товарам: код у товара ДТ один, а строк у товара бывает десятки — по строкам большой товар
    # весит, как десятки малых. Исход товара — первая его строка с кодом из ДТ, иначе «в модель».
    items: dict[tuple, dict] = {}
    for r in out_rows:
        key = (r["dt_number"], r["item_no"]) if r["item_no"] else (r["dt_number"], id(r))
        if key not in items or (r["code"] and not items[key]["code"]):
            items[key] = r
    it = list(items.values())
    summary["items"] = {"items": len(it)}
    for name, subset in [("всего", [r for r in it if r["code"]])] + [(p, [r for r in it if r["path"] == p]) for p in PATHS]:
        summary["items"][name] = {
            "items": len(subset),
            "coverage": len(subset) / len(it) if it else 0.0,
            **{f"acc{k}": (sum(r["code"][:k] == r["expected"][:k] for r in subset) / len(subset) if subset else None)
               for k in (10, 4)},
        }
    to_model = [r for r in out_rows if not r["code"]]
    summary["to_model"] = {
        "rows": len(to_model),
        "with_checks": sum(1 for r in to_model if r["checks"]),
        "with_examples": sum(1 for r in to_model if r["examples"]),
        "designation_only": sum(1 for r in to_model if r["designation"]),
        "parts_only": sum(1 for r in to_model if r["parts"] and not r["designation"]),
    }
    # артикул уже встречался в прошлых ДТ, но с другим кодом — переоформляли
    summary["article_code_changed"] = sum(
        1 for r in answered if r["path"] == "article" and r["code"] != r["expected"])

    if embedder is not None and thresholds:
        # строки, где артикул кода не дал, а описание — не одно обозначение и не такое же
        pool = [r for r in out_rows if r["path"] not in ("article", "description") and not r["designation"]]
        table = []
        for t in thresholds:
            index.near_min = t
            hits = [(r, index.near(r["description"], store, embedder)) for r in pool]
            coded = [(r, h) for r, h in hits if h and "code" in h and h["sim"] < 1.0]
            table.append({"threshold": t, "pool": len(pool), "answered": len(coded),
                          "acc10": sum(h["code"] == r["expected"] for r, h in coded) / len(coded) if coded else None,
                          "acc4": sum(h["code"][:4] == r["expected"][:4] for r, h in coded) / len(coded) if coded else None})
        summary["thresholds"] = table
    return summary, out_rows


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f} %"


def print_report(s: dict) -> None:
    print(f"Обучающая часть: ДТ {s['dt_train']}, строк {s['rows_train']}; "
          f"отложено: ДТ {s['dt_test']}, строк с кодом {s['rows_test']}; векторы: {'да' if s['vectors'] else 'нет'}")
    print(f"{'путь':<20}{'строк':>7}{'покрытие':>10}{'10 зн.':>9}{'6 зн.':>9}{'4 зн.':>9}")
    for name, v in s["paths"].items():
        print(f"{PATH_NAMES.get(name, name):<20}{v['rows']:>7}{pct(v['coverage']):>10}"
              f"{pct(v['acc10']):>9}{pct(v['acc6']):>9}{pct(v['acc4']):>9}")
    i = s["items"]
    print(f"По товарам (ДТ + номер товара), всего {i['items']}:")
    for name in ("всего",) + PATHS:
        v = i[name]
        print(f"  {PATH_NAMES.get(name, name):<18}{v['items']:>7}{pct(v['coverage']):>10}"
              f"{pct(v['acc10']):>9}{'':>9}{pct(v['acc4']):>9}")
    m = s["to_model"]
    print(f"В модель: {m['rows']} (с проверкой по ДТ {m['with_checks']}, с примерами {m['with_examples']}, "
          f"только обозначение {m['designation_only']}, только части {m.get('parts_only', 0)}); артикул с другим кодом, чем в прошлых ДТ: "
          f"{s['article_code_changed']}")
    for t in s.get("thresholds", []):
        print(f"  порог {t['threshold']:.2f}: из {t['pool']} код по похожему описанию у {t['answered']}, "
              f"верно 10 зн. {pct(t['acc10'])}, 4 зн. {pct(t['acc4'])}")


def write_outputs(out: Path, stamp: str, summary: dict, rows: list[dict], train: list[dict]) -> list[Path]:
    from openpyxl import Workbook

    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"decl_eval_{stamp}.json", out / f"decl_rows_{stamp}.csv",
             out / f"decl_train_{stamp}.csv.gz", out / f"decl_model_{stamp}.xlsx"]
    paths[0].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with paths[1].open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["expected"], delimiter=";")
        w.writeheader()
        w.writerows(rows)
    buf = io.StringIO()
    fields = list(dict.fromkeys(k for r in train for k in r))
    w = csv.DictWriter(buf, fieldnames=fields, delimiter=";")
    w.writeheader()
    w.writerows(train)
    paths[2].write_bytes(gzip.compress(buf.getvalue().encode("utf-8")))
    wb = Workbook()
    wb.active.append(["Описание", "Код ТН ВЭД"])
    for r in rows:
        if not r["code"] and not r["designation"] and not r["parts"]:
            wb.active.append([r["description"], r["expected"]])
    wb.save(paths[3])
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path, help="выгрузка ДТ: CSV «;», UTF-8, можно .gz (формат SLAI)")
    ap.add_argument("--holdout", type=float, default=0.2, help="доля ДТ в отложенной части (0.2)")
    ap.add_argument("--split", choices=("date", "hash"), default="date", help="date — самые поздние ДТ (по умолчанию)")
    ap.add_argument("--seed", type=int, default=1, help="для --split hash")
    ap.add_argument("--vectors", action="store_true", help="похожие описания: сервер векторов из env")
    ap.add_argument("--out", type=Path, default=Path("eval_results"))
    args = ap.parse_args()
    if not 0 < args.holdout < 1:
        sys.exit("--holdout: доля из (0, 1)")

    from tnved_data import TNVEDStore

    rows = declarations.read_rows(args.export)
    train, test = split(rows, args.holdout, args.split, args.seed)
    if not train or not test:
        sys.exit(f"деление не удалось: обучающих строк {len(train)}, отложенных {len(test)}")
    embedder = None
    if args.vectors:
        from embedder import get_embedder
        embedder = get_embedder()
    summary, out_rows = measure(train, test, TNVEDStore(), embedder, THRESHOLDS if args.vectors else ())
    summary.update(export=args.export.name, split=args.split, holdout=args.holdout)
    print_report(summary)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for p in write_outputs(args.out, stamp, summary, out_rows, train):
        print(f"→ {p}")


if __name__ == "__main__":
    main()
