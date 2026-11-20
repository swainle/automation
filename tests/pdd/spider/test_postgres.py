from copy import deepcopy
import json
import sqlite3

import psycopg
from psycopg import sql
import pytest
from unittest.mock import Mock

from automation.pdd.spider.mobile_database import Store, TABLES, connect
from automation.pdd.spider.migrate_to_postgres import merge, migrate


def test_connection_uses_only_ods_schema_and_beijing_timezone(monkeypatch):
    connect_driver = Mock()
    monkeypatch.setattr(psycopg, 'connect', connect_driver)
    connect('postgresql://localhost/test', readonly=True)
    options = connect_driver.call_args.kwargs['options']
    assert '-c search_path=ods' in options
    assert '-c timezone=Asia/Shanghai' in options
    assert '-c default_transaction_read_only=on' in options


@pytest.mark.parametrize('readonly', [False, True])
def test_connection_keeps_idle_mobile_sessions_alive(monkeypatch, readonly):
    connect_driver = Mock()
    monkeypatch.setattr(psycopg, 'connect', connect_driver)
    connect('postgresql://localhost/test', readonly=readonly)
    kwargs = connect_driver.call_args.kwargs
    assert {key: kwargs[key] for key in ('keepalives', 'keepalives_idle', 'keepalives_interval', 'keepalives_count')} == {
        'keepalives': 1, 'keepalives_idle': 60, 'keepalives_interval': 30, 'keepalives_count': 5}


@pytest.fixture
def snapshot(pg_database, tmp_path):
    store = Store()
    entry = store.seed_rankings('2026-11-10', [dict(name='来源', main_name='主榜', tab='畅销榜',
        url='https://example.com/rank', tab_index=0)], tmp_path / 'data')[0]
    store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '新产品'}])
    item = store.items(entry['id'])[0]
    store.save_product_image(item['id'], dict(image_type='main', position=1, local_path='主图/001.jpg'))
    rows = {table.removeprefix('t_pdd_'): store.db.execute(sql.SQL('SELECT * FROM {} ORDER BY id').format(
        sql.Identifier('ods', table))).fetchall() for table in TABLES}
    store.db.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY').format(
        sql.SQL(',').join(sql.Identifier('ods', table) for table in TABLES)))
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


def test_native_store_rolls_back_and_connection_stays_open(pg_database, tmp_path):
    store = Store()
    with pytest.raises(RuntimeError):
        with store.db.transaction():
            store.seed_rankings('2026-11-10', [dict(name='来源', main_name='主榜', tab='畅销榜',
                url='https://example.com/rank', tab_index=0)], tmp_path / 'data')
            raise RuntimeError('stop')
    assert store.db.execute('SELECT count(*) AS count FROM t_pdd_rankings').fetchone()['count'] == 0
    assert not store.db.closed
    store.close()


def test_selling_point_is_product_owned_and_updates_timestamp(pg_database, tmp_path):
    store = Store()
    try:
        entry = store.seed_rankings('2026-11-10', [dict(name='来源', main_name='主榜', tab='畅销榜',
            url='https://example.com/rank', tab_index=0)], tmp_path / 'data')[0]
        store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '产品'}])
        product = store.items(entry['id'])[0]
        row = store.db.execute("""INSERT INTO t_pdd_selling_point(ranking_product_id,point_type,content)
            VALUES(%s,'main','卖点') RETURNING *""", (product['id'],)).fetchone()
        updated = store.db.execute("UPDATE t_pdd_selling_point SET content='新卖点' WHERE id=%s RETURNING *",
                                   (row['id'],)).fetchone()
        assert updated['created_at'] == row['created_at']
        assert updated['updated_at'] > row['updated_at']
        assert updated['created_at'].utcoffset() is not None
        with pytest.raises(psycopg.errors.CheckViolation):
            store.db.execute("UPDATE t_pdd_selling_point SET point_type='unknown' WHERE id=%s", (row['id'],))
        with pytest.raises(psycopg.errors.CheckViolation):
            store.db.execute("UPDATE t_pdd_selling_point SET content=' ' WHERE id=%s", (row['id'],))
    finally:
        store.close()


def test_connection_errors_do_not_fall_back_to_sqlite(tmp_path):
    with pytest.raises(psycopg.OperationalError):
        Store('postgresql://127.0.0.1:1/nonexistent')
    assert not list(tmp_path.glob('*.db'))
