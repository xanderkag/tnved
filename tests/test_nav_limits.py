"""Квоты канала site (nav_limits): 3 без почты / 10 с пропуском, потолок сервиса, пропуск,
сутки UTC, бесплатные ответы кнопками. Без сети и без модели."""
import hashlib
import hmac
import time
import unittest
from unittest import mock

import nav_limits
from nav_limits import LimitExceeded, Limits

SECRET = "test-secret"


def make_pass(vid="u1", ttl=3600, secret=SECRET, expires=None):
    exp = str(int(time.time()) + ttl) if expires is None else expires
    sig = hmac.new(secret.encode(), f"{vid}.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{vid}.{exp}.{sig}"


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.lim = Limits(free=3, email=10, cap=100, pass_secret=SECRET, trust_proxy=True)

    def test_three_without_email_then_429_with_email_hint(self):
        for n in (1, 2, 3):
            self.assertEqual(self.lim.take("ip:a", False), (n, 3))
        with self.assertRaises(LimitExceeded) as e:
            self.lim.take("ip:a", False)
        self.assertEqual(e.exception.status, 429)
        self.assertIn("Оставьте почту — будет 10", str(e.exception))

    def test_ten_with_email(self):
        for n in range(1, 11):
            self.assertEqual(self.lim.take("v:u1", True), (n, 10))
        with self.assertRaises(LimitExceeded) as e:
            self.lim.take("v:u1", True)
        self.assertEqual(e.exception.status, 429)
        self.assertIn("10 товаров", str(e.exception))

    def test_visitors_are_counted_separately(self):
        for _ in range(3):
            self.lim.take("ip:a", False)
        self.assertEqual(self.lim.take("ip:b", False), (1, 3))

    def test_service_cap_is_503_for_everyone(self):
        lim = Limits(free=3, email=10, cap=2, pass_secret=SECRET)
        lim.take("ip:a", False)
        lim.take("ip:b", False)
        with self.assertRaises(LimitExceeded) as e:
            lim.take("v:new", True)
        self.assertEqual(e.exception.status, 503)

    def test_refund_returns_slot_and_total(self):
        self.lim.take("ip:a", False)
        self.lim.refund("ip:a")
        self.assertEqual(self.lim.status("ip:a", False), (0, 3))
        self.assertEqual(self.lim.total, 0)
        self.lim.refund("ip:a")  # лишний возврат не уводит в минус
        self.assertEqual(self.lim.total, 0)

    def test_status_does_not_spend(self):
        self.lim.take("ip:a", False)
        for _ in range(5):
            self.assertEqual(self.lim.status("ip:a", False), (1, 3))

    def test_utc_day_rollover_resets_counters_and_bindings(self):
        with mock.patch.object(nav_limits.time, "strftime", return_value="2026-11-07"):
            for _ in range(3):
                self.lim.take("ip:a", False)
            self.lim.bind("r1", "товар")
        with mock.patch.object(nav_limits.time, "strftime", return_value="2026-11-08"):
            self.assertEqual(self.lim.take("ip:a", False), (1, 3))
            self.assertFalse(self.lim.follow("r1", "товар"))

    def test_disabled_when_no_free_and_no_cap(self):
        lim = Limits(free=0, email=10, cap=0)
        self.assertFalse(lim.enabled)
        self.assertEqual(lim.take("ip:a", False), (1, 0))

    def test_plural_forms(self):
        self.assertEqual([nav_limits._goods(n) for n in (1, 2, 5, 11, 21, 22, 112)],
                         ["1 товар", "2 товара", "5 товаров", "11 товаров", "21 товар", "22 товара", "112 товаров"])


class PassTests(unittest.TestCase):
    def setUp(self):
        self.lim = Limits(free=3, email=10, cap=100, pass_secret=SECRET, trust_proxy=True)

    def visitor(self, **headers):
        return self.lim.visitor({k.lower().replace("_", "-"): v for k, v in headers.items()}, "10.0.0.1")

    def test_valid_pass_gives_email_visitor(self):
        self.assertEqual(self.visitor(x_visitor_pass=make_pass()), ("v:u1", True))

    def test_expired_pass_falls_back_to_ip(self):
        self.assertEqual(self.visitor(x_visitor_pass=make_pass(ttl=-5), x_real_ip="203.0.113.7"),
                         ("ip:203.0.113.7", False))

    def test_wrong_signature_and_garbage_are_ignored(self):
        bad = make_pass(secret="other")
        for value in (bad, "u1", "u1.123", "u1.abc.def", "." * 3, make_pass(vid="x" * 129)):
            self.assertEqual(self.visitor(x_visitor_pass=value)[1], False, value)

    def test_no_secret_means_no_passes(self):
        lim = Limits(free=3, pass_secret="")
        self.assertEqual(lim.visitor({"x-visitor-pass": make_pass()}, "h"), ("ip:h", False))

    def test_proxy_headers_only_when_trusted(self):
        self.assertEqual(self.visitor(x_forwarded_for="1.1.1.1, 198.51.100.2"), ("ip:198.51.100.2", False))
        lim = Limits(free=3, trust_proxy=False)
        self.assertEqual(lim.visitor({"x-real-ip": "203.0.113.7"}, "10.0.0.1"), ("ip:10.0.0.1", False))


class FollowTests(unittest.TestCase):
    def test_answers_free_only_for_same_description_and_bounded(self):
        lim = Limits(free=3)
        lim.bind("r1", "кроссовки")
        self.assertFalse(lim.follow("r1", "другой товар"))
        self.assertFalse(lim.follow("r2", "кроссовки"))
        for _ in range(nav_limits.MAX_ANSWERS):
            self.assertTrue(lim.follow("r1", "кроссовки"))
        self.assertFalse(lim.follow("r1", "кроссовки"))


if __name__ == "__main__":
    unittest.main()
