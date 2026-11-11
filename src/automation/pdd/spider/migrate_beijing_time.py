"""Back up and migrate SQLite timestamps/defaults to Beijing ISO 8601."""
import argparse
from datetime import datetime
from pathlib import Path
import re
import sqlite3

from automation.pdd.spider.time_format import SQL_NOW, timestamp


def migrate(path):
    path = Path(path).resolve()
    backup = path.with_name(path.name + '.before-beijing-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    db = sqlite3.connect(path, timeout=30)
    try:
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        counts = {}
        for kind, table, sql in objects:
            if kind != 'table':
                continue
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            dates = [name for name in columns if name.endswith('_at')]
            if not dates:
                continue
            # Rebuild preserves every existing column, constraint and ID while
            # changing defaults without editing sqlite_master directly.
            temporary = '__beijing_' + table
            create = re.sub(r'CREATE TABLE\s+(?:IF NOT EXISTS\s+)?[\w"`\[\]]+', f'CREATE TABLE "{temporary}"', sql, count=1, flags=re.I)
            create = re.sub(r'\bCURRENT_TIMESTAMP\b', SQL_NOW, create, flags=re.I)
            db.execute(create)
            names = ','.join(f'"{name}"' for name in columns)
            db.execute(f'INSERT INTO "{temporary}" ({names}) SELECT {names} FROM "{table}"')
            rows = db.execute(f'SELECT rowid,{",".join(dates)} FROM "{temporary}"').fetchall()
            for row in rows:
                values = [timestamp(value) for value in row[1:]]
                db.execute(f'UPDATE "{temporary}" SET ' + ','.join(f'"{name}"=?' for name in dates) + ' WHERE rowid=?', [*values, row[0]])
            db.execute(f'DROP TABLE "{table}"')
            db.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
            counts[table] = len(rows)
        for kind, name, sql in objects:
            if kind in {'index', 'trigger'} and not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone():
                db.execute(re.sub(r'\bCURRENT_TIMESTAMP\b', SQL_NOW, sql, flags=re.I))
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Foreign key check failed')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Integrity check failed')
        db.commit()
        return backup, counts
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    backup, counts = migrate(args.database)
    print(f'Backup: {backup}')
    print(f'Migrated rows: {counts}')


if __name__ == '__main__':
    main()
