from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from automation.pdd.spider import unified, config
from automation.pdd.spider.mobile_database import Store
from automation.pdd.spider.time_format import BEIJING


@pytest.fixture
def board(tmp_path, monkeypatch, pg_database):
    monkeypatch.setattr(config, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(config, 'BUSINESS_ROOT', tmp_path)
    store = Store()
    entry = store.seed_rankings('2026-11-10', [{'name': '猫粮', 'main_name': '猫粮', 'tab': '畅销榜',
        'url': 'https://example.com/rank', 'tab_index': 0}], tmp_path / 'data')[0]
    store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '商品一'}, {'排行': 2, '标题': '商品二'}])
    yield store, store.list(entry['id'])
    store.close()


def finish_mobile(store, entry):
    with store.db.transaction():
        store.db.execute("UPDATE t_pdd_ranking_products SET mobile_state='completed' WHERE ranking_id=%s", (entry['id'],))


def test_web_cooldown_boundary():
    now = datetime(2026, 11, 10, 12, tzinfo=BEIJING)
    assert unified.remaining_web_wait({'web_collected_at': None}, now) == 0
    assert unified.remaining_web_wait({'web_collected_at': (now - timedelta(minutes=29)).isoformat()}, now) == 60
    assert unified.remaining_web_wait({'web_collected_at': (now - timedelta(minutes=30)).isoformat()}, now) == 0


def test_unavailable_board_skips_without_waiting_and_retries_next_run(monkeypatch):
    from automation.pdd.spider.navigation import BoardUnavailableError
    entries = [dict(id=1, main_name='猫粮', tab='畅销榜'), dict(id=2, main_name='猫砂', tab='畅销榜')]
    store = Mock()
    store.seed_rankings.return_value = entries
    states = {1: 'pending', 2: 'pending'}
    store.list_state.side_effect = lambda rid, state: states.update({rid: state})
    store.items.side_effect = lambda rid: [] if rid == 1 else [dict(mobile_state='completed', sogou_state='pending')]
    monkeypatch.setattr('automation.pdd.spider.rank.read_leaderboards', Mock(return_value=[{}]))
    mobile = Mock(side_effect=BoardUnavailableError('未跳转'))
    monkeypatch.setattr(unified, 'mobile_round', mobile)
    sleep = Mock(side_effect=AssertionError('不可用榜单不应等待重试'))
    monkeypatch.setattr(unified.time, 'sleep', sleep)
    for _ in range(2):
        assert unified.collect(store, Mock()) == 0
        assert states == {1: 'unavailable', 2: 'pending'}
    assert mobile.call_count == 2
    assert all(call.args[2]['id'] == 1 for call in mobile.call_args_list)
    assert [call.args for call in store.export.call_args_list] == [(2,), (2,)]
    sleep.assert_not_called()


def test_web_only_fills_matching_pending_products(board, monkeypatch):
    store, entry = board
    finish_mobile(store, entry)
    with store.db.transaction():
        store.db.execute("UPDATE t_pdd_ranking_products SET sogou_state='completed',platform_product_link='keep' WHERE rank=2")
    read = Mock(return_value=([], [
        {'排行': 8, '标题': '商品一', '产品ID': '123', '产品链接': 'link', '上架日期': '2026-11-01'},
        {'排行': 2, '标题': '其他产品', '产品ID': '456', '产品链接': 'wrong', '上架日期': '2026-11-02'}], None))
    monkeypatch.setattr('automation.pdd.spider.devices.Sogou.read', read)
    unified.web_round(store, Mock(), entry)
    one, two = store.items(entry['id'])
    assert (one['platform_product_id'], one['sogou_state']) == ('123', 'completed')
    assert one['rank'] == 1
    assert two['platform_product_link'] == 'keep'
    unified.web_round(store, Mock(), store.list(entry['id']))
    assert read.call_count == 1


def test_web_failure_keeps_mobile_and_throttles(board, monkeypatch):
    store, entry = board
    finish_mobile(store, entry)
    read = Mock(side_effect=RuntimeError('风控'))
    monkeypatch.setattr('automation.pdd.spider.devices.Sogou.read', read)
    unified.web_round(store, Mock(), entry)
    assert all(i['mobile_state'] == 'completed' and i['sogou_state'] == 'failed' for i in store.items(entry['id']))
    unified.web_round(store, Mock(), store.list(entry['id']))
    assert read.call_count == 1


def test_web_mismatch_does_not_complete(board, monkeypatch):
    store, entry = board
    finish_mobile(store, entry)
    monkeypatch.setattr('automation.pdd.spider.devices.Sogou.read', Mock(return_value=([], [
        {'排行': 1, '标题': '换了商品', '产品ID': '123', '产品链接': 'wrong', '上架日期': '2026-11-01'}], None)))
    unified.web_round(store, Mock(), entry)
    assert all(i['sogou_state'] == 'failed' and not i['platform_product_link'] for i in store.items(entry['id']))


def test_web_duplicate_title_does_not_write_identity(board, monkeypatch):
    store, entry = board
    finish_mobile(store, entry)
    monkeypatch.setattr('automation.pdd.spider.devices.Sogou.read', Mock(return_value=([], [
        {'排行': rank, '标题': '商品一', '产品ID': str(rank), '产品链接': 'link', '上架日期': '2026-11-01'}
        for rank in (1, 8)], None)))
    unified.web_round(store, Mock(), entry)
    item = store.items(entry['id'])[0]
    assert item['sogou_state'] == 'failed' and not item['platform_product_id']


def test_reset_removes_old_files_and_child_rows(board):
    store, entry = board
    item = store.items(entry['id'])[0]
    from pathlib import Path
    folder = Path(item['directory'])
    folder.mkdir(parents=True)
    (folder / 'old.jpg').write_bytes(b'old')
    store.save_product_image(item['id'], {'image_type': 'main', 'position': 1, 'local_path': 'old.jpg'})
    with store.db.transaction():
        store.db.execute("""UPDATE t_pdd_ranking_products SET sogou_state='completed',
            platform_product_id='123',platform_product_link='keep-link',listing_date='2026-11-01' WHERE id=%s""", (item['id'],))
    unified.reset_mobile(store, item, {'排行': 1, '标题': '新产品'})
    refreshed = store.item(item['id'])
    assert refreshed['title'] == '新产品'
    assert not list(folder.iterdir())
    assert not store.db.execute('SELECT 1 FROM t_pdd_product_main_images').fetchone()
    assert (refreshed['sogou_state'], refreshed['platform_product_id'], refreshed['platform_product_link'],
            refreshed['listing_date']) == ('completed', '123', 'keep-link', '2026-11-01')


def test_product_filter_does_not_run_mobile_when_only_web_pending(board, monkeypatch):
    store, entry = board
    item = store.items(entry['id'])[0]
    finish_mobile(store, entry)
    mobile = Mock()
    monkeypatch.setattr(unified, 'mobile_round', mobile)
    def web(*args):
        with store.db.transaction():
            store.db.execute("UPDATE t_pdd_ranking_products SET sogou_state='completed' WHERE id=%s", (item['id'],))
    monkeypatch.setattr(unified, 'web_round', web)
    assert unified.collect(store, Mock(), product_id=item['id']) == 0
    mobile.assert_not_called()
    assert store.list(entry['id'])['collect_state'] != 'completed'


def test_complete_board_marks_itself_completed(board, monkeypatch):
    store, entry = board
    finish_mobile(store, entry)
    with store.db.transaction():
        store.db.execute("UPDATE t_pdd_ranking_products SET sogou_state='completed'")
    monkeypatch.setattr(config, 'POSTGRES_URL', 'test-dsn')
    assert unified.collect(store, Mock(), ranking_id=entry['id']) == 0
    assert store.list(entry['id'])['collect_state'] == 'completed'


def test_check_cli_does_not_launch_collection(board, monkeypatch, capsys):
    from automation.pdd.spider import __main__, commands, health
    from pathlib import Path
    store, entry = board
    execute = Mock()
    monkeypatch.setattr(commands, 'execute', execute)
    monkeypatch.setattr(health, 'environment_checks', lambda: [('Example', 'OK', 'Ready')])
    assert __main__.main(['--check', '--ranking-id', str(entry['id'])]) == 0
    output = capsys.readouterr().out
    assert '| product' in output and output.count('pending') == 4
    execute.assert_not_called()


def test_retries_beyond_three_rounds(board, monkeypatch):
    store, entry = board
    attempts = []
    def mobile(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 5:
            finish_mobile(store, entry)
    def web(*args):
        with store.db.transaction():
            store.db.execute("UPDATE t_pdd_ranking_products SET sogou_state='completed' WHERE mobile_state='completed'")
    monkeypatch.setattr(unified, 'mobile_round', mobile)
    monkeypatch.setattr(unified, 'web_round', web)
    monkeypatch.setattr(config, 'POSTGRES_URL', '')
    clock = iter(range(0, 100000, 1000))
    monkeypatch.setattr(unified.time, 'monotonic', lambda: next(clock))
    assert unified.collect(store, Mock(), ranking_id=entry['id']) == 0
    assert len(attempts) == 5


def test_mobile_collection_never_requests_web(board, monkeypatch):
    store, entry = board
    monkeypatch.setattr(unified, 'mobile_round', lambda *args, **kwargs: finish_mobile(store, entry))
    web = Mock(side_effect=AssertionError('手机采集不应访问网页'))
    monkeypatch.setattr(unified, 'web_round', web)
    assert unified.collect(store, Mock(), ranking_id=entry['id']) == 0
    web.assert_not_called()
    assert store.list(entry['id'])['collect_state'] == 'pending'


def test_mobile_round_replaces_only_unfinished_product(board, monkeypatch):
    from pathlib import Path
    store, entry = board
    one, two = store.items(entry['id'])
    for item in (one, two):
        folder = Path(item['directory'])
        folder.mkdir(parents=True)
        (folder / '产品榜单截图.jpg').write_bytes(b'old')
    with store.db.transaction():
        store.db.execute("UPDATE t_pdd_ranking_products SET mobile_state='completed',platform_product_link='keep' WHERE id=%s", (two['id'],))
    monkeypatch.setattr('automation.pdd.spider.navigation.open_app_board', Mock(return_value='xml'))
    monkeypatch.setattr(ios := unified.ios, '_xcui_texts', Mock(return_value=[]))
    monkeypatch.setattr(ios, '_rank_details', Mock(return_value={'主榜单名称': entry['main_name']}))
    monkeypatch.setattr(ios, '_guess_tab', Mock(side_effect=['好评榜', entry['tab']]))
    switch = Mock(return_value=[{'text': '已切换'}])
    monkeypatch.setattr(ios, '_switch_rank_tab', switch)
    rows = [{'排行': 1, '标题': '新商品一'}, {'排行': 2, '标题': '换了商品二'}]
    monkeypatch.setattr('automation.pdd.spider.rank.current.read_tab', Mock(return_value=([], rows)))
    monkeypatch.setattr('automation.pdd.spider.full_flow.prepare_top', Mock(return_value=('xml', [])))
    def capture(client, rows, xml, directory, **kwargs):
        (directory / '榜单长图.jpg').write_bytes(b'long')
        for item in (one, two):
            folder = directory / f"榜{item['rank']:02d}"
            folder.mkdir()
            (folder / '产品榜单截图.jpg').write_bytes(b'new')
    monkeypatch.setattr(unified.longshot, 'capture', capture)
    run = Mock()
    monkeypatch.setattr('automation.pdd.spider.weekly_mobile.run_products', run)
    unified.mobile_round(store, Mock(), entry)
    assert switch.call_count == 1
    assert switch.call_args.args[1] == entry['tab']
    assert switch.call_args.kwargs == {'initial_elements': []}
    assert store.item(one['id'])['title'] == '商品一'
    assert (Path(one['directory']) / '产品榜单截图.jpg').read_bytes() == b'new'
    assert store.item(two['id'])['title'] == '商品二'
    assert store.item(two['id'])['platform_product_link'] == 'keep'
    assert (Path(two['directory']) / '产品榜单截图.jpg').read_bytes() == b'old'
    assert run.call_args.kwargs['single_round'] is True


def test_mobile_round_missing_tab_is_unavailable(monkeypatch):
    from automation.pdd.spider.navigation import BoardUnavailableError
    monkeypatch.setattr('automation.pdd.spider.navigation.open_app_board', Mock(return_value='xml'))
    monkeypatch.setattr(unified.ios, '_xcui_texts', Mock(return_value=[]))
    monkeypatch.setattr(unified.ios, '_guess_tab', Mock(return_value='畅销榜'))
    monkeypatch.setattr(unified.ios, '_switch_rank_tab', Mock(side_effect=RuntimeError('未找到可点击的子榜标签')))
    store = Mock()
    with pytest.raises(BoardUnavailableError, match='好评榜'):
        unified.mobile_round(store, Mock(), {'id': 1, 'tab': '好评榜', 'source_name': '猫粮',
            'main_name': '猫粮', 'source_url': 'https://example.com/rank'})
    store.save_rows.assert_not_called()
