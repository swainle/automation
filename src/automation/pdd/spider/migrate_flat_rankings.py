"""Remove parent rankings and parent_id, preserving real ranking/product IDs."""
import argparse
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import sqlite3


def flatten(db):
    if db.execute('SELECT 1 FROM ranking_products WHERE ranking_id<0').fetchone():
        raise RuntimeError('负数榜单有关联商品，需先核对，未删除')
    columns = {row[1] for row in db.execute('PRAGMA table_info(rankings)')}
    if 'parent_id' in columns:
        db.execute('DROP INDEX IF EXISTS idx_rankings_parent')
        db.execute('ALTER TABLE rankings DROP COLUMN parent_id')
    count = db.execute('DELETE FROM rankings WHERE id<0').rowcount
    db.execute('PRAGMA user_version=9')
    return count


def migrate(path):
    path = Path(path).resolve()
    backup = path.with_name(path.name + '.before-flat-rankings-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    with sqlite3.connect(path, timeout=30) as db:
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        db.execute('PRAGMA foreign_keys=ON')
        before = db.execute('SELECT * FROM ranking_products ORDER BY id').fetchall()
        with db:
            db.execute('BEGIN IMMEDIATE')
            removed = flatten(db)
            assert before == db.execute('SELECT * FROM ranking_products ORDER BY id').fetchall()
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Foreign key check failed')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Integrity check failed')
    return {'backup': str(backup), 'negative_rankings_deleted': removed, 'products_preserved': len(before)}


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
