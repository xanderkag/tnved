"""Пошлина и НДС в карточке: у каждой ставки источник и дата (KONTRAKT-2), сноски ЕТТ по датам,
редакция в будущем, отказ показывать ставку снятого кода, условный НДС 10 %."""
import datetime as dt
import unittest

import ett
import tnved_data
import vat

NOTE = ("Ставка ввозной таможенной пошлины в размере 0 (ноль) % от таможенной стоимости применяется "
        "с 01.01.2026 по 31.12.2026 включительно.")
NOTE_RU = ("Ставка ввозной таможенной пошлины в размере 5 % от таможенной стоимости применяется "
           "Российской Федерацией с 01.01.2026 по 31.12.2027 включительно.")
RATES = ett.Rates({"fetched": "30.10.2026", "notes": {"1С": NOTE, "2С": NOTE_RU, "3С": "Примечание утратило силу."},
                   "rates": {
                       "1111111111": {"rate": "10", "notes": ["1С"], "source": "ru.11_2022_01.10.2026.pdf"},
                       "2222222222": {"rate": "15", "notes": ["2С"], "source": "ru.22_2022_01.10.2026.pdf"},
                       "3333333333": {"rate": "7", "notes": [], "source": "ru.33_2022_01.12.2026.pdf"},
                       "4444444444": {"rate": "25, но не менее 0,2 евро за 1 кг", "notes": ["3С"],
                                      "source": "ru.44_2022.pdf"},
                   }})
TODAY = dt.date(2026, 11, 7)


class DutyTests(unittest.TestCase):
    def test_note_inside_window_changes_rate_and_keeps_base(self):
        d = RATES.duty("1111111111", TODAY)
        self.assertEqual((d["rate"], d["base"], d["as_of"], d["edition"]), ("0%", "10%", "30.10.2026", "01.10.2026"))
        self.assertEqual(d["source"], ett.SOURCE)
        self.assertEqual(d["notes"][0]["mark"], "1С")

    def test_note_outside_window_keeps_base(self):
        d = RATES.duty("1111111111", dt.date(2027, 1, 1))
        self.assertEqual(d["rate"], "10%")
        self.assertNotIn("base", d)

    def test_russia_only_rate(self):
        d = RATES.duty("2222222222", TODAY)
        self.assertEqual((d["rate"], d["rate_ru"]), ("15%", "5%"))

    def test_future_edition_is_not_shown(self):
        self.assertIsNone(RATES.duty("3333333333", TODAY))
        self.assertEqual(RATES.duty("3333333333", dt.date(2026, 12, 1))["rate"], "7%")

    def test_combined_rate_and_dead_note(self):
        d = RATES.duty("4444444444", TODAY)
        self.assertEqual(d["rate"], "25%, но не менее 0,2 евро за 1 кг")
        self.assertIsNone(d["edition"])

    def test_unknown_code(self):
        self.assertIsNone(RATES.duty("5555555555", TODAY))


class StoreDutyTests(unittest.TestCase):
    def setUp(self):
        self.store = object.__new__(tnved_data.TNVEDStore)
        self.store.ett = RATES

    def test_ett_first(self):
        d = self.store._duty({"code": "1111111111", "duty_rate": "10%", "data_source": "tws"}, "29.09.2026")
        self.assertEqual(d["source"], ett.SOURCE)

    def test_tws_fallback_has_source_and_as_of(self):
        d = self.store._duty({"code": "6404110000", "duty_rate": "10%", "data_source": "tws"}, "29.09.2026")
        self.assertEqual(d, {"rate": "10%", "source": tnved_data.DUTY_SOURCE, "as_of": "29.09.2026"})

    def test_retired_or_rateless_code_has_no_duty(self):
        for src in ("hierarchy", "eec-retired"):
            self.assertIsNone(self.store._duty({"code": "6404990000", "duty_rate": "10%", "data_source": src}, "x"))
        self.assertIsNone(self.store._duty({"code": "6404110000", "duty_rate": None, "data_source": "tws"}, "x"))


class VatTests(unittest.TestCase):
    V = vat.Vat({"fetched": "2026-10-20", "lists": {
        "food": {"title": "Перечень продовольственных", "act": "ПП № 908", "edition": "2025"},
        "kids": {"title": "Перечень детских", "act": "ПП № 908", "edition": "2025"}},
        "entries": [
            {"list": "food", "from": "0401000000", "to": "0401999999", "except": [], "spec": "0401",
             "name": "молоко", "group": "молочные", "partial": False},
            {"list": "food", "from": "0407110000", "to": "0407110000", "except": [], "spec": "0407 11 000 0",
             "name": "яйца для инкубации", "group": "яйца", "partial": False},
            {"list": "kids", "from": "6404110000", "to": "6404199999", "except": ["6404190000"], "spec": "из 6404",
             "name": "обувь детская", "group": "обувь", "partial": True}]})

    def test_standard_rate_always_with_source(self):
        v = self.V.vat("8501100000")
        self.assertEqual(v, {"rate": vat.RATE, "source": vat.SOURCE})

    def test_reduced_is_conditional_with_list_and_date(self):
        v = self.V.vat("6404110000")
        self.assertEqual(v["rate"], "22%")  # основная ставка остаётся, 10 % — только при условии
        r = v["reduced"][0]
        self.assertEqual((r["rate"], r["partial"], r["act"]), ("10%", True, "ПП № 908"))
        self.assertIn("товары для детей", r["condition"])
        self.assertEqual((v["reduced_source"], v["as_of"]), (vat.REDUCED_SOURCE, "2026-10-20"))

    def test_exception_and_any_purpose(self):
        self.assertNotIn("reduced", self.V.vat("6404190000"))
        self.assertIn("пищевых", self.V.vat("0401100000")["reduced"][0]["condition"])
        self.assertIn("цель ввоза не важна", self.V.vat("0407110000")["reduced"][0]["condition"])


if __name__ == "__main__":
    unittest.main()
