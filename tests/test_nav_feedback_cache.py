"""Отзыв «не подошёл» снимает ответ из кэша навигатора (Q-P1-TN-VED-2). Без модели и сети:
дерево, канал решений и сам подбор подменены, считается только, сколько раз решали заново."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import api
import nav_journal
import nav_limits
import navigator

SITE = {"X-Channel": "site", "X-Real-IP": "203.0.113.7"}


class _Store:
    code_to_meta = {"6404110000": {"description": "обувь спортивная"}}


class _Tree:
    roots = {"6404"}


class NavFeedbackCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (api.state.tree, api.state.dec, api.state.journal, api.state.prec, api.limits,
                      api._require_store, api._card, navigator.navigate, navigator.candidate_headings)
        self.calls = 0

        async def navigate(tree, description, decide, path=None, candidates=None):
            self.calls += 1
            return {"code": "6404110000", "digits": 10, "done": True, "path": [], "path_codes": [],
                    "steps": [], "question": None, "codes": ["6404110000"], "cost_usd": 0.001}

        api.state.tree, api.state.dec, api.state.prec = _Tree(), object(), None
        api.state.journal = nav_journal.Journal(str(Path(self.tmp.name) / "nav.db"))
        api.limits = nav_limits.Limits(free=3, email=10, cap=100)
        api._require_store = lambda: _Store()
        api._card = lambda store, code: {"code": code}
        navigator.navigate = navigate
        navigator.candidate_headings = lambda *a, **k: []
        api._nav_cache.clear()
        self.client = TestClient(api.app)

    def tearDown(self):
        (api.state.tree, api.state.dec, api.state.journal, api.state.prec, api.limits,
         api._require_store, api._card, navigator.navigate, navigator.candidate_headings) = self.saved
        api._nav_cache.clear()
        self.tmp.cleanup()

    def ask(self, description="Кроссовки беговые, верх текстиль, подошва резина"):
        r = self.client.post("/api/navigate", json={"description": description}, headers=SITE)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def feedback(self, rid, verdict, **extra):
        r = self.client.post("/api/navigate/feedback", headers=SITE,
                             json={"request_id": rid, "verdict": verdict, "code": "6404110000", **extra})
        self.assertEqual(r.status_code, 200, r.text)

    def test_wrong_drops_cached_answer(self):
        first = self.ask()
        again = self.ask()
        self.assertTrue(again["cached"])
        self.assertEqual(self.calls, 1)
        self.feedback(again["request_id"], "wrong")  # отзыв пришёл на кешированный ответ, как в проде
        third = self.ask()
        self.assertFalse(third["cached"])
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(third["request_id"], first["request_id"])

    def test_wrong_survives_restart(self):
        rid = self.ask()["request_id"]
        api._nav_cache.update(api.state.journal.cache_load(api.NAV_CACHE_SIZE))
        self.feedback(rid, "wrong")
        api._nav_cache.clear()  # перезапуск: кэш поднимается из журнала
        api._nav_cache.update(api.state.journal.cache_load(api.NAV_CACHE_SIZE))
        self.assertEqual(len(api._nav_cache), 0)
        self.assertFalse(self.ask()["cached"])

    def test_wrong_on_old_step_without_key_uses_description(self):
        description = "Кроссовки беговые, верх текстиль, подошва резина"
        rid = self.ask(description)["request_id"]
        with api.state.journal._lock:  # шаг, записанный до 03.11, ключа кэша не помнит
            api.state.journal._db.execute("UPDATE steps SET k = NULL WHERE rid = ?", (rid,))
        self.feedback(rid, "wrong", description=description + ".")
        self.assertFalse(self.ask(description)["cached"])

    def test_wrong_keeps_other_descriptions(self):
        rid = self.ask("Кроссовки беговые")["request_id"]
        self.ask("Сапоги резиновые")
        self.feedback(rid, "wrong")
        self.assertTrue(self.ask("Сапоги резиновые")["cached"])

    def test_right_keeps_cache_and_limits(self):
        first = self.ask()
        self.assertEqual(first["quota"]["used"], 1)
        self.feedback(first["request_id"], "right")
        again = self.ask()
        self.assertTrue(again["cached"])
        self.assertEqual(again["quota"]["used"], 1)  # повтор из кэша лимит не тратит
        self.assertEqual(self.calls, 1)

    def test_recomputed_answer_counts_against_limit(self):
        first = self.ask()
        self.feedback(first["request_id"], "wrong")
        self.assertEqual(self.ask()["quota"]["used"], 2)
        self.ask("Сапоги резиновые")
        r = self.client.post("/api/navigate", json={"description": "Тапочки войлочные"}, headers=SITE)
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main()
