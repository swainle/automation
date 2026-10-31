import sqlite3
from importlib.resources import files

import pytest

from automation.pdd.screenshot.state import StateStore

SQL = files('automation.pdd.screenshot').joinpath('migrate/20261031134259_merge_daily_shops.sql')


def test_migration_preserves_tasks_amounts_and_orphan_images(tmp_path):
    path = tmp_path / 'old.db'
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE daily_shops(day TEXT,shop TEXT,row_number INTEGER,manager TEXT,status TEXT,
            reason TEXT,attempts INTEGER,cleanup_ok INTEGER,updated_at TEXT,step TEXT,
            platform TEXT,started_at TEXT,finished_at TEXT,PRIMARY KEY(day,shop));
        CREATE TABLE receivable_records(day TEXT,shop TEXT,amount TEXT,image_path TEXT,updated_at TEXT);
        CREATE TABLE automation_events(id INTEGER,kind TEXT);
        INSERT INTO daily_shops(day,shop,row_number,manager,status,reason,attempts,cleanup_ok,updated_at)
            VALUES('2026-10-31','成功',12,'管理员','success','',2,1,'10:00'),
                  ('2026-10-31','验证',15,'管理员','login_error','verification_required',1,1,'10:00');
        INSERT INTO receivable_records VALUES('2026-10-31','成功','123.45','a.jpg','11:00'),
            ('2026-10-31','孤立图片','0.00','b.jpg','11:00');
    ''')
    connection.close()
    with pytest.raises(ValueError, match='需要迁移'):
        StateStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SQL.read_text(encoding='utf-8'))
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {'daily_shops'}
    finally:
        connection.close()
    store = StateStore(path)
    try:
        row = store.get('2026-10-31', '成功')
        assert (row['amount'], row['row_number'], row['manager'], row['attempts'], row['cleanup_ok']) == ('123.45', 12, '管理员', 2, 1)
        assert row['status'] == 'success'
        assert store.get('2026-10-31', '验证')['status'] == 'verification_required'
        assert store.get('2026-10-31', '孤立图片')['amount'] == '0.00'
        assert len(store.receivables('2026-10-31')) == 2
        store.finish('2026-10-31', '验证', 'verification_required', 'verification_required')
        split = files('automation.pdd.screenshot').joinpath('migrate/20261031134959_split_verification_status.sql').read_text(encoding='utf-8')
        store.db.executescript(split)
        store.db.executescript(split)  # 重复执行安全
        assert store.get('2026-10-31', '验证')['status'] == 'verification_other'
        assert store.get('2026-10-31', '成功')['status'] == 'success'
    finally:
        store.close()
