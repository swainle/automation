"""Simplify ranking_products without changing raw_json or stages_json."""
import argparse
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import sqlite3

RENAMES = {'state': 'rank_state', 'detail_state': 'product_state',
           'screenshot_state': 'product_screenshot_state', 'crop_state': 'ranking_crop_state',
           'detail_attempts': 'product_attempts'}
REMOVED = ('files_json', 'completed_at', 'device')


def migrate(path):
    path = Path(path).resolve()
    backup = path.with_name(path.name + '.before-product-fields-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    with sqlite3.connect(path, timeout=30) as db:
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        db.execute('PRAGMA foreign_keys=ON')
        before = db.execute('SELECT id,raw_json,stages_json FROM ranking_products ORDER BY id').fetchall()
        with db:
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(ranking_products)')}
            for old, new in RENAMES.items():
                if old in columns:
                    db.execute(f'ALTER TABLE ranking_products RENAME COLUMN {old} TO {new}')
            for column in REMOVED:
                if column in columns:
                    db.execute(f'ALTER TABLE ranking_products DROP COLUMN {column}')
            assert before == db.execute('SELECT id,raw_json,stages_json FROM ranking_products ORDER BY id').fetchall()
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Foreign key check failed')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Integrity check failed')
            db.execute('PRAGMA user_version=8')
    return {'backup': str(backup), 'products_preserved': len(before), 'json_unchanged': True}


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
