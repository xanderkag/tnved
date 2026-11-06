"""Спуск навигатора по дереву (navigator.navigate): done / question / порог, выбор позиции среди
кандидатов, «другая позиция», числовые границы правилом без модели, неизвестный путь, снятый код.
Дерево — маленькая база во временной папке, канал решений — функция-заглушка."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import conditions
import navigator

G64, H6404 = "ОБУВЬ", "ОБУВЬ С ПОДОШВОЙ ИЗ РЕЗИНЫ, ВЕРХОМ ИЗ ТЕКСТИЛЬНЫХ МАТЕРИАЛОВ"
G85, H8501 = "ЭЛЕКТРИЧЕСКИЕ МАШИНЫ", "ДВИГАТЕЛИ И ГЕНЕРАТОРЫ ЭЛЕКТРИЧЕСКИЕ"
LEAVES = [
    ("6404110000", f"{G64} → {H6404} → обувь спортивная → теннисная, баскетбольная", "tws"),
    ("6404110009", f"{G64} → {H6404} → обувь спортивная → прочая спортивная", "tws"),
    ("6404190000", f"{G64} → {H6404} → прочая", "hierarchy+tws"),
    ("6404990000", f"{G64} → {H6404} → снятая ветка", "hierarchy"),  # снят: только в дереве 2017
    ("8501100000", f"{G85} → {H8501} → мощностью не более 30 кВт", "tws"),
    ("8501200000", f"{G85} → {H8501} → мощностью более 30 кВт", "tws"),
]


def ans(choice, conf, probs=None, cost=0.0004):
    return SimpleNamespace(choice=choice, confidence=conf, probabilities=probs or {choice: conf}, cost_usd=cost)


class Decide:
    """Отвечает по очереди из script; каждый ответ — функция от criteria или готовый ответ."""

    def __init__(self, *script):
        self.script, self.seen = list(script), []

    async def __call__(self, state, instructions, criteria):
        self.seen.append((instructions, dict(criteria)))
        step = self.script.pop(0)
        return step(criteria) if callable(step) else step


def pick(word, conf=0.95):
    return lambda crit: ans(next(k for k in crit if word in k), conf)


class NavigatorWalkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "t.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE codes(code TEXT, description TEXT, level INT, parent_code TEXT, "
                        "full_path TEXT, duty_rate TEXT, data_source TEXT)")
            con.executemany("INSERT INTO codes VALUES (?,?,1,NULL,?,NULL,'hierarchy')",
                            [("64", G64, G64), ("6404", H6404, H6404), ("85", G85, G85), ("8501", H8501, H8501)])
        meta = [{"code": c, "description": p.split(" → ")[-1], "full_path": p, "duty_rate": "10%", "data_source": s}
                for c, p, s in LEAVES]
        with mock.patch.object(navigator, "DB_PATH", db), \
                mock.patch.object(navigator.notes, "load_from_env", return_value=None):
            cls.tree = navigator.Tree(SimpleNamespace(meta=meta))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_nav(self, decide, description="кроссовки", **kw):
        return asyncio.run(navigator.navigate(self.tree, description, decide, **kw))

    def path_6404(self):
        return self.tree.heading_path["6404"]

    def test_retired_code_never_enters_tree(self):
        self.assertNotIn("6404990000", self.tree.top.codes)
        self.assertEqual(len(self.tree.top.codes), 5)
        self.assertEqual(sorted(self.tree.roots), ["6404", "8501"])

    def test_done_when_confident_to_leaf(self):
        d = Decide(pick("спортивная"), pick("теннисная"))
        res = self.run_nav(d, path=self.path_6404())
        self.assertTrue(res["done"])
        self.assertEqual((res["code"], res["digits"], res["question"]), ("6404110000", 10, None))
        self.assertEqual([s["confidence"] for s in res["steps"]], [0.95, 0.95])
        self.assertAlmostEqual(res["cost_usd"], 0.0008)
        self.assertEqual([p["code"] for p in res["path_codes"]][-1], "6404110000")

    def test_single_option_descends_without_model(self):
        d = Decide()  # раздел → единственная группа → единственная позиция → правило по кВт
        res = self.run_nav(d, "двигатель 5 кВт", path=self.tree.heading_path["8501"][:1])
        self.assertEqual((res["code"], res["done"], d.seen), ("8501100000", True, []))
        self.assertEqual(res["path_codes"][-1]["code"], "8501100000")

    def test_below_threshold_stops_with_question(self):
        d = Decide(pick("прочая", conf=0.5))
        res = self.run_nav(d, path=self.path_6404())
        self.assertFalse(res["done"])
        self.assertEqual(res["code"], "6404")
        q = res["question"]
        self.assertEqual(q["path"], self.path_6404())
        self.assertEqual({o["code"] for o in q["options"]}, {"64041100", "6404190000"})
        for o in q["options"]:  # каждый вариант — путь, с которого продолжить
            self.assertEqual(o["path"][:-1], self.path_6404())
            self.assertTrue(o["short"])

    def test_threshold_is_inclusive(self):
        t = navigator.NAV_THRESHOLD
        self.assertIsNone(self.run_nav(Decide(pick("прочая", conf=t)), path=self.path_6404())["question"])
        self.assertIsNotNone(self.run_nav(Decide(pick("спортивная", conf=t - 0.001)),
                                          path=self.path_6404())["question"])

    def test_candidates_first_step_and_elsewhere(self):
        d = Decide(pick("6404"), pick("спортивная"), pick("теннисная"))
        res = self.run_nav(d, candidates=["6404", "8501", "9999"])
        self.assertTrue(res["done"])
        crit = d.seen[0][1]
        self.assertIn("другая позиция", crit)
        self.assertFalse(any(k.startswith("9999") for k in crit))  # позиции нет в дереве — не предлагаем
        d = Decide(ans("другая позиция", 0.9), pick("XVI"))
        res = self.run_nav(d, "двигатель 45 кВт", candidates=["6404"])
        self.assertEqual((res["code"], len(d.seen)), ("8501200000", 2))  # спуск от корня по разделам

    def test_uncertain_heading_choice_asks_from_top(self):
        res = self.run_nav(Decide(pick("6404", conf=0.4)), candidates=["6404", "8501"])
        self.assertIsNone(res["question"]["path"])
        self.assertEqual(res["path"], [])
        self.assertEqual(res["digits"], 0)

    def test_numeric_boundaries_are_decided_by_rule(self):
        path = self.tree.heading_path["8501"]
        for text, code in (("двигатель мощностью 30 кВт", "8501100000"),
                           ("двигатель мощностью 30,5 кВт", "8501200000"),
                           ("двигатель мощностью 0 кВт", "8501100000")):
            d = Decide()
            res = self.run_nav(d, text, path=path)
            self.assertEqual((res["code"], d.seen), (code, []), text)
            self.assertEqual(res["steps"][-1]["confidence"], 1.0)
            self.assertIn("rule", res["steps"][-1])

    def test_two_values_go_to_model(self):
        d = Decide(pick("не более", conf=0.97))
        res = self.run_nav(d, "двигатель 10 кВт и 50 кВт", path=self.tree.heading_path["8501"])
        self.assertEqual((res["code"], len(d.seen)), ("8501100000", 1))

    def test_unknown_path_is_keyerror(self):
        with self.assertRaises(KeyError):
            self.run_nav(Decide(), path=["нет такого узла"])

    def test_empty_node_is_done_without_model(self):
        res = self.run_nav(Decide(), path=self.path_6404() + ["прочая"])
        self.assertEqual((res["code"], res["done"], res["steps"]), ("6404190000", True, []))

    def test_other_option_gets_note(self):
        res = self.run_nav(Decide(pick("прочая", conf=0.3)), path=self.path_6404())
        other = next(o for o in res["question"]["options"] if o["label"] == "прочая")
        self.assertIn("обувь спортивная", other["note"])


class ConditionsBoundaryTests(unittest.TestCase):
    LABELS = ["мощностью не более 30 кВт", "мощностью более 30 кВт, но не более 75 кВт", "прочие"]

    def test_edges(self):
        self.assertEqual(conditions.choose(self.LABELS, "мотор 30 кВт")[0], 0)
        self.assertEqual(conditions.choose(self.LABELS, "мотор 30,01 кВт")[0], 1)
        self.assertEqual(conditions.choose(self.LABELS, "мотор 75 кВт")[0], 1)
        self.assertEqual(conditions.choose(self.LABELS, "мотор 75,1 кВт")[0], 2)
        self.assertIsNone(conditions.choose(self.LABELS, "мотор без указания мощности"))


if __name__ == "__main__":
    unittest.main()
