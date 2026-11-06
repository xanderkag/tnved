"""Контракт /api/navigate, /api/codes/{code}, /api/navigate/feedback и /stats: состояния
done/question/empty/error, лимиты канала site, кэш и нормализация, конкурирующие запросы,
отзывы и что хранится. Дерево, канал решений и подбор подменены — ни сети, ни денег."""
import asyncio
import hashlib
import hmac
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx2 as httpx
from fastapi.testclient import TestClient

import api
import decisions
import nav_journal
import nav_limits
import navigator

SECRET = "pass-secret"
SITE = {"X-Channel": "site", "X-Real-IP": "203.0.113.7"}
DESC = "Кроссовки беговые, верх текстиль, подошва резина"


def visitor_pass(vid="u1"):
    exp = str(int(time.time()) + 3600)
    return f"{vid}.{exp}." + hmac.new(SECRET.encode(), f"{vid}.{exp}".encode(), hashlib.sha256).hexdigest()


def done(code="6404110000"):
    return {"code": code, "digits": 10, "done": True, "path": ["a"], "path_codes": [], "steps": [],
            "question": None, "codes": [code], "n_codes": 1, "cost_usd": 0.0012}


def question(code="", digits=0):
    return {"code": code, "digits": digits, "done": False, "path": [], "path_codes": [], "steps": [{}],
            "question": {"path": None, "title": "?", "text": "?", "options": []},
            "codes": ["6404110000", "6404190000"], "n_codes": 2, "cost_usd": 0.0004}


class _Store:
    code_to_meta = {"6404110000": {"description": "обувь спортивная"},
                    "6404190000": {"description": "прочая"}}

    def code_card(self, code):
        return {"6404110000": {"code": code, "status": "current", "duty": {"rate": "10%", "source": "ЕТТ",
                                                                           "as_of": "30.10.2026"}},
                "8112924100": {"code": code, "status": "retired", "duty": None},
                "6404": {"code": code, "status": "not_leaf", "duty": None}}.get(code)


class _Tree:
    roots = {"6404"}


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (api.state.tree, api.state.dec, api.state.journal, api.state.prec, api.state.conf,
                      api.limits, api._require_store, navigator.navigate, navigator.candidate_headings)
        self.calls, self.result, self.delay = 0, done(), 0.0

        async def navigate(tree, description, decide, path=None, candidates=None):
            self.calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            r = self.result
            if isinstance(r, BaseException):
                raise r
            return dict(r)

        api.state.tree, api.state.dec, api.state.prec, api.state.conf = _Tree(), object(), None, None
        self.db = Path(self.tmp.name) / "nav.db"
        api.state.journal = nav_journal.Journal(str(self.db))
        api.limits = nav_limits.Limits(free=3, email=10, cap=100, pass_secret=SECRET, trust_proxy=True)
        api._require_store = lambda: _Store()
        navigator.navigate = navigate
        navigator.candidate_headings = lambda *a, **k: ["6404"]
        api._nav_cache.clear()
        self.client = TestClient(api.app, raise_server_exceptions=False)

    def tearDown(self):
        (api.state.tree, api.state.dec, api.state.journal, api.state.prec, api.state.conf,
         api.limits, api._require_store, navigator.navigate, navigator.candidate_headings) = self.saved
        api._nav_cache.clear()
        self.tmp.cleanup()

    def nav(self, description=DESC, headers=SITE, **body):
        return self.client.post("/api/navigate", json={"description": description, **body}, headers=headers)


class NavigateContractTests(ApiCase):
    def test_done_contract_on_site(self):
        r = self.nav()
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertRegex(j["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual((j["code"], j["digits"], j["done"], j["question"], j["cached"]),
                         ("6404110000", 10, True, None, False))
        self.assertEqual(j["codes"], [{"code": "6404110000", "name": "обувь спортивная"}])
        self.assertEqual(j["card"]["duty"], {"rate": "10%", "source": "ЕТТ", "as_of": "30.10.2026"})
        self.assertEqual(j["quota"], {"used": 1, "limit": 3, "email": False})
        self.assertNotIn("cost_usd", j)  # стоимость браузеру не отдаём

    def test_internal_channel_sees_cost_and_has_no_quota(self):
        j = self.nav(headers={}).json()
        self.assertEqual((j["cost_usd"], j["quota"]), (0.0012, None))

    def test_question_state_without_code_has_no_card(self):
        self.result = question()
        j = self.nav().json()
        self.assertEqual((j["done"], j["digits"], j["card"]), (False, 0, None))
        self.assertTrue(j["question"]["options"] is not None)
        self.assertEqual(len(j["codes"]), 2)

    def test_partial_code_gets_heading_card(self):
        self.result = question(code="6404", digits=4)
        self.assertEqual(self.nav().json()["card"]["status"], "not_leaf")

    def test_empty_and_overlong_description_422(self):
        self.assertEqual(self.nav("   ").status_code, 422)
        self.assertEqual(self.nav("x" * (api.NAV_MAX_DESCRIPTION_LEN + 1)).status_code, 422)
        self.assertEqual(self.calls, 0)
        self.assertEqual(api.limits.total, 0)

    def test_disabled_navigator_503(self):
        api.state.dec = None
        self.assertEqual(self.nav().status_code, 503)

    def test_gateway_down_is_503_and_refunded(self):
        self.result = decisions.DecisionsUnavailable("Канал решений jev не ответил: HTTP 502")
        r = self.nav()
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("Retry-After", r.headers)
        self.assertEqual(api.limits.status("ip:203.0.113.7", False), (0, 3))

    def test_gateway_quota_has_retry_after_utc(self):
        self.result = decisions.DecisionsUnavailable("лимит", quota=True)
        r = self.nav()
        self.assertEqual(r.status_code, 503)
        self.assertLessEqual(int(r.headers["Retry-After"]), 86400)

    def test_unknown_path_400_and_refunded(self):
        self.result = KeyError("узел")
        r = self.nav(path=["нет"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(api.limits.total, 0)

    def test_unexpected_error_500_and_refunded(self):
        self.result = RuntimeError("boom")
        self.assertEqual(self.nav().status_code, 500)
        self.assertEqual(api.limits.total, 0)

    def test_foreign_request_id_is_replaced(self):
        j = self.nav(request_id="not-ours").json()
        self.assertNotEqual(j["request_id"], "not-ours")


class CacheAndQuotaTests(ApiCase):
    def test_normalized_repeat_is_cached_and_free(self):
        first = self.nav("Кроссовки  беговые.").json()
        again = self.nav("кроссовки беговые").json()
        self.assertEqual((again["cached"], self.calls), (True, 1))
        self.assertEqual(again["quota"]["used"], 1)
        self.assertNotEqual(first["request_id"], again["request_id"])

    def test_different_path_is_not_the_same_cache_entry(self):
        self.nav()
        self.nav(path=["a"])
        self.assertEqual(self.calls, 2)

    def test_fourth_item_429_but_cached_repeat_still_served(self):
        for i in range(3):
            self.assertEqual(self.nav(f"товар {i}").status_code, 200)
        r = self.nav("товар 3")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertIn("Оставьте почту", r.json()["detail"])
        r = self.nav("Товар 0.")
        self.assertEqual((r.status_code, r.json()["cached"]), (200, True))

    def test_email_pass_gives_ten(self):
        h = SITE | {"X-Visitor-Pass": visitor_pass()}
        for i in range(10):
            self.assertEqual(self.nav(f"товар {i}", headers=h).status_code, 200)
        self.assertEqual(self.nav("товар 10", headers=h).status_code, 429)

    def test_answers_with_request_id_are_free_new_description_is_not(self):
        self.result = question()
        rid = self.nav().json()["request_id"]
        for step in range(5):
            r = self.nav(path=["a", str(step)], request_id=rid)
            self.assertEqual(r.json()["quota"], None)  # ответ кнопкой не засчитан
        self.assertEqual(api.limits.status("ip:203.0.113.7", False)[0], 1)
        self.nav("совсем другой товар", path=["a"], request_id=rid)
        self.assertEqual(api.limits.status("ip:203.0.113.7", False)[0], 2)

    def test_service_cap_503(self):
        api.limits = nav_limits.Limits(free=3, email=10, cap=1, pass_secret=SECRET, trust_proxy=True)
        self.nav("товар 1")
        r = self.client.post("/api/navigate", json={"description": "товар 2"},
                             headers={"X-Channel": "site", "X-Real-IP": "198.51.100.9"})
        self.assertEqual(r.status_code, 503)

    def test_internal_channel_unlimited(self):
        for i in range(6):
            self.assertEqual(self.nav(f"товар {i}", headers={}).status_code, 200)

    def test_concurrent_requests_cannot_overrun_quota(self):
        self.delay = 0.05

        async def burst():
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                return await asyncio.gather(*(c.post("/api/navigate", json={"description": f"товар {i}"},
                                                     headers=SITE) for i in range(6)))

        codes = sorted(r.status_code for r in asyncio.run(burst()))
        self.assertEqual(codes, [200, 200, 200, 429, 429, 429])
        self.assertEqual(self.calls, 3)


class CardTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.real_card = api._card

    def test_card_states(self):
        self.assertEqual(self.client.get("/api/codes/6404 11 000 0").json()["status"], "current")
        self.assertEqual(self.client.get("/api/codes/8112924100").json()["status"], "retired")
        self.assertEqual(self.client.get("/api/codes/6404").json()["status"], "not_leaf")
        self.assertEqual(self.client.get("/api/codes/9999999999").status_code, 404)
        for bad in ("640", "64041100001", "abc"):
            self.assertEqual(self.client.get(f"/api/codes/{bad}").status_code, 400, bad)

    def test_card_includes_conformity_when_loaded(self):
        api.state.conf = mock.Mock(lookup=lambda code: {"regs": ["ТР ТС 017/2011"], "as_of": "2026-10"})
        self.assertEqual(self.client.get("/api/codes/6404110000").json()["conformity"]["as_of"], "2026-10")


class FeedbackTests(ApiCase):
    def fb(self, rid, verdict="wrong", headers=SITE, **extra):
        return self.client.post("/api/navigate/feedback", headers=headers,
                                json={"request_id": rid, "verdict": verdict, "code": "6404110000", **extra})

    def test_validation(self):
        rid = self.nav().json()["request_id"]
        self.assertEqual(self.fb(rid, verdict="maybe").status_code, 422)
        self.assertEqual(self.fb(rid, expected_code="12345678901").status_code, 422)
        self.assertEqual(self.fb("0" * 32).status_code, 404)  # не выдавали
        self.assertEqual(self.fb("zzz").status_code, 404)
        self.assertEqual(self.client.post("/api/navigate/feedback", json={"verdict": "wrong"}).status_code, 422)

    def test_one_feedback_per_item_replaces_previous(self):
        rid = self.nav().json()["request_id"]
        self.assertEqual(self.fb(rid, "wrong", description=DESC).status_code, 200)
        self.assertEqual(self.fb(rid, "right").status_code, 200)
        with sqlite3.connect(self.db) as con:
            rows = con.execute("SELECT verdict, description FROM feedback").fetchall()
        self.assertEqual(rows, [("right", "")])

    def test_site_daily_cap(self):
        rid = self.nav().json()["request_id"]
        with mock.patch.object(nav_journal, "FEEDBACK_DAILY_CAP", 1):
            self.assertEqual(self.fb(rid, "right").status_code, 200)
            self.assertEqual(self.fb(rid, "right").status_code, 429)
            self.assertEqual(self.fb(rid, "right", headers={}).status_code, 200)  # внутренний — без потолка

    def test_storage_keeps_no_description_until_feedback_and_trims_it(self):
        self.nav(DESC + " уникальнаяметка")
        raw = b"".join(p.read_bytes() for p in Path(self.tmp.name).glob("nav.db*"))
        self.assertNotIn("уникальнаяметка".encode(), raw)
        self.assertNotIn(b"203.0.113.7", raw)  # адрес посетителя не храним
        rid = self.nav("другое").json()["request_id"]
        self.fb(rid, description="д" * 5000, comment="к" * 900)
        with sqlite3.connect(self.db) as con:
            d, c = con.execute("SELECT description, comment FROM feedback").fetchone()
        self.assertEqual((len(d), len(c)), (api.NAV_MAX_DESCRIPTION_LEN, 500))

    def test_stats_internal_only(self):
        self.nav()
        self.assertEqual(self.client.get("/api/navigate/stats", headers=SITE).status_code, 403)
        st = self.client.get("/api/navigate/stats").json()
        self.assertEqual(st["channels"]["site"]["items"], 1)

    def test_journal_off_is_503(self):
        rid = self.nav().json()["request_id"]
        api.state.journal = None
        self.assertEqual(self.fb(rid).status_code, 503)


if __name__ == "__main__":
    unittest.main()
