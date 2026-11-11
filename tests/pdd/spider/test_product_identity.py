import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.product import Product
from automation.pdd.spider.product_extension import ExtensionProduct
from automation.pdd.spider.product_info import discover_goods_id


def packet(data, path='/api/goods', **extra):
    return {'url': 'https://mobile.yangkeduo.com' + path, 'status': 200,
            'type': 'Fetch', 'data': data, **extra}


@pytest.mark.parametrize(('url', 'page_url', 'responses'), [
    ('https://mobile.yangkeduo.com/goods.html?goods_id=123', 'https://mobile.yangkeduo.com/goods.html', []),
    ('https://p.pinduoduo.com/token', 'https://mobile.yangkeduo.com/goods.html?goods_id=123', []),
    ('https://p.pinduoduo.com/token', 'https://mobile.yangkeduo.com/goods2.html?ps=token',
     [packet({'data': {'goods': {'goods_id': 123, 'goods_name': '当前商品'},
                       'recommendations': [{'goods_id': 999}]}})]),
])
def test_id_is_resolved_from_link_redirect_or_primary_response(url, page_url, responses):
    runtime = Mock()
    runtime.ranking_request.return_value = {'responses': responses}
    identified = Mock()
    collector = ExtensionProduct(runtime, wait=lambda _: None, on_identity=identified)
    row = {'产品链接': url}
    with patch.object(collector, 'dom', return_value={'url': page_url, 'sold_out': False}):
        _, goods_id = collector.resolve_identity(row)
    assert goods_id == row['产品ID'] == '123'
    identified.assert_called_once_with('123')


@pytest.mark.parametrize('responses', [
    [packet({'recommendations': [{'goods_id': 999}]})],
    [packet({'goods_id': 999}, path='/api/goods/recommend')],
    [packet({'goods_id': 999}, status=500)],
    [packet({'goods_id': 999, 'success': False})],
    [packet({'goods_id': 999}, url='https://other.example/api/goods')],
])
def test_recommendation_error_and_foreign_responses_are_not_identity(responses):
    assert discover_goods_id(responses) == ''


def test_conflicting_primary_ids_are_rejected():
    with pytest.raises(RuntimeError, match='唯一商品 ID'):
        discover_goods_id([packet({'goods_id': 123}), packet({'goods_id': 456})])


def test_unresolved_identity_retries_then_leaves_product_pending():
    runtime = Mock()
    runtime.ranking_request.return_value = {'responses': []}
    identified = Mock()
    collector = ExtensionProduct(runtime, wait=lambda _: None, on_identity=identified)
    with patch.object(collector, 'dom', return_value={
        'url': 'https://mobile.yangkeduo.com/goods2.html?ps=opaque', 'sold_out': False}) as page, \
            pytest.raises(RuntimeError, match='保留待重试'):
        collector.resolve_identity({'产品链接': 'https://p.pinduoduo.com/token'})
    assert page.call_count == 6
    identified.assert_not_called()


def test_redirect_must_not_replace_known_identity():
    collector = ExtensionProduct(Mock(), wait=lambda _: None)
    with patch.object(collector, 'dom', return_value={
        'url': 'https://mobile.yangkeduo.com/goods.html?goods_id=456', 'sold_out': False}), \
            pytest.raises(RuntimeError, match='不一致'):
        collector.resolve_identity({'产品链接': 'https://p.pinduoduo.com/token', '产品ID': '123'})


def test_missing_id_is_queued_and_persisted_before_later_collection_failure(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        board = store.seed('2026-11-02', [{'name': '猫粮', 'url': ''}])[0]
        store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
        store.save_rows(1, tmp_path, [], [{'排行': 1, '标题': '商品',
                        '产品链接': 'https://p.pinduoduo.com/token'}], 'ios')
        task, = store.product_tasks()
        assert task['goods_id'] is None
        with patch('automation.pdd.spider.product_extension.ExtensionProduct') as factory:
            def collect(_):
                factory.call_args.kwargs['on_identity']('123')
                raise RuntimeError('后续图片下载失败')
            factory.return_value.collect.side_effect = collect
            with pytest.raises(RuntimeError, match='下载失败'):
                Product(store, Mock()).collect(task)
        assert store.item(1)['goods_id'] == '123'
        assert json.loads(store.item(1)['row_json'])['产品ID'] == '123'
        assert json.loads((tmp_path / '榜01/分享链接.json').read_text(encoding='utf-8'))['goods_id'] == '123'
        assert task['goods_id'] == '123'
        assert store.item(1)['product_state'] == 'pending'
    finally:
        store.close()
