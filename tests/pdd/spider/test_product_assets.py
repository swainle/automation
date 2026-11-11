import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.product_extension import ExtensionProduct
from automation.pdd.spider.status import update_stage


def test_only_negative_tags_are_collected_and_merged_total_is_capped_at_100(tmp_path):
    collector = ExtensionProduct(Mock(), wait=lambda _: None)
    collector.rpc = Mock(return_value={'phase': 1})
    collector.dom = Mock(side_effect=lambda operation, **kwargs: {
        'reviews_enter': {}, 'reviews_ready': {'ready': True},
        'reviews_tags': {'names': ['难吃(60)', '包装差(80)'], 'tags': [
            {'name': '难吃(60)', 'count': 60}, {'name': '包装差(80)', 'count': 80}]},
        'reviews_tag': {}}[operation])
    first = [{'review_id': str(i), 'comment': '差评'} for i in range(60)]
    second = [{'review_id': str(i), 'comment': '差评'} for i in range(40, 120)]
    collector.review_batch = Mock(side_effect=[first, second])
    collector.comments(tmp_path)
    assert collector.review_batch.call_count == 2
    assert collector.review_batch.call_args_list[0].kwargs == {'expected': 60}
    assert collector.review_batch.call_args_list[1].kwargs == {'expected': 80}
    rows = json.loads((tmp_path / '差评原始.json').read_text(encoding='utf-8'))
    assert len(rows) == 100
    assert rows[40]['negative_tags'] == ['难吃(60)', '包装差(80)']
    assert not (tmp_path / '评论.csv').exists()
    assert not (tmp_path / '好评.csv').exists()


def test_review_batch_waits_for_end_or_limit_instead_of_silently_truncating():
    collector = ExtensionProduct(Mock(), wait=lambda _: None)
    collector.dom = Mock()
    collector.rpc = Mock(side_effect=[{'received': True, 'comments': [{'review_id': '1'}]},
                                     {'received': True, 'comments': [{'review_id': '1'}, {'review_id': '2'}], 'exhausted': True}])
    assert len(collector.review_batch(1, 100)) == 2
    collector.rpc = Mock(return_value={'received': True, 'comments': [{'review_id': '1'}]})
    with pytest.raises(RuntimeError, match='未确认结束'):
        collector.review_batch(1, 100)


def test_failed_download_records_url_and_path_for_retry(tmp_path):
    changed = []
    collector = ExtensionProduct(Mock(), on_image=lambda row: changed.append(dict(row)))
    with patch('automation.pdd.spider.product_extension.download', return_value=False), \
            pytest.raises(RuntimeError, match='下载或校验失败'):
        collector.download_image(tmp_path, 'main', 1, 'https://images.example/a.jpg', '主图/主图_01.jpg')
    assert [row['state'] for row in changed] == ['pending', 'failed']
    assert changed[-1]['source_url'] == 'https://images.example/a.jpg'
    assert changed[-1]['local_path'] == '主图/主图_01.jpg'


def test_images_and_comment_tag_union_are_persisted_in_database(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        board = store.seed('2026-11-02', [{'name': '主榜', 'url': ''}])[0]
        store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
        store.save_rows(1, tmp_path, [], [{'排行': 1, '标题': '商品'}], 'ios')
        record = {'image_type': 'main', 'position': 1, 'source_url': 'https://images.example/a.jpg',
                  'local_path': '主图/主图_01.jpg', 'state': 'pending'}
        store.save_product_image(1, record)
        assert store.db.execute('SELECT state FROM product_images').fetchone()[0] == 'pending'
        record['state'] = 'completed'
        store.product_state(1, 'completed', details={'images': [record],
            'comments': [{'review_id': '1', 'comment': '评论'}],
            'negative_comments': [{'review_id': '1', 'comment': '评论', 'negative_tags': ['难吃', '包装差']}],
            'sku': [{'SPU1': '鸡肉', '图片路径': 'SKU/01.jpg'}]})
        assert store.db.execute('SELECT count(*) FROM product_images').fetchone()[0] == 1
        assert tuple(store.db.execute('SELECT source_url,local_path,state FROM product_images').fetchone()) == (
            'https://images.example/a.jpg', '榜01/主图/主图_01.jpg', 'completed')
        assert json.loads(store.db.execute('SELECT tags_json FROM product_comments').fetchone()[0]) == ['难吃', '包装差']
        assert store.db.execute('SELECT image FROM product_skus').fetchone()[0] == '榜01/SKU/01.jpg'
        assert 'images_json' not in {row[1] for row in store.db.execute('PRAGMA table_info(product_info)')}
    finally:
        store.close()


def test_image_migrations_preserve_legacy_paths_and_remove_json_column():
    root = Path(__file__).resolve().parents[3] / 'src/automation/pdd/spider'
    db = sqlite3.connect(':memory:')
    db.executescript((root / 'migrate/20261102115136_rankings_products.sql').read_text(encoding='utf-8'))
    db.execute("INSERT INTO rankings(id,capture_date,source_name,tab_name) VALUES(1,'2026-11-02','主榜','畅销榜')")
    db.execute("INSERT INTO ranking_products(id,ranking_id,rank) VALUES(1,1,1)")
    db.execute('INSERT INTO product_info(ranking_product_id,images_json) VALUES(1,?)',
               (json.dumps({'main': ['主图/主图_01.jpg'], 'detail': ['详情图片/01.jpg', '详情图片/长图.jpg']}),))
    db.commit()
    for filename in ['20261102183000_product_images.sql', '20261102183001_product_images_data.sql',
                     '20261102183002_product_images_cleanup.sql']:
        db.executescript((root / 'migrate' / filename).read_text(encoding='utf-8'))
    assert db.execute('SELECT count(*) FROM product_images').fetchone()[0] == 3
    assert db.execute("SELECT source_url,state FROM product_images WHERE image_type='main'").fetchone() == (None, 'pending')
    assert 'images_json' not in {row[1] for row in db.execute('PRAGMA table_info(product_info)')}
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
