from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.rank import current


def excel_source(name, url):
    return {'name': name, 'main_name': name, 'tab': '畅销榜', 'url': url, 'tab_index': 0}


def test_current_ios_reuses_existing_records_without_duplicates(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    def rows(client, main, tab, **kwargs):
        return ['排行', '标题'], [{'排行': 1, '标题': tab + '商品', '主榜单名称': main,
                                 'metrics': {'display_price': '¥29.90'}}]
    with patch.object(current, 'current_board', return_value=('全价猫粮', ['畅销榜', '好评榜', '降价榜'], True)), \
            patch.object(current, 'read_tab', side_effect=rows) as read, \
            patch.object(current.ios, '_switch_rank_tab') as switch, patch.object(current.config, 'OUTPUT_ROOT', tmp_path), \
            patch.object(current.ios, 'open_link') as navigate:
        assert current.collect(store, runtime) == 0
        assert read.call_count == 3
        assert switch.call_count == 3
        old_ids = [row[0] for row in store.db.execute('SELECT id FROM ranking_products ORDER BY id')]
        assert current.collect(store, runtime) == 0
        assert read.call_count == 3
        assert old_ids == [row[0] for row in store.db.execute('SELECT id FROM ranking_products ORDER BY id')]
        navigate.assert_not_called()
    assert store.db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM ranking_products').fetchone()[0] == 3
    store.close()


def test_once_records_failed_tab_without_retry_loop_and_can_resume(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    with patch.object(current, 'current_board', return_value=('猫粮', ['畅销榜', '好评榜'], True)), \
            patch.object(current, 'read_tab', side_effect=[RuntimeError('读取失败'), (['排行'], [{'排行': 1, '标题': '商品'}])]), \
            patch.object(current.ios, '_switch_rank_tab'), patch.object(current.config, 'OUTPUT_ROOT', tmp_path):
        assert current.collect(store, runtime) == 1
    assert store.list(1)['state'] == 'failed'
    assert store.list(2)['state'] == 'completed'
    assert store.list(1)['last_error'] == '读取失败'
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_tasks'").fetchone()
    store.close()


def test_first_snapshot_is_reused_without_drag_or_another_source_read():
    client = Mock()
    # An off-screen recommendation boundary still proves it is in the full tree.
    elements = [{'text': '更多上榜商品推荐', 'y': 4687}]
    with patch.object(current.ios, '_rank_details', return_value={'主榜单名称': '猫粮'}), \
            patch.object(current.ios, '_guess_tab', return_value='畅销榜'), \
            patch.object(current.ios, '_parse_rows', return_value=[{'排行': 1, '标题': 'A'}]) as parse, \
            patch.object(current.ios, '_swipe') as swipe, \
            patch.object(current.ios, '_scroll_to_top') as top, \
            patch.object(current.ios, '_current_page_source') as source:
        _, rows = current.read_tab(client, '猫粮', '畅销榜', initial_elements=elements)
    source.assert_not_called()
    swipe.assert_not_called()
    top.assert_not_called()
    parse.assert_called_once_with(elements, current.TAB_TO_CLASS['畅销榜'])
    assert rows[0]['标题'] == 'A'


def test_missing_end_boundary_is_not_saved_or_loaded_by_dragging():
    with patch.object(current.ios, '_rank_details', return_value={'主榜单名称': '猫粮'}), \
            patch.object(current.ios, '_guess_tab', return_value='畅销榜'), \
            patch.object(current.ios, '_parse_rows') as parse, \
            patch.object(current.ios, '_swipe') as swipe, \
            pytest.raises(RuntimeError, match='不拖动'):
        current.read_tab(Mock(), '猫粮', '畅销榜', initial_elements=[{'text': '热卖指数123'}])
    parse.assert_not_called()
    swipe.assert_not_called()


def test_without_cached_snapshot_reads_source_once():
    elements = [{'text': '更多推荐', 'y': 4000}]
    client = Mock()
    with patch.object(current.ios, '_current_page_source', return_value='xml') as source, \
            patch.object(current.ios, '_xcui_texts', return_value=elements), \
            patch.object(current.ios, '_rank_details', return_value={'主榜单名称': '猫粮'}), \
            patch.object(current.ios, '_guess_tab', return_value='畅销榜'), \
            patch.object(current.ios, '_parse_rows', return_value=[{'排行': 1, '标题': 'A'}]):
        current.read_tab(client, '猫粮', '畅销榜')
    source.assert_called_once_with(client)


def test_cli_accepts_requested_flags_without_reading_source_excel():
    with patch('automation.pdd.spider.commands.execute', return_value=0) as execute:
        assert current.main(['--ios']) == 0
    assert execute.call_args.kwargs['args'].ios
    assert execute.call_args.kwargs['args'].once
    assert execute.call_args.kwargs['args'].full


def test_full_cli_dispatches_the_complete_flow():
    with patch.object(current, 'collect', return_value=0) as collect, \
            patch('automation.pdd.spider.commands.execute', side_effect=lambda description, callback, **kwargs: callback('store', 'runtime')):
        assert current.main(['--ios', '--current', '--once', '--full', '--refresh']) == 0
    collect.assert_called_once_with('store', 'runtime', source_name=None, full=True, refresh=True)


@pytest.mark.parametrize('flags', [['--ios'], ['--ios', '--refresh']])
def test_short_cli_runs_full_collection(flags):
    with patch.object(current, 'collect_sources', return_value=0) as collect, \
            patch('automation.pdd.spider.commands.execute', side_effect=lambda description, callback, **kwargs: callback('store', 'runtime')):
        assert current.main(flags) == 0
    collect.assert_called_once_with('store', 'runtime', refresh='--refresh' in flags, ranking_id=None)


def test_same_day_completed_board_skips_app_snapshot(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed(current.today(), [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, tmp_path, [], [{'排行': 1, '标题': '商品'}], 'ios')
    store.item_state(1, 'completed')
    store.stage_state(1, 'ranking_long_screenshot', 'completed')
    store.list_state(1, 'completed')
    with patch.object(current, 'current_board', return_value=('猫粮', ['畅销榜'], False)), \
            patch.object(current, 'read_tab', return_value=([], [{'排行': 1, '标题': '新商品'}])) as read, \
            patch.object(current.config, 'OUTPUT_ROOT', tmp_path), \
            patch('automation.pdd.spider.full_flow.IosFullFlow') as flow:
        assert current.collect(store, Mock(), full=True) == 0
    read.assert_not_called()
    flow.return_value.run_list.assert_not_called()
    assert store.item(1)['title'] == '商品'
    assert store.item(1)['state'] == 'completed'
    store.close()


def test_foreground_must_be_pdd_and_does_not_navigate_automatically():
    client = Mock()
    client.http.get.return_value.value = {'bundleId': 'com.apple.mobilesafari'}
    with patch.object(current.ios, 'open_link') as navigate, pytest.raises(RuntimeError, match='前台'):
        current.current_board(client)
    navigate.assert_not_called()


def test_opaque_webview_reports_missing_product_nodes_and_saves_source(tmp_path):
    client = Mock()
    client.http.get.return_value.value = {'bundleId': current.ios.PDD_BUNDLE_ID}
    xml = '<XCUIElementTypeApplication><XCUIElementTypeNavigationBar name="散装猫零食畅销榜"/><XCUIElementTypeWebView/></XCUIElementTypeApplication>'
    with patch.object(current.config, 'BUSINESS_ROOT', tmp_path), \
            patch.object(current.ios, '_current_page_source', return_value=xml), \
            pytest.raises(RuntimeError, match='没有商品文本节点'):
        current.current_board(client)
    assert len(list((tmp_path / 'data/diagnostics').glob('*.xml'))) == 1
    assert current.ios._guess_tab([{'type': 'NavigationBar', 'text': '散装猫零食畅销榜'}]) == '畅销榜'


def test_excel_loop_continues_after_navigation_failure_and_records_source(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    sources = [excel_source('猫粮', 'https://example.com/1'), excel_source('猫砂', 'https://example.com/2')]
    with patch('automation.pdd.spider.rank.read_leaderboards', return_value=sources), \
            patch('automation.pdd.spider.navigation.open_app_board', side_effect=[RuntimeError('未跳转'), None]) as navigate, \
            patch.object(current, 'collect', return_value=0) as collect:
        assert current.collect_sources(store, runtime, refresh=True) == 1
    assert [call.args[1] for call in navigate.call_args_list] == sources
    collect.assert_called_once_with(store, runtime, full=True, refresh=True, entry=store.list(2))
    assert store.list(1)['state'] == 'failed'
    store.close()


def test_excel_loop_propagates_interrupt_and_fatal_errors(tmp_path):
    from automation.pdd.spider.workflow import FatalWorkflowError
    store = Store(tmp_path / 'sqlite.db')
    sources = [excel_source('猫粮', 'https://example.com/1')]
    for error in (KeyboardInterrupt(), FatalWorkflowError('WDA unavailable')):
        with patch('automation.pdd.spider.rank.read_leaderboards', return_value=sources), \
                patch('automation.pdd.spider.navigation.open_app_board', side_effect=error), \
                pytest.raises(type(error)):
            current.collect_sources(store, Mock())
    store.close()


def test_wda_transport_timeout_does_not_mark_remaining_links_failed(tmp_path):
    from wda.usbmux.exceptions import HTTPError
    from automation.pdd.spider.workflow import FatalWorkflowError
    store = Store(tmp_path / 'sqlite.db')
    sources = [excel_source('猫粮', 'https://example.com/1'), excel_source('猫砂', 'https://example.com/2')]
    with patch('automation.pdd.spider.rank.read_leaderboards', return_value=sources), \
            patch('automation.pdd.spider.navigation.open_app_board', side_effect=HTTPError(TimeoutError('timed out'))) as navigate, \
            pytest.raises(FatalWorkflowError, match='WDA 通信异常'):
        current.collect_sources(store, Mock())
    navigate.assert_called_once()
    assert store.list(1)['state'] == 'failed'
    assert store.list(2)['state'] == 'pending'
    store.close()


def test_excel_loop_collects_each_board_before_opening_the_next(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    sources = [excel_source('猫粮', 'https://example.com/1'), excel_source('猫砂', 'https://example.com/2')]
    order = []
    with patch('automation.pdd.spider.rank.read_leaderboards', return_value=sources), \
            patch('automation.pdd.spider.navigation.open_app_board',
                  side_effect=lambda client, source: order.append(('open', source['name']))), \
            patch.object(current, 'current_board', side_effect=[('猫粮', ['畅销榜'], False), ('猫砂', ['畅销榜'], False)]), \
            patch.object(current, 'read_tab', return_value=(['排行', '标题'], [{'排行': 1, '标题': '商品'}])), \
            patch.object(current.config, 'OUTPUT_ROOT', tmp_path), \
            patch('automation.pdd.spider.full_flow.IosFullFlow') as flow:
        flow.return_value.run_list.side_effect = lambda list_id, *args: order.append(
            ('collect', store.list(list_id)['source_name']))
        assert current.collect_sources(store, runtime) == 0
    assert order == [('open', '猫粮'), ('collect', '猫粮'), ('open', '猫砂'), ('collect', '猫砂')]
    assert all(board['state'] == 'completed' for board in store.boards(current.today()))
    store.close()


def test_failed_ios_list_refreshes_snapshot_and_keeps_unchanged_product_id(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed(current.today(), [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, tmp_path, [], [{'排行': 1, '标题': '商品'}], 'ios')
    store.list_state(1, 'failed', '链接未完成')
    with patch.object(current, 'current_board', return_value=('猫粮', ['畅销榜'], False)), \
            patch.object(current, 'read_tab', return_value=(['排行', '标题'], [{'排行': 1, '标题': '商品'}])) as read, \
            patch.object(current.config, 'OUTPUT_ROOT', tmp_path), \
            patch('automation.pdd.spider.full_flow.IosFullFlow') as flow:
        assert current.collect(store, Mock(), full=True) == 0
    read.assert_called_once()
    assert flow.return_value.run_list.call_args.args[1][0]['标题'] == '商品'
    assert store.items(1)[0]['id'] == 1
    assert flow.call_args.kwargs == {'refresh': False, 'latest_snapshot': True}
    store.close()


def test_resume_uses_current_titles_instead_of_saved_titles(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed(current.today(), [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, tmp_path, [], [
        {'排行': 1, '标题': '旧商品', '产品ID': '111', '产品链接': 'https://example.com/?goods_id=111'},
        {'排行': 2, '标题': '未变化商品', '产品ID': '222', '产品链接': 'https://example.com/?goods_id=222'},
    ], 'ios')
    store.product_state(1, 'completed', details={'info': {'goods_id': '111'}})
    store.list_state(1, 'failed', '旧标题找不到')
    latest = [{'排行': 1, '标题': '当前商品'}, {'排行': 2, '标题': '未变化商品'}]
    with patch.object(current, 'current_board', return_value=('猫粮', ['畅销榜'], False)), \
            patch.object(current, 'read_tab', return_value=(['排行', '标题'], latest)), \
            patch.object(current.config, 'OUTPUT_ROOT', tmp_path), \
            patch('automation.pdd.spider.full_flow.IosFullFlow') as flow:
        assert current.collect(store, Mock(), full=True) == 0
    assert [row['标题'] for row in flow.return_value.run_list.call_args.args[1]] == ['当前商品', '未变化商品']
    assert [item['id'] for item in store.items(1)] == [1, 2]
    assert store.item(1)['goods_id'] is None
    assert store.item(1)['product_json'] == '{}'
    assert store.item(2)['goods_id'] == '222'
    store.close()


def test_completed_ios_source_does_not_connect_or_open(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed(current.today(), [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.list_state(store.lists(board['id'])[0]['id'], 'completed')
    runtime = Mock()
    with patch('automation.pdd.spider.rank.read_leaderboards', return_value=[excel_source('猫粮', 'https://example.com')]), \
            patch('automation.pdd.spider.navigation.open_app_board') as open_board:
        assert current.collect_sources(store, runtime) == 0
    runtime.ensure_wda.assert_not_called()
    open_board.assert_not_called()
    store.close()


def test_ranking_id_opens_only_saved_source_without_reading_excel(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        sources = [excel_source('猫粮', 'https://example.com/1'), excel_source('猫砂', 'https://example.com/2')]
        entries = store.seed_rankings('2026-10-31', sources, tmp_path)
        runtime = Mock()
        with patch('automation.pdd.spider.rank.read_leaderboards') as excel, \
                patch('automation.pdd.spider.navigation.open_app_board') as navigate, \
                patch.object(current, 'collect', return_value=0) as collect:
            assert current.collect_sources(store, runtime, ranking_id=entries[1]['id']) == 0
        excel.assert_not_called()
        navigate.assert_called_once_with(runtime.ensure_wda.return_value, {
            'name': '猫砂', 'main_name': '猫砂', 'url': 'https://example.com/2'})
        collect.assert_called_once_with(store, runtime, full=True, refresh=False, entry=entries[1])
        assert store.list(entries[0]['id'])['state'] == 'pending'
        store.list_state(entries[1]['id'], 'completed')
        with patch('automation.pdd.spider.navigation.open_app_board') as navigate:
            assert current.collect_sources(store, runtime, ranking_id=entries[1]['id']) == 0
            navigate.assert_not_called()
    finally:
        store.close()


def test_selected_ranking_preserves_date_and_directory(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    try:
        entry = store.seed_rankings('2026-10-31', [excel_source('猫粮', 'https://example.com/1')], tmp_path)[0]
        with patch.object(current, 'current_board', return_value=('猫粮', ['畅销榜'], False)), \
                patch.object(current, 'read_tab', return_value=(['排行', '标题'], [{'排行': 1, '标题': '商品'}])), \
                patch.object(current, 'today', return_value='2026-11-05'):
            assert current.collect(store, Mock(), entry=entry) == 0
        saved = store.list(entry['id'])
        assert saved['capture_date'] == '2026-10-31'
        assert saved['directory'] == entry['directory']
    finally:
        store.close()


def test_unknown_ranking_id_does_not_connect(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    runtime = Mock()
    try:
        with pytest.raises(ValueError, match='榜单 ID 不存在'):
            current.collect_sources(store, runtime, ranking_id=12)
        runtime.ensure_wda.assert_not_called()
    finally:
        store.close()


def test_refresh_reloads_completed_ranking_metrics(tmp_path):
    import json
    store = Store(tmp_path / 'sqlite.db')
    try:
        entry = store.seed_rankings('2026-11-05', [excel_source('猫粮', 'https://example.com/rank')], tmp_path)[0]
        store.save_rows(entry['id'], entry['directory'], ['排行', '标题', '价格'], [{'排行': 1, '标题': '旧商品', '价格': '10'}], 'ios')
        store.list_state(entry['id'], 'completed')
        entry = store.list(entry['id'])
        with patch.object(current, 'current_board', return_value=('猫粮', ['畅销榜'], False)), \
                patch.object(current, 'read_tab', return_value=(['排行', '标题', '价格'], [{'排行': 1, '标题': '新商品', '价格': '8'}])) as read:
            assert current.collect(store, Mock(), entry=entry, refresh=True) == 0
        read.assert_called_once()
        row = json.loads(store.items(entry['id'])[0]['row_json'])
        assert row['标题'] == '新商品'
        assert row['价格'] == '8'
    finally:
        store.close()
