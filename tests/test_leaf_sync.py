import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import leaf_sync
import tnved_data

OLD_PATH = "Металлы → прочие → ниобий (колумбий)"


def _fixture(folder: Path) -> tuple[Path, Path]:
    db, rates = folder / "t.db", folder / "rates.json"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE codes(code TEXT PRIMARY KEY,description TEXT NOT NULL,level INT NOT NULL,"
                    "parent_code TEXT,full_path TEXT,duty_rate TEXT,data_source TEXT)")
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        con.executemany("INSERT INTO codes VALUES (?,?,?,?,?,?,?)", [
            ("811292", "ПРОЧИЕ", 3, "8112", "Металлы → ПРОЧИЕ", None, "hierarchy"),
            ("8112924100", "ниобий (колумбий)", 4, "811292", OLD_PATH, "10%", "tws"),
            ("8112928100", "индий", 4, "811292", "Металлы → прочие → индий", "10%", "hierarchy+tws"),
        ])
    rates.write_text(json.dumps({"rates": {
        "8112924101": {"rate": "10", "notes": ["127С"]},
        "8112924109": {"rate": "10", "notes": []},
        "8112928100": {"rate": "10", "notes": []}}}), encoding="utf8")
    return db, rates


class LeafSyncTests(unittest.TestCase):
    def test_audit_finds_split_and_apply_fixes_it_once(self):
        with tempfile.TemporaryDirectory() as d:
            db, rates = _fixture(Path(d))
            self.assertEqual(leaf_sync.audit(db, rates),
                             {"not_in_ett": ["8112924100"], "missing_leaves": ["8112924101", "8112924109"]})
            self.assertEqual(leaf_sync.apply(db, rates_path=rates),
                             ["81129241", "8112924100", "8112924101", "8112924109"])
            self.assertEqual(leaf_sync.apply(db, rates_path=rates), [])
            self.assertEqual(leaf_sync.audit(db, rates), {"not_in_ett": [], "missing_leaves": []})
            with sqlite3.connect(db) as con:
                rows = {r[0]: r[1:] for r in con.execute(
                    "SELECT code,parent_code,full_path,duty_rate,data_source FROM codes")}
                meta = dict(con.execute("SELECT key,value FROM meta"))
            self.assertEqual(rows["8112924100"][3], "eec-retired")
            self.assertEqual(rows["8112924101"][:2], ("81129241", OLD_PATH + " → ниобий-алюминиевая лигатура "
                                                      "для производства титановых слитков, сплавов и слябов"))
            self.assertEqual(rows["8112924109"][2:], ("10%", "eec"))
            self.assertEqual(rows["8112928100"], ("811292", "Металлы → прочие → индий", "10%", "hierarchy+tws"))
            self.assertEqual(meta["leaf_sync_source"], "ru.81_2022_08.10.2026.pdf")
            self.assertFalse(list(Path(d).glob("*.tmp")))

    def test_card_status_after_sync(self):
        with tempfile.TemporaryDirectory() as d:
            db, rates = _fixture(Path(d))
            leaf_sync.apply(db, rates_path=rates)
            store = object.__new__(tnved_data.TNVEDStore)
            with mock.patch.object(tnved_data, "DB_PATH", db):
                self.assertEqual(store.code_status("8112924100")[0], "retired")
                self.assertEqual(store.code_status("8112924101")[0], "current")
                self.assertEqual(store.code_status("8112924109")[0], "current")

    def test_registry_refuses_code_that_ett_still_has(self):
        with tempfile.TemporaryDirectory() as d:
            db, rates = _fixture(Path(d))
            data = json.loads(rates.read_text())
            data["rates"]["8112924100"] = {"rate": "10"}
            rates.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                leaf_sync.apply(db, rates_path=rates)

    def test_navigator_puts_new_leaves_under_niobium(self):
        import navigator
        with tempfile.TemporaryDirectory() as d:
            db, rates = _fixture(Path(d))
            leaf_sync.apply(db, rates_path=rates)
            with sqlite3.connect(db) as con:
                con.row_factory = sqlite3.Row
                meta = [dict(r) for r in con.execute(
                    "SELECT code,description,full_path,duty_rate,data_source FROM codes WHERE level>=3")]
            store = mock.Mock(meta=meta)
            with mock.patch.object(navigator, "DB_PATH", db), \
                    mock.patch.object(navigator.notes, "load_from_env", return_value=None):
                tree = navigator.Tree(store)
            self.assertNotIn("8112924100", tree.top.codes)
            node = tree.walk(tree.heading_path["8112"] + OLD_PATH.split(" → ")[1:])
            self.assertEqual(sorted(node.codes), ["8112924101", "8112924109"])


if __name__ == "__main__":
    unittest.main()
