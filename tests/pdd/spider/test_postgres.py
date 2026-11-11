from copy import deepcopy
import json
import sqlite3

import psycopg
from psycopg import sql
import pytest

from automation.pdd.spider.mobile_database import Store, TABLES, connect
from automation.pdd.spider.migrate_to_postgres import merge, migrate


@pytest.fixture
def snapshot(pg_database, tmp_path):
    store = Store()
    entry = store.seed_rankings('2026-11-10', [dict(name='来源', main_name='主榜', tab='畅销榜',
        url='https://example.com/rank', tab_index=0)], tmp_path / 'data')[0]
    store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '新产品'}])
    item = store.items(entry['id'])[0]
    store.save_product_image(item['id'], dict(image_type='main', position=1, local_path='主图/001.jpg'))
    rows = {table.removeprefix('t_pdd_'): store.db.execute(sql.SQL('SELECT * FROM {} ORDER BY id').format(
        sql.Identifier('spider', table))).fetchall() for table in TABLES}
    store.db.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY CASCADE').format(
        sql.SQL(',').join(sql.Identifier('spider', table) for table in TABLES)))
    store.close()
    return rows


def source_files(tmp_path, rows, legacy=None):
    path, backup = tmp_path / 'source.db', tmp_path / 'legacy.json'
    with sqlite3.connect(path) as db:
        for table, values in rows.items():
            names = list(values[0]) if values else ['id']
            db.execute(f'CREATE TABLE {table} (' + ','.join('"' + name + '"' for name in names) + ')')
            for row in values:
                db.execute(f'INSERT INTO {table} VALUES (' + ','.join('?' for _ in names) + ')', list(row.values()))
    backup.write_text(json.dumps(legacy or {}, ensure_ascii=False), encoding='utf-8')
    return path, backup


def test_migration_is_verified_repeatable_and_keeps_original_ids(snapshot, pg_database, tmp_path):
    path, backup = source_files(tmp_path, snapshot)
    before = path.read_bytes()
    counts = migrate(path, backup, pg_database)
    assert counts['t_pdd_rankings'] == counts['t_pdd_ranking_products'] == 1
    assert migrate(path, backup, pg_database) == counts
    assert path.read_bytes() == before
    store = Store()
    assert store.item(1)['title'] == '新产品'
    second = store.seed_rankings('2026-11-17', [dict(name='来源', main_name='主榜', tab='畅销榜',
        url='https://example.com/rank', tab_index=0)], tmp_path / 'data')[0]
    assert second['id'] > 1
    store.close()


def test_merge_does_not_mix_old_product_children(snapshot):
    legacy = deepcopy(snapshot)
    legacy['ranking_products'][0]['title'] = '旧产品'
    legacy['product_main_images'][0]['local_path'] = '旧产品/001.jpg'
    result = merge(snapshot, legacy)
    assert result['ranking_products'][0]['title'] == '新产品'
    assert result['product_main_images'][0]['local_path'] != '旧产品/001.jpg'
    assert len(result['product_main_images']) == 1


def test_merge_imports_postgres_only_parent_and_remaps_children(snapshot):
    legacy = deepcopy(snapshot)
    legacy['rankings'][0]['source_name'] = '独有来源'
    result = merge(snapshot, legacy)
    assert [row['id'] for row in result['rankings']] == [1, 2]
    assert result['ranking_products'][1]['ranking_id'] == 2
    assert result['ranking_products'][1]['id'] == 2
    assert result['product_main_images'][1]['ranking_product_id'] == 2


def test_migration_rolls_back_all_rows_on_foreign_key_error(snapshot, pg_database, tmp_path):
    snapshot['product_main_images'][0]['ranking_product_id'] = 999
    path, backup = source_files(tmp_path, snapshot)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrate(path, backup, pg_database)
    with connect(readonly=True) as db:
        assert db.execute('SELECT count(*) AS count FROM t_pdd_rankings').fetchone()['count'] == 0


def test_native_store_rolls_back_and_connection_stays_open(pg_database):
    store = Store()
    with pytest.raises(RuntimeError):
        with store.db.transaction():
            store.db.execute("INSERT INTO t_pdd_selling_point(主卖点) VALUES ('rollback')")
            raise RuntimeError('stop')
    assert store.db.execute('SELECT count(*) AS count FROM t_pdd_selling_point').fetchone()['count'] == 0
    assert not store.db.closed
    store.close()


def test_connection_errors_do_not_fall_back_to_sqlite(tmp_path):
    with pytest.raises(psycopg.OperationalError):
        Store('postgresql://127.0.0.1:1/nonexistent')
    assert not list(tmp_path.glob('*.db'))
