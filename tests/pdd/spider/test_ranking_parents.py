import sqlite3
from types import SimpleNamespace

from automation.pdd.spider.database import Store
from automation.pdd.spider.migrate_flat_rankings import migrate


def test_sources_do_not_create_parent_records(tmp_path):
    store = Store(tmp_path / 'test.db')
    source = store.seed('2026-11-04', [{'name': '猫粮', 'url': ''}])[0]
    assert store.db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 0
    store.discover(source['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    child = store.lists(source['id'])[0]
    assert child['id'] > 0
    assert 'parent_id' not in child
    assert store.db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 1
    store.close()


def test_flat_migration_preserves_product_and_real_ranking_ids(tmp_path):
    path = tmp_path / 'test.db'
    store = Store(path)
    store.db.execute('ALTER TABLE rankings ADD COLUMN parent_id INTEGER REFERENCES rankings(id)')
    store.db.execute("INSERT INTO rankings(id,capture_date,source_name,tab_name) VALUES(-1,'2026-11-04','猫粮','')")
    store.db.execute("INSERT INTO rankings(id,parent_id,capture_date,source_name,tab_name) VALUES(27,-1,'2026-11-04','猫粮','畅销榜')")
    store.db.execute("INSERT INTO ranking_products(id,ranking_id,rank,main_images_state) VALUES(42,27,1,'completed')")
    store.db.commit()
    store.close()
    result = migrate(path)
    assert result['negative_rankings_deleted'] == 1
    store = Store(path)
    assert store.db.execute('SELECT id FROM rankings').fetchone()[0] == 27
    assert store.item(42)['main_images_state'] == 'completed'
    assert 'parent_id' not in {row[1] for row in store.db.execute('PRAGMA table_info(rankings)')}
    assert store.db.execute('PRAGMA foreign_key_check').fetchall() == []
    store.close()
