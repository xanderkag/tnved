"""Свод прецедентов ФСА (PRECEDENTS_PATH) из выгрузки реестра SLAI.

Вход — TSV (можно .gz) SLAI «fsa_hs_corpus»: hs_code, doc_kind, doc_status, product_name,
trade_mark, model, article, registered_at, expires_at; одна строка — пара «код ↔ товар»,
список кодов записи уже развёрнут в строки. Заявителей и ИНН в выгрузке нет.

Выход — TSV .gz с колонками name_norm, code, n (формат precedents.py): наименование в
нижнем регистре с одинарными пробелами, 10-значный код и сколько записей его заявили.

Запись реестра восстанавливается по совпадению всех полей, кроме кода. Запись со списком
кодов в свод не идёт: какой код к какому товару списка — не видно (так же собран свод asha,
«один допустимый код»). --status — только записи с этим статусом (по умолчанию все: статус
говорит о документе, а не о верности кода).

    python build_precedents.py incoming/fsa_hs_corpus_*.tsv.gz incoming/precedents.tsv.gz
    python build_precedents.py ... --status Действует
"""
from __future__ import annotations

import argparse
import csv
import gzip
import re
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize if sys.maxsize < 2**31 else 2**31 - 1)
KEY_FIELDS = ("doc_kind", "doc_status", "product_name", "trade_mark", "model", "article",
              "registered_at", "expires_at")
MULTI = ""  # у записи больше одного кода


def norm_name(raw: object) -> str:
    return " ".join(str(raw or "").lower().replace("ё", "е").split())


def build(src: Path, status: str | None) -> tuple[Counter, dict]:
    opener = gzip.open if src.suffix == ".gz" else open
    record_code: dict[int, str] = {}
    record_name: dict[int, str] = {}
    stats = Counter()
    with opener(src, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        missing = [c for c in ("hs_code", "product_name", *KEY_FIELDS) if c not in (reader.fieldnames or [])]
        if missing:
            sys.exit(f"{src}: нет колонок {', '.join(missing)}")
        for row in reader:
            stats["строк"] += 1
            if status and row["doc_status"] != status:
                continue
            code = re.sub(r"\D", "", row["hs_code"] or "")
            name = norm_name(row["product_name"])
            if not name:
                continue
            key = hash(tuple(row[k] for k in KEY_FIELDS))
            prev = record_code.get(key)
            if prev is None:
                record_code[key] = code if len(code) == 10 else MULTI
                record_name[key] = name
            elif prev != code:
                record_code[key] = MULTI
    pairs = Counter()
    for key, code in record_code.items():
        stats["записей"] += 1
        if code == MULTI:
            stats["записей со списком кодов или кодом не из 10 знаков"] += 1
            continue
        pairs[(record_name[key], code)] += 1
    stats["записей с одним кодом"] = sum(pairs.values())
    stats["наименований"] = len({n for n, _ in pairs})
    return pairs, dict(stats)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="выгрузка реестра SLAI: TSV, можно .gz")
    ap.add_argument("out", type=Path, help="свод: TSV .gz (name_norm, code, n)")
    ap.add_argument("--status", help="только записи с этим doc_status, например «Действует»")
    args = ap.parse_args()
    pairs, stats = build(args.src, args.status)
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["name_norm", "code", "n"])
        for (name, code), n in sorted(pairs.items()):
            w.writerow([name, code, n])
    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
