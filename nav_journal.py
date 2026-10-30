"""
Журнал навигатора: шаги подбора и отзывы «код подошёл / не подошёл». Из него — сводка для нас
(GET /api/navigate/stats): сколько товаров, где спрашиваем, где бросают, где код не подошёл.

Шаг пишется без описания товара: request_id, канал, код, сколько знаков, был ли вопрос. Описание
попадает в журнал только с отзывом — его присылает сам человек, нажав «не подошёл», и без него
отзыв не разобрать. Почты и адреса посетителя здесь нет.

NAV_JOURNAL_PATH — файл SQLite вне образа (на asha — ./state/nav.db). Не задан — журнала нет,
отзыв отвечает 503. Хранится NAV_JOURNAL_DAYS суток (180), старое чистится при старте.
Отзыв принимается только на выданный нами request_id, один на товар (повтор заменяет прежний);
каналу site — не больше FEEDBACK_DAILY_CAP отзывов в сутки на весь сервис.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

JOURNAL_DAYS = int(os.environ.get("NAV_JOURNAL_DAYS", "180"))
FEEDBACK_DAILY_CAP = int(os.environ.get("NAV_FEEDBACK_DAILY_CAP", "500"))
VERDICTS = ("wrong", "right")


class Journal:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS steps (ts REAL, rid TEXT, channel TEXT, first INTEGER, "
                         "depth INTEGER, code TEXT, digits INTEGER, done INTEGER, question INTEGER, "
                         "cached INTEGER, cost REAL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS steps_rid ON steps (rid)")
        self._db.execute("CREATE INDEX IF NOT EXISTS steps_ts ON steps (ts)")
        self._db.execute("CREATE TABLE IF NOT EXISTS feedback (rid TEXT PRIMARY KEY, ts REAL, channel TEXT, "
                         "verdict TEXT, code TEXT, expected TEXT, comment TEXT, description TEXT)")
        cutoff = time.time() - JOURNAL_DAYS * 86400
        self._db.execute("DELETE FROM steps WHERE ts < ?", (cutoff,))
        self._db.execute("DELETE FROM feedback WHERE ts < ?", (cutoff,))

    def step(self, rid: str, channel: str, path: list[str] | None, res: dict, cached: bool) -> None:
        row = (time.time(), rid, channel, int(path is None), len(path or []), res.get("code") or "",
               res.get("digits", 0), int(bool(res.get("done"))), int(bool(res.get("question"))),
               int(cached), 0.0 if cached else float(res.get("cost_usd") or 0))
        with self._lock:
            self._db.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)

    def issued(self, rid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM steps WHERE rid = ? LIMIT 1", (rid,)).fetchone() is not None

    def feedback_today(self, channel: str) -> int:
        day = time.time() - time.time() % 86400  # сутки UTC, как у лимитов
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM feedback WHERE channel = ? AND ts >= ?",
                                    (channel, day)).fetchone()[0]

    def feedback(self, rid: str, channel: str, verdict: str, code: str, expected: str, comment: str,
                 description: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?,?,?)",
                             (rid, time.time(), channel, verdict, code, expected, comment, description))

    def stats(self, days: int) -> dict:
        """Сводка за days суток по каналам: товары, вопросы, дошли ли до кода, отзывы."""
        since = time.time() - days * 86400
        with self._lock:
            rows = self._db.execute("SELECT rid, channel, first, code, digits, done, question, cached, cost "
                                    "FROM steps WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
            fb = self._db.execute("SELECT rid, channel, verdict, code, expected, comment, description, ts "
                                  "FROM feedback WHERE ts >= ? ORDER BY ts DESC", (since,)).fetchall()
        items: dict[str, dict] = {}
        for rid, channel, first, code, digits, done, question, cached, cost in rows:
            it = items.setdefault(rid, {"channel": channel, "first": None, "answers": 0, "last": None, "cost": 0.0})
            if first and it["first"] is None:
                it["first"] = {"question": bool(question), "cached": bool(cached), "digits": digits}
            elif not first:
                it["answers"] += 1
            it["last"] = {"code": code, "digits": digits, "done": bool(done), "question": bool(question)}
            it["cost"] += cost
        out: dict[str, dict] = {}
        for it in items.values():
            if it["first"] is None:  # первый шаг раньше окна сводки
                continue
            c = out.setdefault(it["channel"], {"items": 0, "cached": 0, "asked_first": 0, "answers": 0,
                                               "done": 0, "stopped_at_question": 0, "cost_usd": 0.0,
                                               "final_digits": {}})
            c["items"] += 1
            c["cached"] += it["first"]["cached"]
            c["asked_first"] += it["first"]["question"]
            c["answers"] += it["answers"]
            c["done"] += it["last"]["done"]
            c["stopped_at_question"] += it["last"]["question"]
            c["cost_usd"] += it["cost"]
            d = str(it["last"]["digits"])
            c["final_digits"][d] = c["final_digits"].get(d, 0) + 1
        for c in out.values():
            c["cost_usd"] = round(c["cost_usd"], 4)
            c["answers_per_item"] = round(c["answers"] / c["items"], 2)
        verdicts = {v: sum(1 for f in fb if f[2] == v) for v in VERDICTS}
        wrong = [{"request_id": f[0][:8], "channel": f[1], "code": f[3], "expected": f[4], "comment": f[5],
                  "description": f[6], "at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(f[7]))}
                 for f in fb if f[2] == "wrong"][:50]
        return {"days": days, "channels": out, "feedback": verdicts, "wrong": wrong}


def load_from_env() -> Journal | None:
    path = os.environ.get("NAV_JOURNAL_PATH", "").strip()
    return Journal(path) if path else None
