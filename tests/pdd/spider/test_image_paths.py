import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.migrate_image_paths import migrate


def prepared(tmp_path):
    store = Store(tmp_path / 'data/sqlite.db')
    board = store.seed('2026-11-05', [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    directory = tmp_path / 'data/grab/pdd/猫粮/畅销榜/2026-11-05'
    directory.mkdir(parents=True)
    store.save_rows(1, directory, ['排行', '标题'], [{'排行': 1, '标题': '猫粮商品'}], 'ios')
    return store, directory / '榜01'


def test_database_images_use_data_relative_paths_and_collectors_can_resume(tmp_path):
    store, directory = prepared(tmp_path)
    record = {'image_type': 'sku', 'position': 1, 'local_path': 'SKU/0001.jpg', 'state': 'completed'}
    store.save_product_image(1, record)
    expected = 'grab/pdd/猫粮/畅销榜/2026-11-05/榜01/SKU/0001.jpg'
    assert store.db.execute('SELECT local_path FROM product_images').fetchone()[0] == expected
    store.save_product_image(1, dict(record, local_path=expected))
    assert store.db.execute('SELECT local_path FROM product_images').fetchone()[0] == expected
    assert json.loads(store.item(1)['product_json'])['images'][0]['local_path'] == 'SKU/0001.jpg'
    store.product_state(1, 'completed', details={'sku': [{'SPU1': '鸡肉', 'price': '12.50', '图片路径': 'SKU/0001.jpg'}]})
    row = store.db.execute('SELECT image,raw_json FROM product_skus').fetchone()
    assert row['image'] == expected
    assert json.loads(row['raw_json'])['图片路径'] == expected
    assert json.loads(store.item(1)['product_json'])['sku'][0]['图片路径'] == 'SKU/0001.jpg'
    with pytest.raises(ValueError):
        store.save_product_image(1, dict(record, local_path='../outside.jpg'))
    store.close()


def test_migration_backs_up_paths_and_only_backfills_verified_screenshots(tmp_path):
    store, directory = prepared(tmp_path)
    store.db.execute("INSERT INTO product_images(ranking_product_id,image_type,position,local_path,state) VALUES(1,'sku',1,'SKU/0001.jpg','completed')")
    store.db.execute("INSERT INTO product_skus(ranking_product_id,dedup_key,image,raw_json) VALUES(1,'sku','SKU/0001.jpg',?)", (json.dumps({'图片路径': 'SKU/0001.jpg'}),))
    store.db.commit()
    store.close()
    with patch('automation.pdd.spider.longshot.reusable', return_value=False):
        first = migrate(tmp_path / 'data/sqlite.db')
    assert first['images'] == first['sku'] == 1
    assert first['ranking_screenshots'] == 0
    with sqlite3.connect(first['backup']) as db:
        assert db.execute('SELECT local_path FROM product_images').fetchone()[0] == 'SKU/0001.jpg'
    with patch('automation.pdd.spider.longshot.reusable', return_value=True):
        second = migrate(tmp_path / 'data/sqlite.db')
    assert second['images'] == second['sku'] == 0
    assert second['ranking_screenshots'] == 1
    with sqlite3.connect(tmp_path / 'data/sqlite.db') as db:
        assert db.execute('SELECT ranking_screenshot_state FROM ranking_products').fetchone()[0] == 'completed'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_failed_longshot_backfills_failed_item_screenshot_state(tmp_path):
    store, _ = prepared(tmp_path)
    store.stage_state(1, 'ranking_long_screenshot', 'failed', reason='截图失败')
    store.close()
    with patch('automation.pdd.spider.longshot.reusable', return_value=False):
        result = migrate(tmp_path / 'data/sqlite.db')
    assert result['failed_ranking_screenshots'] == 1
    with sqlite3.connect(tmp_path / 'data/sqlite.db') as db:
        assert db.execute('SELECT ranking_screenshot_state FROM ranking_products').fetchone()[0] == 'failed'


def test_longshot_capture_failure_updates_item_screenshot_state(tmp_path):
    from automation.pdd.spider.full_flow import IosFullFlow
    from unittest.mock import Mock
    store, _ = prepared(tmp_path)
    with patch('automation.pdd.spider.longshot.reusable', return_value=False), \
            patch('automation.pdd.spider.full_flow.prepare_top', return_value=('<App/>', [])), \
            patch('automation.pdd.spider.longshot.capture', side_effect=RuntimeError('截图失败')):
        with pytest.raises(RuntimeError, match='截图失败'):
            IosFullFlow(store, Mock()).run_list(1, [{'排行': 1, '标题': '猫粮商品'}], [])
    assert store.items(1)[0]['screenshot_state'] == 'failed'
    assert store.stage(1, 'ranking_long_screenshot')['state'] == 'failed'
    store.close()


def test_completed_canonical_detail_stage_replaces_old_segment_records(tmp_path):
    store, _ = prepared(tmp_path)
    store.save_product_image(1, {'image_type': 'detail', 'position': 1, 'local_path': '详情/old-1.jpg'})
    store.save_product_image(1, {'image_type': 'detail', 'position': 2, 'local_path': '详情/old-2.jpg'})
    store.product_state(1, 'completed', details={'images': [
        {'image_type': 'detail', 'position': 1, 'local_path': '详情/0001.jpg', 'state': 'completed'}],
        'status': {'stages': {'detail_images': {'state': 'completed'}}}})
    rows = store.db.execute("SELECT local_path FROM product_images WHERE image_type='detail'").fetchall()
    assert len(rows) == 1
    assert rows[0][0].endswith('/详情/0001.jpg')
    store.close()
