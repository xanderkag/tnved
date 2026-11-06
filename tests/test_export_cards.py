import unittest
from datetime import datetime, timezone
from pathlib import Path

import export_cards

WORDSTAT = Path.home() / "AgentMail/attachments/tnved-spros-wordstat-20261029.csv"


class Store:
    def code_card(self, code):
        if code == "0000000000":
            return None
        if code == "1111111111":
            raise RuntimeError("boom")
        return {"code": code, "status": "current", "duty": None, "vat": None}

    def version(self):
        return {"tariff_as_of": "29.09.2026"}


class Conf:
    def lookup(self, code):
        return None


class ExportCardsTests(unittest.TestCase):
    def test_wordstat_parsing_takes_only_ten_digit_codes(self):
        text = "вид,товар_или_код,частота_в_месяц\nцифры,3926909709,5\nтовар,игрушки,4\nцифры,6404,3\nцифры,3926 909 709,1\n"
        self.assertEqual(export_cards.wordstat_codes(text), ["3926909709"])

    @unittest.skipUnless(WORDSTAT.exists(), "нет выгрузки Wordstat")
    def test_real_wordstat_has_45_codes(self):
        self.assertEqual(len(export_cards.wordstat_codes(WORDSTAT.read_text(encoding="utf-8-sig"))), 45)

    def test_nulls_and_errors_are_explicit_and_revision_is_stable(self):
        now = datetime(2026, 11, 7, tzinfo=timezone.utc)
        a = export_cards.build(["6404110000", "0000000000", "1111111111"], Store(), Conf(), now)
        b = export_cards.build(["6404110000", "0000000000", "1111111111"], Store(), Conf(),
                               datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(a["revision"], b["revision"])
        ok, missing, broken = a["codes"]
        self.assertEqual(ok["card"]["conformity"], None)
        self.assertIn("conformity", ok["card"])
        self.assertEqual((missing["status"], missing["card"], missing["error"]),
                         ("unknown", None, "Кода нет в справочнике"))
        self.assertEqual(broken["error"], "RuntimeError: boom")
        self.assertEqual(a["sources"], {"tariff_as_of": "29.09.2026"})


if __name__ == "__main__":
    unittest.main()
