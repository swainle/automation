import json
from pathlib import Path

from PIL import Image
import pytest

from automation.pdd.spider.migrate_states import migrate, removed_columns
from automation.pdd.spider.mobile_database import Store, PRODUCT_COLUMNS, columns, product_complete


def test_removed_columns_keep_business_dates_and_only_requested_states():
    names = {'created_at', 'updated_at', 'listing_date', 'capture_date', 'week_start',
             'collected_at', 'mobile_state', 'rank_state', 'product_state', 'last_stage', *PRODUCT_COLUMNS}
    assert set(removed_columns('t_pdd_ranking_products', names)) == {
        'collected_at', 'mobile_state', 'rank_state', 'product_state', 'last_stage'}


def test_migrate_preserves_records_and_business_dates_and_is_repeatable(pg_database, tmp_path):
    store = Store()
    try:
        entry = store.seed_rankings('2026-11-10', [dict(name='来源', main_name='主榜', tab='畅销榜',
            url='https://example.com/rank', tab_index=0)], tmp_path / 'data')[0]
        store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '产品'}])
        item = store.items(entry['id'])[0]
        folder = Path(item['directory'])
        folder.mkdir(parents=True)
        Image.new('RGB', (20, 20)).save(folder / '产品榜单截图.jpg')
        Image.new('RGB', (20, 20)).save(Path(entry['directory']) / '榜单长图.jpg')
        (Path(entry['directory']) / '榜单数据.xlsx').write_bytes(b'export')
        db = store.db
        db.execute("ALTER TABLE t_pdd_rankings ADD COLUMN collect_state TEXT DEFAULT 'completed', ADD COLUMN web_collected_at TEXT")
        db.execute("ALTER TABLE t_pdd_rankings DROP COLUMN state")
        db.execute("ALTER TABLE t_pdd_ranking_products ADD COLUMN mobile_state TEXT DEFAULT 'completed', ADD COLUMN collected_at TEXT")
        db.execute("ALTER TABLE t_pdd_ranking_products DROP COLUMN ranking_product_screenshot_state")
        for column in PRODUCT_COLUMNS[1:]:
            db.execute(f"UPDATE t_pdd_ranking_products SET {column}='completed'")
        db.execute("UPDATE t_pdd_ranking_products SET platform_product_id='123',listing_date='2026-01-01'")
        before = db.execute('SELECT created_at,updated_at FROM t_pdd_ranking_products').fetchone()
        backup, counts = migrate(db, tmp_path / 'backups')
        assert counts == {'t_pdd_rankings': 1, 't_pdd_ranking_products': 1}
        saved = json.loads(backup.read_text(encoding='utf-8'))
        assert saved['tables']['t_pdd_ranking_products'][0]['mobile_state'] == 'completed'
        assert set(c for c in columns(db, 't_pdd_ranking_products') if c.endswith('_state')) == set(PRODUCT_COLUMNS)
        assert set(c for c in columns(db, 't_pdd_rankings') if c == 'state' or c.endswith('_state')) == {'state'}
        new = store.item(item['id'])
        assert product_complete(new)
        assert new['platform_product_id'] == '123' and new['listing_date'] == '2026-01-01'
        assert {c: new[c] for c in before} == before
        assert store.list(entry['id'])['state'] == 'completed'
        migrate(db, tmp_path / 'backups')
        assert len(store.items(entry['id'])) == 1
    finally:
        store.close()


def test_migration_rolls_back_on_dependent_view(pg_database, tmp_path):
    store = Store()
    db = store.db
    try:
        db.execute('ALTER TABLE t_pdd_rankings ADD COLUMN web_collected_at TEXT')
        db.execute('CREATE VIEW ods.state_migration_guard AS SELECT web_collected_at FROM t_pdd_rankings')
        import psycopg
        with pytest.raises(psycopg.errors.DependentObjectsStillExist):
            migrate(db, tmp_path / 'backups')
        assert 'web_collected_at' in columns(db, 't_pdd_rankings')
        assert len(list((tmp_path / 'backups').glob('*.json'))) == 1
    finally:
        db.execute('DROP VIEW IF EXISTS ods.state_migration_guard')
        db.execute('ALTER TABLE t_pdd_rankings DROP COLUMN IF EXISTS web_collected_at')
        store.close()
