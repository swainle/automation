import json
from types import SimpleNamespace

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.longshot import fingerprint


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / 'sqlite.db')
    board = instance.seed('2026-11-02', [{'name': '猫粮', 'url': ''}])[0]
    instance.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    rows = [{'排行': i, '标题': f'旧商品{i}', '产品ID': str(i),
             '产品链接': f'https://mobile.yangkeduo.com/goods.html?goods_id={i}', '价格': '10'}
            for i in (1, 2)]
    instance.save_rows(1, tmp_path, [], rows, 'ios')
    for item in instance.items(1):
        instance.item_stage(item['id'], 'link', row=json.loads(item['row_json']))
        instance.stage_state(1, 'product_screenshot', 'completed', item_id=item['id'])
        instance.item_state(item['id'], 'completed')
        instance.product_state(item['id'], 'completed', details={'info': {'goods_id': item['goods_id']}})
    yield instance
    instance.close()


def refresh(store, tmp_path, rows):
    store.save_rows(1, tmp_path, [], rows, 'ios', update_existing=True, latest_snapshot=True)


def test_name_change_resets_progress_even_if_id_is_unchanged(store, tmp_path):
    refresh(store, tmp_path, [{'排行': 1, '标题': '当前文案', '产品ID': '1', '价格': '8'}])
    item = store.item(1)
    assert item['title'] == '当前文案'
    assert json.loads(item['row_json'])['价格'] == '8'
    assert item['state'] == 'pending'
    assert item['link_state'] == item['product_state'] == 'pending'
    assert store.stage(1, 'product_screenshot', 1)['state'] == 'pending'
    assert item['goods_url'] is None
    assert len(store.items(1)) == 1


def test_unchanged_verified_id_can_reuse_completed_capture(store, tmp_path):
    refresh(store, tmp_path, [{'排行': 1, '标题': '旧商品1', '产品ID': '1', '价格': '10'}])
    assert store.item(1)['state'] == 'completed'
    assert store.stage(1, 'product_screenshot', 1)['state'] == 'completed'


def test_same_rank_and_title_refresh_metrics_and_preserve_link_without_new_id(store, tmp_path):
    refresh(store, tmp_path, [{'排行': 1, '标题': '旧商品1', '价格': '7'}])
    item = store.item(1)
    assert item['state'] == 'pending'
    assert item['link_state'] == item['product_state'] == 'completed'
    assert item['goods_id'] == '1'
    assert item['goods_url'].endswith('goods_id=1')
    assert json.loads(item['product_json'])['info']['goods_id'] == '1'
    assert json.loads(item['row_json'])['价格'] == '7'
    assert store.stage(1, 'product_screenshot', 1)['state'] == 'pending'


def test_same_rank_and_title_reuse_completion_without_id_in_current_snapshot(store, tmp_path):
    refresh(store, tmp_path, [{'排行': 1, '标题': '旧商品1', '价格': '10'}])
    assert store.item(1)['state'] == 'completed'
    assert store.item(1)['goods_id'] == '1'


def test_old_decorated_page_title_does_not_change_board_identity(store, tmp_path):
    row = json.loads(store.item(1)['row_json'])
    row.update(榜单标题='旧商品1', 标题='【送赠品】旧商品1')
    store.item_stage(1, 'link', row=row)
    refresh(store, tmp_path, [{'排行': 1, '标题': '旧商品1', '价格': '10'}])
    item = store.item(1)
    assert item['state'] == 'completed'
    assert item['title'] == '旧商品1'
    assert json.loads(item['row_json'])['商品页标题'] == '【送赠品】旧商品1'


def test_swapped_ranks_never_reuse_other_products_artifacts(store, tmp_path):
    refresh(store, tmp_path, [{'排行': 1, '标题': '旧商品2', '产品ID': '2'},
                              {'排行': 2, '标题': '旧商品1', '产品ID': '1'}])
    assert [item['goods_id'] for item in store.items(1)] == ['2', '1']
    assert [item['state'] for item in store.items(1)] == ['pending', 'pending']
    assert all(item['product_json'] == '{}' for item in store.items(1))
    assert all(item['link_state'] == 'pending' for item in store.items(1))


def test_latest_snapshot_rolls_back_resets_and_removal_on_error(store, tmp_path):
    before = [dict(row) for row in store.db.execute('SELECT * FROM ranking_products ORDER BY id')]
    with pytest.raises(AttributeError):
        refresh(store, tmp_path, [{'排行': 1, '标题': '新商品', 'metrics': None}])
    assert before == [dict(row) for row in store.db.execute('SELECT * FROM ranking_products ORDER BY id')]
    assert store.item(1)['product_state'] == 'completed'


def test_long_image_is_invalidated_by_current_price_or_metrics():
    old = [{'排行': 1, '标题': '商品', '价格': '10'}]
    assert fingerprint(old) != fingerprint([{'排行': 1, '标题': '商品', '价格': '8'}])
    assert fingerprint(old) != fingerprint([{'排行': 1, '标题': '商品', '价格': '10', 'metrics': {'hot_index': '999'}}])
