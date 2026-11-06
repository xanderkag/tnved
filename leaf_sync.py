"""Сверка действующих 10-значных кодов базы с официальным ЕТТ ЕЭК и правка по реестру.

Зачем: тариф TWS.BY отстаёт от решений ЕЭК. Когда ЕЭК делит код (8112 92 410 0 →
…410 1 и …410 9, 08.10.2026), база продолжает отдавать старый код как действующий —
с таким кодом декларацию не примут.

audit(db, rates)  — расхождения: действующие листы, которых нет в ЕТТ, и коды ЕТТ,
                    которых нет среди действующих листов. Без сети, без LLM.
apply(db, ref)    — правка по реестру references/leaf-sync-*.json: снятые коды получают
                    data_source='eec-retired' (статус retired), новые — 'eec' (current)
                    со ставкой из ЕТТ. Атомарно (копия + os.replace) и идемпотентно.

После пересборки базы (parse_tnved.py) правку применить заново:
    python leaf_sync.py --apply            # затем
    python leaf_sync.py --audit            # расхождений быть не должно
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
REFERENCE = BASE / "references/leaf-sync-2026-11-07.json"
RATES = BASE / "data/eec_rates.json"
CURRENT_SOURCES = ("hierarchy+tws", "tws", "eec")
RETIRED = "eec-retired"
SEP = " → "


def _rates(rates_path) -> dict:
    data = json.loads(Path(rates_path).read_text(encoding="utf8"))
    return data["rates"]


def audit(db, rates_path=RATES) -> dict:
    """{"not_in_ett": [...], "missing_leaves": [...]} — оба списка пусты, если база сходится с ЕТТ."""
    ett = _rates(rates_path)
    with sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True) as con:
        leaves = {c for c, s in con.execute("SELECT code, data_source FROM codes WHERE length(code) = 10")
                  if s in CURRENT_SOURCES}
    return {"not_in_ett": sorted(leaves - ett.keys()), "missing_leaves": sorted(ett.keys() - leaves)}


def _rate(ett: dict, code: str) -> str | None:
    rate = (ett.get(code) or {}).get("rate")
    if rate is None:
        return None
    return rate if not rate.replace(".", "", 1).isdigit() else f"{rate}%"


def apply(db, ref_path=REFERENCE, rates_path=RATES) -> list[str]:
    """Возвращает изменённые коды; пустой список — правка уже стоит."""
    db, ref, ett = Path(db), json.loads(Path(ref_path).read_text(encoding="utf8")), _rates(rates_path)
    for row in ref["add"]:
        if row["code"] not in ett:
            raise ValueError(f"{row['code']} нет в eec_rates.json — реестр расходится с ЕТТ")
    for row in ref["retire"]:
        if row["code"] in ett:
            raise ValueError(f"{row['code']} есть в eec_rates.json — снимать его нельзя")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = {r["code"]: dict(r) for r in con.execute("SELECT * FROM codes")}
    changed: list[tuple[str, tuple]] = []
    for row in ref["retire"]:
        have = rows.get(row["code"])
        if have and have["data_source"] != RETIRED:
            changed.append((row["code"], ("UPDATE codes SET data_source = ? WHERE code = ?", (RETIRED, row["code"]))))
    paths = {c: r["full_path"] for c, r in rows.items()}
    # Путь листа строим от пути снятого кода: тот шёл цепочкой тарифа, где «ниобий (колумбий)»
    # уже есть последним звеном, — так новые листы встают в навигаторе под ним.
    retired_path = {c: paths.get(r["code"]) for r in ref["retire"] for c in r["replaced_by"]}
    for node in ref["nodes"]:
        if node["code"] in rows:
            continue
        full = SEP.join(p for p in (paths.get(node["parent"]), node["description"].upper()) if p)
        paths[node["code"]] = full
        changed.append((node["code"], ("INSERT INTO codes VALUES (?,?,?,?,?,?,?)",
                                       (node["code"], node["description"].upper(), 3, node["parent"],
                                        full, None, "hierarchy"))))
    for leaf in ref["add"]:
        if leaf["code"] in rows:
            continue
        stem = retired_path.get(leaf["code"]) or paths.get(leaf["parent"])
        full = SEP.join(p for p in (stem, leaf["description"]) if p)
        changed.append((leaf["code"], ("INSERT INTO codes VALUES (?,?,?,?,?,?,?)",
                                       (leaf["code"], leaf["description"], 4, leaf["parent"], full,
                                        _rate(ett, leaf["code"]), "eec"))))
    if not changed:
        return []
    tmp = db.with_name(db.name + ".leaf-sync.tmp")
    shutil.copy2(db, tmp)
    try:
        with sqlite3.connect(tmp) as con:
            for _, (sql, args) in changed:
                con.execute(sql, args)
            for k, v in (("leaf_sync_revision", ref["revision"]), ("leaf_sync_as_of", ref["checked_at"]),
                         ("leaf_sync_source", ref["source"])):
                con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        os.replace(tmp, db)
    finally:
        if tmp.exists():
            tmp.unlink()
    return sorted(c for c, _ in changed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=BASE / "data/tnved.db")
    parser.add_argument("--rates", type=Path, default=RATES)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    if args.apply:
        print("Изменены: " + (", ".join(apply(args.db, rates_path=args.rates)) or "ничего — правка уже стоит"))
    if args.audit or not args.apply:
        print(json.dumps(audit(args.db, args.rates), ensure_ascii=False, indent=1))
