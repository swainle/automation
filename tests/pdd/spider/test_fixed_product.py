import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.product import Product
from automation.pdd.spider.product_info import ProductInfo


@pytest.fixture(autouse=True)
def product_day():
    with patch('automation.pdd.spider.product.today', return_value='2026-10-31'):
        yield


def response(data, *, host='mobile.yangkeduo.com', goods_id='123'):
    value = Mock(url=f'https://{host}/api/goods?goods_id={goods_id}', status=200)
    value.request.resource_type = 'xhr'
    value.request.post_data_json = None
    value.json.return_value = data
    return value


def completed_task(store, tmp_path):
    board = store.seed('2026-10-31', [{'name': '主榜', 'url': 'https://example.com/rank'}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, tmp_path, ['排行', '标题', '产品链接'], [{'排行': 1, '标题': '商品', '产品ID': '123', '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=123'}], 'sogou')
    store.list_state(1, 'completed')
    store.finish('2026-10-31')


def test_capture_ignores_other_hosts_and_other_products():
    capture = ProductInfo('123')
    capture.receive(response({'goods_id': 123}, host='other.example'))
    capture.receive(response({'goods_id': 999}))
    with pytest.raises(RuntimeError, match='未监听到'):
        capture.finish()
    capture.receive(response({'data': {'goods': {'goods_id': 123, 'goods_name': '当前商品'}, 'recommendations': [{'goods_id': 999}]}}))
    result = capture.finish()
    assert result['responses'][0]['goods'] == [{'goods_id': 123, 'goods_name': '当前商品'}]


def test_identity_can_be_in_request_but_error_responses_are_not_product_info():
    capture = ProductInfo('123')
    capture.receive(response({'error_code': 54001, 'message': 'blocked'}))
    assert capture.responses == []
    capture.receive(response({'data': {'goods_name': '商品', 'price': 1230}}))
    assert capture.finish()['responses'][0]['data']['data']['price'] == 1230


def test_response_listener_is_attached_before_navigation_and_removed_on_error():
    from automation.pdd.spider.product_data import open_product_page
    context = Mock()
    page = context.new_page.return_value
    calls = []
    page.goto.side_effect = lambda *args, **kwargs: calls.append('navigate')
    open_product_page(context, 'https://example.com/goods', on_page=lambda value: calls.append('listen'))
    assert calls == ['listen', 'navigate']
    capture = ProductInfo('123')
    capture.attach(page)
    capture.close()
    page.remove_listener.assert_called_once_with('response', capture.receive)


def test_products_retry_until_completed_using_manual_connection(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    try:
        assert Product(store, runtime).run() == 0
        runtime.connect_product_browser.assert_not_called()
        completed_task(store, tmp_path / '榜单')
        store.item_state(1, 'completed')  # Full iOS collection is not detail completion.
        runner = Product(store, runtime, delay=0, item_delay=(0, 0))
        runner.collect = Mock(side_effect=[RuntimeError('temporary failure'), ('completed', {'sku': [{'规格': '小袋'}], 'status': {'listing_date': '2024-04-27'}})])
        assert runner.run() == 0
        assert runner.collect.call_count == 2
        row = store.items(1)[0]
        assert row['product_state'] == 'completed'
        assert row['product_attempts'] == 2
        assert json.loads(row['row_json'])['上架日期'] == '2024-04-27'
        assert (tmp_path / '榜单/商品数据.xlsx').is_file()
        runner.collect.reset_mock()
        assert runner.run() == 0
        runner.collect.assert_not_called()
        runtime.connect_product_browser.assert_called_once()
        runtime.start_ranking_browser.assert_not_called()
    finally:
        store.close()


def test_product_interrupt_preserves_checkpoint_and_closes_runtime(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    try:
        completed_task(store, tmp_path / '榜单')
        runner = Product(store, runtime, delay=0, item_delay=(0, 0))
        runner.collect = Mock(side_effect=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt):
            runner.run()
        assert store.items(1)[0]['product_state'] == 'running'
        runtime.close.assert_called_once()
    finally:
        store.close()


def test_pending_run_does_not_block_products_with_valid_links(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        completed_task(store, tmp_path / 'old')
        board = store.seed('2026-11-01', [{'name': '主榜', 'url': 'https://example.com/rank'}])[0]
        assert len(store.product_tasks()) == 1
        store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
        store.save_rows(2, tmp_path / 'new', ['排行'], [{'排行': 1, '产品ID': '456', '产品链接': 'https://example.com/456'}], 'sogou')
        store.list_state(2, 'failed', '其他商品截图失败')
        assert [task['directory'] for task in store.product_tasks()] == [str((tmp_path / 'old').resolve()), str((tmp_path / 'new').resolve())]
        runtime = Mock()
        runner = Product(store, runtime, delay=0, item_delay=(0, 0))
        runner.collect = Mock(return_value=('completed', {}))
        with patch('automation.pdd.spider.product.today', return_value='2026-11-01'):
            assert runner.run() == 0
        assert [call.args[0]['directory'] for call in runner.collect.call_args_list] == [str((tmp_path / 'new').resolve())]
        assert store.item(1)['product_state'] == 'pending'
        assert store.item(2)['product_state'] == 'completed'
        assert not (tmp_path / 'old/商品数据.xlsx').exists()
    finally:
        store.close()


def test_complete_and_skipped_products_do_not_connect_or_collect(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        completed_task(store, tmp_path / '榜单')
        for state in ('completed', 'skipped', 'blocked_webview'):
            store.product_state(1, state)
            runtime = Mock()
            runner = Product(store, runtime)
            runner.collect = Mock()
            assert runner.run() == 0
            runtime.connect_product_browser.assert_not_called()
            runtime.start_ranking_browser.assert_not_called()
            runner.collect.assert_not_called()
    finally:
        store.close()


def test_invalid_or_missing_product_urls_are_not_queued(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        completed_task(store, tmp_path / '榜单')
        for link in ('', 'not-a-url', 'javascript:alert(1)', 'https://[invalid'):
            with store.db:
                store.db.execute('UPDATE ranking_products SET goods_url=? WHERE id=1', (link,))
            assert store.product_tasks() == []
    finally:
        store.close()


def test_mixed_queue_only_collects_incomplete_product_details(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        completed_task(store, tmp_path / '榜单')
        rows = [json.loads(store.item(1)['row_json']),
                {'排行': 2, '标题': '待采商品', '产品ID': '456',
                 '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=456'}]
        store.save_rows(1, tmp_path / '榜单', [], rows, 'sogou', update_existing=True)
        store.product_state(1, 'completed')
        runtime = Mock()
        runner = Product(store, runtime, delay=0, item_delay=(0, 0))
        runner.collect = Mock(return_value=('completed', {'info': {'goods_id': '456'}}))
        assert runner.run() == 0
        assert runner.collect.call_count == 1
        assert runner.collect.call_args.args[0]['goods_id'] == '456'
        assert store.item(1)['product_attempts'] == 0
        assert store.item(2)['product_state'] == 'completed'
    finally:
        store.close()


def test_sold_out_reason_is_persisted_and_rerun_skips_product(tmp_path):
    from automation.pdd.spider.native_product import SOLD_OUT_REASON
    from openpyxl import load_workbook
    store = Store(tmp_path / 'sqlite.db')
    try:
        completed_task(store, tmp_path / '榜单')
        runner = Product(store, Mock(), delay=0, item_delay=(0, 0))
        runner.collect = Mock(return_value=('skipped', {'status': {'stages': {'product': {
            'state': 'skipped', 'reason': SOLD_OUT_REASON, 'outcome': 'sold_out', 'retryable': False}}}}))
        assert runner.run() == 0
        assert runner.run() == 0
        runner.collect.assert_called_once()
        assert store.item(1)['product_state'] == 'skipped'
        assert store.item(1)['reason'] == SOLD_OUT_REASON
        with (tmp_path / '榜单/商品数据.xlsx').open('rb') as stream:
            book = load_workbook(stream, read_only=True)
            rows = list(book.active.values)
            assert rows[1][rows[0].index('异常原因')] == SOLD_OUT_REASON
            book.close()
    finally:
        store.close()


def test_ranking_id_only_collects_selected_ranking_even_on_another_day(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        completed_task(store, tmp_path / 'first')
        board = store.seed('2026-11-01', [{'name': '另一个榜单', 'url': 'https://example.com/rank2'}])[0]
        store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
        store.save_rows(2, tmp_path / 'second', ['排行', '标题', '产品链接'], [
            {'排行': 1, '标题': '指定榜单商品', '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=456'}], 'sogou')
        runner = Product(store, Mock(), ranking_id=2, item_delay=(0, 0))
        runner.collect = Mock(return_value=('completed', {}))
        with patch('automation.pdd.spider.concurrency.ranking_active', return_value=True):
            assert runner.run() == 0
        assert runner.collect.call_count == 1
        assert runner.collect.call_args.args[0]['ranking_list_id'] == 2
        assert store.item(1)['product_state'] == 'pending'
        assert store.item(2)['product_state'] == 'completed'
        assert len(store.product_tasks(ranking_id=2)) == 1
        assert store.product_tasks(day='2026-10-31', ranking_id=2) == []
    finally:
        store.close()


def test_unknown_ranking_id_fails_before_connecting(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    try:
        with pytest.raises(ValueError, match='榜单 ID 不存在'):
            Product(store, runtime, ranking_id=24).run()
        runtime.connect_product_browser.assert_not_called()
    finally:
        store.close()


@pytest.mark.parametrize('args', [
    ['--ranking-id', '0'], ['--ranking-id', '-1'],
    ['--android', '--ranking-id', '24', '--current'],
    ['--android', '--ranking-id', '24', '--jump', 'https://example.com'],
])
def test_ranking_id_rejects_invalid_arguments(args):
    from automation.pdd.spider.product import main
    with patch('automation.pdd.spider.commands.execute') as execute, pytest.raises(SystemExit):
        main(args)
    execute.assert_not_called()


def test_ranking_id_cli_passes_selection_to_worker():
    from automation.pdd.spider.product import main
    store, runtime = Mock(), Mock()
    with patch('automation.pdd.spider.commands.execute', side_effect=lambda description, run, **kw: run(store, runtime)), \
            patch('automation.pdd.spider.product.Product') as worker:
        main(['--android', '--ranking-id', '24'])
    worker.assert_called_once_with(store, runtime, ranking_id=24)
    worker.return_value.run.assert_called_once()
