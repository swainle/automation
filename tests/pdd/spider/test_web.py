from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import nullcontext

import pytest

from automation.pdd.spider import web, unified
from automation.pdd.spider.runtime import Runtime


@pytest.fixture
def task():
    item = dict(id=2, mobile_state='completed', sogou_state='pending')
    entry = dict(id=1, web_collected_at=None)
    store = Mock()
    store.list.return_value = entry
    store.items.return_value = [item]
    return store, item


def test_mobile_entry_does_not_start_or_wait_for_web(task, monkeypatch):
    store, item = task
    mobile, browser = Mock(), Mock(side_effect=AssertionError('unexpected web call'))
    monkeypatch.setattr(unified, 'mobile_round', mobile)
    monkeypatch.setattr(unified, 'web_round', browser)
    assert unified.collect(store, Runtime(), ranking_id=1) == 0
    mobile.assert_not_called()
    browser.assert_not_called()
    store.list_state.assert_called_once_with(1, 'pending')


def test_web_success_closes_task_window_without_mobile_collection(task, monkeypatch):
    store, item = task
    runtime = Runtime()
    runtime.ranking_tab = 10
    runtime._ranking_keep_open = True
    runtime.ranking_request = Mock()
    def finish(*args):
        item['sogou_state'] = 'completed'
    monkeypatch.setattr(web, 'web_round', finish)
    assert web.collect(store, runtime, ranking_id=1) == 0
    runtime.close()
    runtime.ranking_request.assert_called_once_with('ranking_close', {'tab_id': 10}, timeout=35)
    store.list_state.assert_called_once_with(1, 'completed')


def test_web_retries_after_cooldown_and_retains_window_on_interrupt(task, monkeypatch):
    store, item = task
    runtime = Runtime()
    runtime._ranking_keep_open = True
    clock = [0]
    monkeypatch.setattr(web.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(web.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(web, 'remaining_web_wait', lambda entry: max(0, 1800 - clock[0]))
    attempts = []
    def fail(*args):
        attempts.append(clock[0])
        if len(attempts) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(web, 'web_round', fail)
    with pytest.raises(KeyboardInterrupt):
        web.collect(store, runtime, ranking_id=1)
    assert attempts == [0, 1800]
    assert runtime._ranking_keep_open


def test_web_collects_before_mobile_without_changing_mobile_state(task, monkeypatch):
    store, item = task
    item['mobile_state'] = 'pending'
    def finish(*args):
        item['sogou_state'] = 'completed'
    read = Mock(side_effect=finish)
    monkeypatch.setattr(web, 'web_round', read)
    assert web.collect(store, Runtime(), ranking_id=1) == 0
    read.assert_called_once()
    assert item['mobile_state'] == 'pending'
    store.list_state.assert_called_once_with(1, 'pending')


@pytest.mark.parametrize('browser', ['sogou', 'chrome', 'edge'])
def test_browser_cli_passes_selection_and_filters(browser, monkeypatch):
    from automation.pdd.spider import commands
    runtime = Runtime()
    monkeypatch.setattr(web, 'browser_settings', lambda browser: ('browser.exe', 'profile'))
    collect = Mock(return_value=0)
    monkeypatch.setattr(web, 'collect', collect)
    monkeypatch.setattr(commands, 'execute', lambda description, run, **kwargs: run('store', runtime))
    assert web.main(browser, ['--ranking-id', '1', '--ranking-product-id', '2']) == 0
    assert (runtime.ranking_browser, runtime.ranking_executable, runtime.ranking_user_data_dir,
            runtime.ranking_window) == (browser, 'browser.exe', 'profile', True)
    collect.assert_called_once_with('store', runtime, 1, 2)


def test_web_rejects_mismatched_product_before_opening_browser(task):
    store, item = task
    store.db.execute.return_value.fetchone.return_value = {'ranking_id': 99}
    with pytest.raises(ValueError, match='不属于'):
        web.collect(store, Runtime(), ranking_id=1, product_id=2)


def test_default_selection_uses_current_week_without_creating_rankings(task, monkeypatch):
    store, item = task
    item['sogou_state'] = 'completed'
    store.db.execute.return_value.fetchall.return_value = [{'id': 1}]
    monkeypatch.setattr(web, 'today', lambda: '2026-11-11')
    assert web.collect(store, Runtime()) == 0
    assert store.db.execute.call_args.args[1] == ('2026-11-09',)
    store.seed_rankings.assert_not_called()


def test_edge_process_is_identified_even_with_chrome_user_agent(monkeypatch):
    from automation.com import extension_bridge
    connection = SimpleNamespace(pid=123, laddr=SimpleNamespace(port=456), raddr=SimpleNamespace(port=8765))
    monkeypatch.setattr(extension_bridge.psutil, 'net_connections', lambda **kwargs: [connection])
    monkeypatch.setattr(extension_bridge.psutil, 'Process', lambda pid: SimpleNamespace(name=lambda: 'msedge.exe'))
    assert extension_bridge.browser_for_peer(456, 8765, 'chrome') == 'edge'


@pytest.mark.parametrize('browser', ['chrome', 'edge'])
def test_launcher_uses_selected_browser_and_an_independent_window(browser, tmp_path, monkeypatch):
    from automation.pdd.spider import runtime as module
    executable = tmp_path / f'{browser}.exe'
    executable.touch()
    runtime = Runtime()
    runtime.ranking_browser = browser
    runtime.ranking_executable = executable
    runtime.ranking_user_data_dir = tmp_path / 'profile'
    runtime.ranking_window = True
    runtime.ranking_request = Mock(side_effect=[
        {'connected': False, 'protocol': 3},
        {'connected': True, 'protocol': 3, 'extension_peer_port': 1234},
        {'tab_id': 10, 'protocol': 2}])
    monkeypatch.setattr(module, 'port_ready', lambda port: True)
    monkeypatch.setattr(runtime, '_verify_ranking_browser', Mock())
    launch = Mock()
    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    runtime.start_ranking_browser()
    assert launch.call_args.args[0] == [str(executable), f'--user-data-dir={tmp_path / "profile"}', '--no-startup-window']
    runtime.ranking_request.assert_called_with('ranking_open', {'reuse_workspace': False, 'new_window': True})
    assert runtime._ranking_keep_open


def test_mobile_retry_preserves_web_fields_and_state(tmp_path, monkeypatch):
    from automation.pdd.spider import config
    monkeypatch.setattr(config, 'OUTPUT_ROOT', tmp_path)
    directory = tmp_path / 'board/2026-11-11'
    folder = directory / '榜01'
    folder.mkdir(parents=True)
    (folder / 'old.jpg').write_bytes(b'old')
    item = dict(id=2, ranking_id=1, rank=1, directory=str(folder), title='产品', mobile_state='failed',
                sogou_state='completed', platform_product_id='123', platform_product_link='keep-link', listing_date='2026-11-01')
    store = Mock()
    store.list.return_value = {'directory': str(directory)}
    store.db.transaction.side_effect = nullcontext
    def execute(sql, values):
        if sql.startswith('UPDATE t_pdd_ranking_products SET '):
            fields = sql.split(' SET ', 1)[1].split(' WHERE ', 1)[0].split(',')
            item.update((field.split('=')[0], value) for field, value in zip(fields, values))
    store.db.execute.side_effect = execute
    unified.reset_mobile(store, item, {'排行': 1, '标题': '产品'})
    assert item['mobile_state'] == 'pending'
    assert (item['sogou_state'], item['platform_product_id'], item['platform_product_link'],
            item['listing_date']) == ('completed', '123', 'keep-link', '2026-11-01')
    assert not list(folder.iterdir())


def test_web_round_updates_only_web_fields_when_mobile_failed(monkeypatch):
    store = Mock()
    store.items.return_value = [dict(id=2, title='产品', mobile_state='failed', sogou_state='pending')]
    store.db.transaction.side_effect = nullcontext
    monkeypatch.setattr('automation.pdd.spider.devices.Sogou.read', Mock(return_value=([], [
        {'标题': '产品', '产品ID': '123', '产品链接': 'keep-link', '上架日期': '2026-11-01'}], None)))
    entry = dict(id=1, source_name='榜单', main_name='榜单', source_url='url', capture_date='2026-11-11', web_collected_at=None)
    unified.web_round(store, Mock(), entry)
    queries = [call.args[0] for call in store.db.execute.call_args_list]
    assert any("sogou_state='completed'" in query for query in queries)
    assert all(not any(field in query for field in ('mobile_state=', 'sku_state=', 'main_images_state=',
                                                  'detail_images_state=', 'comments_state=', 'DELETE FROM')) for query in queries)
