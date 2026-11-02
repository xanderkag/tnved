import sqlite3
import tempfile
import unittest
from pathlib import Path
from reference_catalog import add_missing_headings, repair_db, export_reference

class ReferenceCatalogTests(unittest.TestCase):
    def test_adds_named_ancestor_without_changing_leaf(self):
        leaf = dict(code='8806100011', description='Example leaf', data_source='tws', duty_rate='10')
        got = add_missing_headings([leaf])
        self.assertEqual(got[0],leaf)
        self.assertEqual(got[1]['code'],'8806')
        self.assertEqual(got[1]['description'],'Беспилотные летательные аппараты')
        self.assertEqual(len(add_missing_headings(got)),2)

    def test_repair_is_idempotent_and_preserves_rate_and_status(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'test.db'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE codes(code TEXT PRIMARY KEY,description TEXT,level INT,parent_code TEXT,full_path TEXT,duty_rate TEXT,data_source TEXT)')
                con.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
                con.executemany('INSERT INTO codes VALUES (?,?,?,?,?,?,?)',[
                    ('88','Летательные аппараты',1,None,'Летательные аппараты',None,'hierarchy'),
                    ('8806100011','вертолеты',4,'88','Летательные аппараты → вертолеты','10','tws')])
                con.execute('INSERT INTO meta VALUES (?,?)',('tariff_as_of','29.09.2026'))
            self.assertEqual(repair_db(db),['8806'])
            self.assertEqual(repair_db(db),[])
            with sqlite3.connect(db) as con:
                row=con.execute('SELECT description,parent_code,full_path,duty_rate,data_source FROM codes WHERE code=?',('8806100011',)).fetchone()
                self.assertEqual(row,('вертолеты','8806','Летательные аппараты → Беспилотные летательные аппараты → вертолеты','10','tws'))
                self.assertEqual(dict(con.execute('SELECT key,value FROM meta'))['tariff_as_of'],'29.09.2026')
            result=export_reference(db)
            self.assertEqual(result['groups'][0]['headings'][0]['code'],'8806')
            self.assertEqual(result['leaf_tariff_as_of'],'29.09.2026')

if __name__=='__main__':unittest.main()
