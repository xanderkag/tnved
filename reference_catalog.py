"""Canonical, source-backed group/heading names and first-wave site export.

The website consumes this platform export, never a separately maintained taxonomy.
Leaf eligibility remains the existing CURRENT_SOURCES predicate. EEC tables
supply names and validate the set of 4-digit ancestors, not duty/tax amounts.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
REFERENCE = BASE / 'references/eec-heading-names-2026-11-02.json'
CURRENT_SOURCES = ('hierarchy+tws', 'tws', 'eec')


def load_reference():
    data = json.loads(REFERENCE.read_text(encoding='utf8'))
    groups = {g['code']: g for g in data['groups']}
    headings = {code: dict(h, source=g['source'], sha256=g['sha256'])
                for g in data['groups'] for code, h in g['headings'].items()}
    return data, groups, headings


def add_missing_headings(rows):
    """Keep all existing records; supply absent named ancestors before path building."""
    _, _, headings = load_reference()
    known = {r['code'] for r in rows}
    needed = {r['code'][:4] for r in rows
              if len(r['code']) == 10 and r.get('data_source') in CURRENT_SOURCES}
    missing = needed - known
    if missing - headings.keys():
        raise ValueError(f'No official name for headings: {sorted(missing-headings.keys())}')
    return list(rows) + [dict(code=c, description=headings[c]['name'], duty_rate=None,
                             data_source='hierarchy') for c in sorted(missing)]


def repair_db(path):
    """Atomic, idempotent insertion of missing ancestors. Leaves/rates stay intact."""
    path = Path(path)
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        original = [dict(r) for r in con.execute('SELECT * FROM codes')]
    repaired = add_missing_headings(original)
    original_codes = {r['code'] for r in original}
    additions = [r for r in repaired if r['code'] not in original_codes]
    if not additions:
        return []
    tmp = path.with_name(path.name+'.reference-repair.tmp')
    shutil.copy2(path, tmp)
    names = {r['code']:r['description'] for r in repaired}
    added = {r['code'] for r in additions}
    with sqlite3.connect(tmp) as con:
        for row in additions:
            c = row['code']; full = names[c[:2]]+' → '+row['description']
            con.execute('INSERT INTO codes VALUES (?,?,?,?,?,?,?)',
                        (c,row['description'],2,c[:2],full,None,'hierarchy'))
        for row in original:
            c = row['code']
            if len(c)<=4 or c[:4] not in added:
                continue
            parent = next((c[:n] for n in (8,6,4,2) if n<len(c) and c[:n] in names),None)
            path_parts = [s.strip() for s in (row['full_path'] or '').split('→') if s.strip()]
            heading = names[c[:4]]
            if heading.casefold() not in {s.casefold().rstrip(':') for s in path_parts}:
                path_parts.insert(1 if path_parts else 0,heading)
            con.execute('UPDATE codes SET parent_code=?,full_path=? WHERE code=?',
                        (parent,' → '.join(path_parts),c))
        data,_,_ = load_reference()
        for k,v in [('reference_names_as_of',data['checked_at']),('reference_names_revision',data['revision']),
                    ('total',str(len(repaired)))]:
            con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(k,v))
        assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    os.replace(tmp,path)
    return sorted(added)


def export_reference(path):
    data,groups,headings = load_reference()
    with sqlite3.connect(f'file:{Path(path)}?mode=ro',uri=True) as con:
        names = dict(con.execute('SELECT code,description FROM codes'))
        meta = dict(con.execute('SELECT key,value FROM meta'))
        leaf_rows = list(con.execute('SELECT code,description,full_path FROM codes WHERE length(code)=10 ORDER BY code'))
        leaves = {c for c,s in con.execute('SELECT code,data_source FROM codes')
                  if len(c)==10 and s in CURRENT_SOURCES}
    wanted = {c[:4] for c in leaves}
    if wanted-headings.keys() or wanted-names.keys():
        raise ValueError('Platform ancestors missing; apply repair before site export')
    group_codes = sorted({c[:2] for c in leaves})
    official = {c for g in groups.values() for c in g['leafs']}
    examples = {}
    for code, name, path in leaf_rows:
        if code in leaves and code in official:
            bucket = examples.setdefault(code[:4], [])
            if len(bucket) < 5:
                bucket.append(dict(code=code,name=name,path=path))
    return {'schema_version':1,'revision':data['revision'],'names_checked_at':data['checked_at'],
            'leaf_tariff_as_of':meta.get('tariff_as_of'),
            'source_index':data['source_index'],
            'groups':[dict(code=c,name=groups[c]['name'],source=groups[c]['source'],
                           source_sha256=groups[c]['sha256'],
                           headings=[dict(code=h,name=headings[h]['name'],page=headings[h]['page'],examples=examples.get(h,[]))
                                     for h in sorted(wanted) if h.startswith(c)]) for c in group_codes]}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=BASE/'data/tnved.db')
    parser.add_argument('--repair-db',action='store_true')
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    if args.repair_db:
        print('Inserted headings: '+', '.join(repair_db(args.db)))
    if args.out:
        payload=export_reference(args.db)
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(f"Exported {len(payload['groups'])} groups and {sum(len(g['headings']) for g in payload['groups'])} headings to {args.out}")
