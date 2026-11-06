"""Канал решений (decisions.choose): разбор ответа, повреждённый ответ, timeout, ограниченный
retry, суточная квота шлюза. Шлюз подменён httpx MockTransport — сети и денег нет."""
import asyncio
import json
import unittest
from unittest import mock

import httpx2 as httpx

import decisions

CFG = decisions.DecisionsConfig(base_url="http://gateway.invalid/v1", api_key="k", model="jev")
CRIT = {"обувь": "обувь", "прочие": "прочие"}


def ok(choice="обувь", conf=0.93):
    return httpx.Response(200, json={"answers": {"q": {"choice": choice, "confidence": conf,
                                                       "probabilities": {"обувь": conf, "прочие": 1 - conf}}},
                                     "usage": {"cost": 0.0004}})


class Gateway:
    """Отдаёт ответы по очереди; последний повторяется. Считает вызовы и паузы."""

    def __init__(self, *answers):
        self.answers, self.calls, self.sleeps = list(answers), 0, []
        self.bodies = []

    def handler(self, request):
        self.calls += 1
        self.bodies.append(json.loads(request.content))
        a = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return a

    def run(self, budget=10.0):
        real = httpx.AsyncClient

        def client(**kw):
            return real(transport=httpx.MockTransport(self.handler), **kw)

        async def sleep(d):
            self.sleeps.append(d)

        with mock.patch.object(decisions.httpx, "AsyncClient", client), \
                mock.patch.object(decisions.asyncio, "sleep", sleep):
            return asyncio.run(decisions.choose(CFG, "кроссовки", "что это?", CRIT, retry_budget=budget))


class ChooseTests(unittest.TestCase):
    def test_parses_choice_probabilities_and_cost(self):
        g = Gateway(ok())
        c = g.run()
        self.assertEqual((c.choice, c.confidence, c.cost_usd), ("обувь", 0.93, 0.0004))
        self.assertAlmostEqual(c.probabilities["прочие"], 0.07)
        self.assertEqual(g.bodies[0]["questions"]["q"]["criteria"], CRIT)
        self.assertEqual(g.bodies[0]["model"], "jev")

    def test_choice_outside_list_is_unavailable(self):
        with self.assertRaisesRegex(decisions.DecisionsUnavailable, "вне списка"):
            Gateway(ok(choice="одежда")).run()

    def test_corrupted_answers_are_unavailable_not_keyerror(self):
        for resp in (httpx.Response(200, json={}), httpx.Response(200, json={"answers": {}}),
                     httpx.Response(200, json={"answers": {"q": None}}), httpx.Response(200, text="<html>"),
                     httpx.Response(200, json={"answers": {"q": {"choice": "обувь", "confidence": "высокая"}}})):
            with self.assertRaises(decisions.DecisionsUnavailable, msg=resp.content):
                Gateway(resp).run()

    def test_restart_502_is_retried_then_succeeds(self):
        g = Gateway(httpx.Response(502), httpx.Response(503), ok())
        self.assertEqual(g.run().choice, "обувь")
        self.assertEqual((g.calls, g.sleeps), (3, [1.0, 2.0]))

    def test_timeout_is_retried(self):
        g = Gateway(httpx.ReadTimeout("slow"), ok())
        self.assertEqual(g.run().choice, "обувь")
        self.assertEqual(g.calls, 2)

    def test_retry_is_bounded_by_budget(self):
        g = Gateway(httpx.Response(502))
        with self.assertRaisesRegex(decisions.DecisionsUnavailable, "HTTP 502"):
            g.run(budget=10)
        self.assertEqual(g.sleeps, [1.0, 2.0, 4.0, 8.0])  # следующая пауза 16 вышла бы за бюджет
        self.assertEqual(g.calls, 5)

    def test_zero_budget_means_single_attempt(self):
        g = Gateway(httpx.ConnectError("refused"))
        with self.assertRaisesRegex(decisions.DecisionsUnavailable, "ConnectError"):
            g.run(budget=0)
        self.assertEqual(g.calls, 1)

    def test_daily_quota_is_not_retried(self):
        g = Gateway(httpx.Response(429, json={"error": {"code": "quota_exceeded"}}))
        with self.assertRaises(decisions.DecisionsUnavailable) as e:
            g.run()
        self.assertTrue(e.exception.quota)
        self.assertEqual(g.calls, 1)

    def test_rate_429_is_retried(self):
        g = Gateway(httpx.Response(429, json={"error": {"code": "rate_limited"}}), ok())
        self.assertEqual(g.run().choice, "обувь")
        self.assertEqual(g.calls, 2)

    def test_other_http_errors_fail_fast(self):
        g = Gateway(httpx.Response(401, text="bad key"))
        with self.assertRaisesRegex(decisions.DecisionsUnavailable, "HTTP 401"):
            g.run()
        self.assertEqual(g.calls, 1)


if __name__ == "__main__":
    unittest.main()
