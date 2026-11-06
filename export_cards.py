"""Выгрузка карточек кодов для сайта: один UTF-8 JSON, card — ровно ответ GET /api/codes/{code}.

Зачем: сайту нужны полные карточки (пошлина с источником и датой, НДС, маркировка, акциз,
conformity) для страниц спроса — без серии публичных GET на каждой сборке и без LLM.
Карточка собирается той же функцией, что и в API (store.code_card + conformity), поэтому
расхождения между страницей и подбором нет.

Формат (schema_version 1):
  {"schema_version": 1, "generated_at": ISO, "revision": sha256 от codes,
   "sources": {tariff_as_of, db_built_at, leaf_sync_revision, ...},
   "codes": [{"code", "status", "content_sha256", "card"|null, "error"|null}]}
content_sha256 — хэш карточки: меняется только при содержательной правке, по нему сайт
ставит lastmod. generated_at — время выгрузки, а не дата норм: даты норм внутри card.

Запуск в контейнере (LITE: без FAISS и без сервера векторов):
  docker compose exec -T app python export_cards.py --csv wordstat.csv > cards.json
  docker compose exec -T app python export_cards.py 3926909709 6404110000
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

# Всегда LITE, даже если в контейнере LITE_MODE=0: карточке не нужен FAISS, а второй индекс
# в памяти рядом с сервисом на проде лишний. Ставим до импорта tnved_data.
os.environ["LITE_MODE"] = "1"

CODE_RE = re.compile(r"\d{10}")


def wordstat_codes(text: str) -> list[str]:
    """10-значные коды из выгрузки Wordstat (вид=цифры), в порядке частоты, без повторов."""
    seen: dict[str, None] = {}
    for row in csv.DictReader(io.StringIO(text)):
        code = re.sub(r"\D", "", row.get("товар_или_код") or "")
        if row.get("вид") == "цифры" and CODE_RE.fullmatch(code):
            seen.setdefault(code, None)
    return list(seen)


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build(codes: list[str], store, conf, now: datetime | None = None) -> dict:
    items = []
    for code in codes:
        try:
            card = store.code_card(code)
            if card is not None and conf is not None:
                card["conformity"] = conf.lookup(code)
            err = None if card is not None else "Кода нет в справочнике"
        except Exception as e:  # noqa: BLE001 — ошибка по коду явная, выгрузка остальных идёт
            card, err = None, f"{type(e).__name__}: {e}"
        items.append({"code": code, "status": card["status"] if card else "unknown",
                      "content_sha256": _sha(card) if card else None, "card": card, "error": err})
    version = store.version()
    return {
        "schema_version": 1,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "revision": _sha([(i["code"], i["content_sha256"], i["error"]) for i in items]),
        "sources": version,
        "codes": items,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("codes", nargs="*")
    p.add_argument("--csv", help="выгрузка Wordstat (CSV, «-» — stdin)")
    args = p.parse_args(argv)
    codes = list(args.codes)
    if args.csv:
        text = sys.stdin.read() if args.csv == "-" else open(args.csv, encoding="utf-8-sig").read()
        codes += [c for c in wordstat_codes(text) if c not in codes]
    if not codes:
        p.error("нет кодов")
    import conformity
    from tnved_data import TNVEDStore
    with contextlib.redirect_stdout(sys.stderr):  # служебная печать загрузки не должна портить JSON
        store, conf = TNVEDStore(), conformity.load_from_env()
    out = build(codes, store, conf)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    bad = [i["code"] for i in out["codes"] if i["status"] != "current"]
    print(f"[export_cards] кодов {len(codes)}, не current: {bad or 'нет'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
