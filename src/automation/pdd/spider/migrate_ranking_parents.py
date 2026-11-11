"""Merge ranking_sources into rankings using parent_id, preserving child IDs."""
import argparse
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import sqlite3


def insert_source(db, source):
    row = db.execute("SELECT id FROM rankings WHERE parent_id IS NULL AND tab_name='' AND capture_date=? AND source_name=?",
                     (source['capture_date'], source['source_name'])).fetchone()
    if row:
        return row[0]
    # Keep existing subranking IDs and product foreign keys intact. A separate
    # ID range also lets old checkpoints continue to address the same lists.
    owner_id = db.execute('SELECT MIN(COALESCE(MIN(id),0),0)-1 FROM rankings').fetchone()[0]
    columns = ['capture_date','source_name','source_url','source_order','state','last_error','created_at','updated_at']
    values = {key: source[key] for key in columns if key in source}
    db.execute(f"INSERT INTO rankings(id,parent_id,tab_name,{','.join(values)}) VALUES(?,NULL,'',{','.join('?' for _ in values)})",
               [owner_id, *values.values()])
    return owner_id


def attach_children(db):
    for row in db.execute("SELECT DISTINCT capture_date,source_name,source_url,source_order FROM rankings WHERE tab_name!=''").fetchall():
        source = dict(zip(('capture_date','source_name','source_url','source_order'), row))
        parent = insert_source(db, source)
        db.execute("UPDATE rankings SET parent_id=? WHERE capture_date=? AND source_name=? AND tab_name!=''",
                   (parent, source['capture_date'], source['source_name']))


def merge_sources(db):
    count = 0
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='ranking_sources'").fetchone():
        cursor = db.execute('SELECT * FROM ranking_sources ORDER BY id')
        names = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            insert_source(db, dict(zip(names, row)))
            count += 1
        attach_children(db)
        db.execute('DROP TABLE ranking_sources')
    else:
        attach_children(db)
    db.execute('CREATE INDEX IF NOT EXISTS idx_rankings_parent ON rankings(parent_id,tab_index)')
    return count


def migrate(path):
    from automation.pdd.spider.database import Store
    path = Path(path).resolve()
    backup = path.with_name(path.name + '.before-ranking-parents-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    with sqlite3.connect(path) as db:
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
    store = Store(path, migrating=True)
    try:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            before = store.db.execute('SELECT COUNT(*) FROM ranking_products').fetchone()[0]
            count = merge_sources(store.db)
            if store.db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Foreign key check failed')
            if store.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Integrity check failed')
            assert store.db.execute('SELECT COUNT(*) FROM ranking_products').fetchone()[0] == before
        return {'backup': str(backup), 'sources_merged': count, 'products_preserved': before}
    finally:
        store.close()


def main():
    from automation.pdd.spider.concurrency import workflow_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    with ExitStack() as locks:
        for role in ('rank','product','mobile','device-ios','device-android','device-browser'):
            locks.enter_context(workflow_lock(role, timeout=0))
        print(migrate(args.database))


if __name__ == '__main__':
    main()
