from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATE = ROOT / 'src/automation/pdd/spider/migrate'
STRUCTURE = (MIGRATE / '20261102173000_owner_states.sql').read_text(encoding='utf-8')
DATA = (MIGRATE / '20261102173001_owner_states_data.sql').read_text(encoding='utf-8')
CLEANUP = (MIGRATE / '20261102173002_owner_states_cleanup.sql').read_text(encoding='utf-8')
SCHEMA = (Path(__file__).with_name('fixtures') / 'schema_v3.sql').read_text(encoding='utf-8')


def test_owner_state_migration_preserves_checkpoints_and_failure():
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA)
    db.execute("INSERT INTO rankings(id,capture_date,source_name,tab_name,state,last_error) VALUES(1,'2026-11-02','source','tab','failed','board error')")
    db.execute("INSERT INTO ranking_products(id,ranking_id,rank,state,goods_url) VALUES(1,1,1,'failed','https://example.com/goods')")
    db.execute("INSERT INTO ranking_products(id,ranking_id,rank,state) VALUES(2,1,2,'running')")
    stages = [('collect', None, 'completed'), ('ranking_long_screenshot', None, 'completed'),
              ('ranking_crop', 1, 'completed'), ('product_screenshot', 1, 'completed'),
              ('share_link', 1, 'completed'), ('product', 1, 'running'),
              ('return_to_board', 1, 'failed')]
    for i, (stage, item, state) in enumerate(stages):
        db.execute('''INSERT INTO collection_tasks(task_key,capture_date,ranking_id,ranking_product_id,
                      stage,state,attempts,last_error) VALUES(?, '2026-11-02',1,?,?,?,?,?)''',
                   (str(i), item, stage, state, 3, 'return error' if state == 'failed' else ''))
    db.commit()
    db.executescript(STRUCTURE)
    assert db.execute('PRAGMA user_version').fetchone() == (400,)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone()
    assert db.execute('SELECT link_state FROM ranking_products WHERE id=1').fetchone() == ('pending',)
    schema_before = db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    db.executescript(DATA)
    assert db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == schema_before
    assert db.execute('PRAGMA user_version').fetchone() == (401,)
    db.executescript(CLEANUP)
    assert db.execute('PRAGMA user_version').fetchone() == (4,)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone() is None
    assert db.execute('SELECT collect_state,screenshot_state,last_error FROM rankings').fetchone() == ('completed', 'completed', 'board error')
    assert db.execute('''SELECT state,crop_state,screenshot_state,link_state,detail_state,detail_attempts,
                       last_stage,last_error,goods_url FROM ranking_products WHERE id=1''').fetchone() == (
        'failed', 'completed', 'completed', 'completed', 'pending', 3,
        'return_to_board', 'return error', 'https://example.com/goods')
    assert db.execute('SELECT state FROM ranking_products WHERE id=2').fetchone() == ('pending',)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        db.executescript(STRUCTURE)
    db.rollback()
    assert db.execute('SELECT count(*) FROM ranking_products').fetchone() == (2,)


def test_owner_state_migration_ddl_can_be_rolled_back():
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA)
    with pytest.raises(sqlite3.OperationalError):
        db.executescript(STRUCTURE.replace('PRAGMA user_version=400;', 'SELECT missing_upgrade_column FROM rankings;'))
    db.rollback()
    assert 'crop_state' not in {row[1] for row in db.execute('PRAGMA table_info(ranking_products)')}
    assert db.execute('PRAGMA user_version').fetchone() == (3,)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone()


def test_cleanup_rejects_incomplete_data_transfer():
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA)
    db.executescript(STRUCTURE)
    with pytest.raises(sqlite3.IntegrityError):
        db.executescript(CLEANUP)
    db.rollback()
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone()
    db.executescript(DATA)
    db.executescript(CLEANUP)
    assert db.execute('PRAGMA user_version').fetchone() == (4,)
