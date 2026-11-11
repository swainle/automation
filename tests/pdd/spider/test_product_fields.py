import sqlite3
from pathlib import Path

from automation.pdd.spider.database import Store
from automation.pdd.spider.migrate_product_fields import migrate, RENAMES, REMOVED


def test_field_migration_preserves_json_bytes_and_state_values(tmp_path):
    path = tmp_path / 'old.db'
    raw = '{ "标题" : "测试商品", "id": 3 }'
    stages = '{ "product": {"custom": true, "state": "failed"} }'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE ranking_products(id INTEGER PRIMARY KEY,state TEXT,detail_state TEXT,
            screenshot_state TEXT,crop_state TEXT,detail_attempts INTEGER,files_json TEXT,completed_at TEXT,
            device TEXT,raw_json TEXT,stages_json TEXT);
            CREATE TABLE child(id INTEGER PRIMARY KEY,owner INTEGER REFERENCES ranking_products(id));''')
        db.execute('INSERT INTO ranking_products VALUES(3,?,?,?,?,?,?,?,?,?,?)',
                   ('completed','blocked_webview','completed','pending',4,'[]',None,'android',raw,stages))
        db.execute('INSERT INTO child VALUES(1,3)')
    result = migrate(path)
    assert Path(result['backup']).is_file()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(ranking_products)')}
        assert set(RENAMES.values()) <= columns
        assert not (set(RENAMES) | set(REMOVED)) & columns
        assert db.execute('SELECT raw_json,stages_json FROM ranking_products').fetchone() == (raw, stages)
        assert db.execute('SELECT rank_state,product_state,product_attempts FROM ranking_products').fetchone() == ('completed','blocked_webview',4)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_fresh_runtime_uses_simplified_columns(tmp_path):
    store = Store(tmp_path / 'new.db')
    columns = {row[1] for row in store.db.execute('PRAGMA table_info(ranking_products)')}
    assert set(RENAMES.values()) <= columns
    assert not (set(RENAMES) | set(REMOVED)) & columns
    store.close()
