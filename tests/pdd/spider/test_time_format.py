from datetime import datetime
import sqlite3

from automation.pdd.spider.time_format import timestamp
from automation.pdd.spider.migrate_beijing_time import migrate


def test_timestamp_converts_utc_and_preserves_beijing_and_date():
    assert timestamp('2026-11-04 05:00:00') == '2026-11-04T13:00:00+08:00'
    assert timestamp('2026-11-04T13:00:00+08:00') == '2026-11-04T13:00:00+08:00'
    assert timestamp('2026-11-04') == '2026-11-04'
    assert timestamp('1793771833') == '2026-11-04T13:57:13+08:00'
    assert timestamp(None) is None


def test_migration_preserves_ids_relations_and_updates_defaults(tmp_path):
    path = tmp_path / 'test.db'
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE parent(id INTEGER PRIMARY KEY,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id REFERENCES parent(id),updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX child_parent ON child(parent_id);
        INSERT INTO parent VALUES(42,'2026-11-04 05:00:00');
        INSERT INTO child VALUES(7,42,'2026-11-04 05:00:00');""")
    backup, counts = migrate(path)
    assert backup.is_file() and counts == {'parent': 1, 'child': 1}
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM child').fetchone() == (7, 42, '2026-11-04T13:00:00+08:00')
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        db.execute('INSERT INTO parent(id) VALUES(43)')
        assert datetime.fromisoformat(db.execute('SELECT created_at FROM parent WHERE id=43').fetchone()[0]).utcoffset().total_seconds() == 28800
    migrate(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT created_at FROM parent WHERE id=42').fetchone()[0] == '2026-11-04T13:00:00+08:00'
