"""Свод «код → техрегламенты и вид документа» из открытого реестра ФСА (PAY-1, CONFORMITY_PATH).

Вход — помесячные выгрузки реестров деклараций (rds) и сертификатов (rss) соответствия после
fsa_extract на asha: TSV .gz с колонками kind, id, status, tr, codes. Поля заявителя в них уже нет.
Запись реестра берётся один раз — из самого позднего месяца. Коды записи — 10-значные из
действующей номенклатуры (--codes, по строке на код); запись с больше чем MAX_CODES кодами не
берём: в таких списках регламент относится к части товаров, какой — не видно.

Выход — TSV .gz, первой строкой «# period: ММ.ГГГГ–ММ.ГГГГ» по именам выгрузок: code (10 знаков
или старший узел — 8, 6, 4), n — документов, n_cert — из них сертификатов,
tr — регламенты «название=документов» через « | », по убыванию. Что и сколько оформляли под
этим кодом за период выгрузок, а не требование закона: код ставит заявитель, его не проверяют.

    python build_conformity.py out/*.tsv.gz --codes codes10.txt -o conformity.tsv.gz
"""
from __future__ import annotations

import argparse
import csv
import gzip
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize if sys.maxsize < 2**31 else 2**31 - 1)
MAX_CODES = 5
_CODE = re.compile(r"(?<![\d.])\d[\d ]{9,14}(?![\d.])")


def codes_of(text: str, valid: set[str]) -> set[str]:
    return {c for c in (re.sub(r"\D", "", x) for x in _CODE.findall(text or "")) if c in valid}


def regulations(text: str) -> tuple[str, ...]:
    """Названия регламентов записи: через «;», пробелы схлопнуты, без повторов."""
    out = dict.fromkeys(" ".join(t.split()).strip(" .").replace("|", "/").replace("=", "-")
                        for t in (text or "").split(";"))
    return tuple(sys.intern(t) for t in out if t)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--codes", required=True)
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    valid = {line.strip() for line in open(args.codes, encoding="utf-8") if line.strip()}
    recs: dict[tuple[str, str], tuple[bool, tuple[str, ...], tuple[str, ...]]] = {}
    for path in sorted(args.inputs):  # имена out/{вид}-{ГГГГММДД} — поздний месяц перезапишет ранний
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                cs = codes_of(row["codes"], valid)
                if not cs or len(cs) > MAX_CODES:
                    recs.pop((row["kind"], row["id"]), None)
                    continue
                recs[(row["kind"], row["id"])] = (row["kind"] == "rss", tuple(sorted(cs)), regulations(row["tr"]))
        print(path, len(recs), flush=True)

    n: Counter = Counter()
    cert: Counter = Counter()
    trs: dict[str, Counter] = defaultdict(Counter)
    for is_cert, cs, tr in recs.values():
        for key in {c[:d] for c in cs for d in (4, 6, 8, 10)}:
            n[key] += 1
            cert[key] += is_cert
            trs[key].update(tr)
    months = sorted(m for p in args.inputs for m in re.findall(r"-(\d{6})\d\d", Path(p).name))
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as f:
        if months:
            f.write(f"# period: {months[0][4:]}.{months[0][:4]}–{months[-1][4:]}.{months[-1][:4]}\n")
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["code", "n", "n_cert", "tr"])
        for key in sorted(n):
            w.writerow([key, n[key], cert[key],
                        " | ".join(f"{t}={k}" for t, k in trs[key].most_common())])
    print(f"записей {len(recs)}, кодов {sum(len(k) == 10 for k in n)}, позиций {sum(len(k) == 4 for k in n)}, "
          f"период {months[0] if months else '?'}–{months[-1] if months else '?'}")


if __name__ == "__main__":
    main()
