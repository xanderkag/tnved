"""Сессии, чаты и пакетные задачи на диске (TECH_DEBT #4, B2): перезапуск и выкат их не теряют.

STATE_DB_PATH — файл SQLite вне образа (compose монтирует ./state). Не задан — всё только в
памяти, как раньше, и /health это показывает (state: memory). Задан и не открывается — сервис
не стартует. Объект хранится целиком, JSON; срок жизни — тот же SESSION_TTL_HOURS, чистит
_cleanup_loop в api.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

KINDS = ("sessions", "chats", "batch_jobs")


class StateStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS objects (kind TEXT NOT NULL, id TEXT NOT NULL, "
                         "body TEXT NOT NULL, PRIMARY KEY (kind, id))")

    def save(self, kind: str, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO objects (kind, id, body) VALUES (?, ?, ?)",
                             (kind, obj["id"], body))

    def load(self, kind: str) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT id, body FROM objects WHERE kind = ?", (kind,)).fetchall()
        return {i: json.loads(body) for i, body in rows}

    def delete(self, kind: str, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM objects WHERE kind = ? AND id = ?", [(kind, i) for i in ids])

    def close(self) -> None:
        with self._lock:
            self._db.close()


def load_from_env() -> StateStore | None:
    path = os.environ.get("STATE_DB_PATH", "").strip()
    if not path:
        return None
    try:
        return StateStore(path)
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"STATE_DB_PATH={path}: хранилище не открывается ({exc})") from exc
