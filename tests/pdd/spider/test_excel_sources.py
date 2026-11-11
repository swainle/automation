from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.rank import read_leaderboards
from automation.pdd.spider.rank import current
from automation.pdd.spider.ranking import Ranking
from automation.pdd.spider.utils import write_xlsx


def workbook(tmp_path, rows):
    path = tmp_path / '来源.xlsx'
    write_xlsx(path, ['种类', '来源', '主榜', '子榜', '链接'], rows)
    return path


def sources(tmp_path):
    return read_leaderboards(workbook(tmp_path, [
        ['猫', '猫粮【三个榜】', '猫粮', '畅销榜', 'https://example.com/rank?list_id=1&activeTab=0'],
        ['猫', '猫粮【三个榜】', '猫粮', '好评榜', 'https://example.com/rank?list_id=1&activeTab=1'],
        ['猫', '猫粮【三个榜】', '猫粮', '降价版', 'https://example.com/rank?list_id=1&activeTab=2'],
        ['猫', '失效来源', '猫粮', None, 'https://example.com/invalid'],
    ]))


def test_excel_reads_each_tab_without_deduplicating_active_tab(tmp_path):
    rows = sources(tmp_path)
    assert len(rows) == 4
    assert [row['tab'] for row in rows] == ['畅销榜', '好评榜', '降价榜', '']
    assert rows[1]['url'].endswith('activeTab=1')
    assert rows[0]['name'] == '猫粮【三个榜】'
    assert rows[0]['main_name'] == '猫粮'


@pytest.mark.parametrize('row', [
    ['猫', '来源', '主榜', '好评榜', 'https://example.com/?activeTab=0'],
    ['猫', '来源', '主榜', '未知榜', 'https://example.com/'],
    ['猫', '来源', '主榜', '畅销榜', '主榜名称不是链接'],
])
def test_invalid_row_rejected_before_collection(tmp_path, row):
    with pytest.raises(ValueError):
        read_leaderboards(workbook(tmp_path, [row]))


def test_repeated_source_names_seed_distinct_tabs_and_keep_product_ids(tmp_path):
    store = Store(tmp_path / 'test.db')
    rows = sources(tmp_path)
    entries = store.seed_rankings('2026-11-05', rows, tmp_path / 'out')
    store.save_rows(entries[0]['id'], tmp_path / 'out/猫粮/畅销榜/2026-11-05', [], [{'排行': 1, '标题': '商品'}], 'ios')
    store.list_state(entries[0]['id'], 'completed')
    ids = store.db.execute('SELECT id FROM ranking_products').fetchall()
    resumed = store.seed_rankings('2026-11-05', rows, tmp_path / 'out')
    assert [e['id'] for e in entries] == [e['id'] for e in resumed]
    assert resumed[0]['state'] == 'completed'
    assert store.db.execute('SELECT id FROM ranking_products').fetchall() == ids
    assert resumed[3]['state'] == 'failed' and resumed[3]['last_error'] == '链接失效'
    assert list((tmp_path / 'out/猫粮/2026-11-05').iterdir()) == []
    assert resumed[2]['directory'] == str((tmp_path / 'out/猫粮/降价榜/2026-11-05').resolve())
    store.close()


def test_ios_only_collects_each_configured_tab_and_uses_main_directory(tmp_path):
    rows = sources(tmp_path)
    store = Store(tmp_path / 'test.db')
    runtime = Mock()
    with patch('automation.pdd.spider.rank.read_leaderboards', return_value=rows), \
            patch.object(current.config, 'OUTPUT_ROOT', tmp_path / 'out'), \
            patch('automation.pdd.spider.navigation.open_app_board') as navigate, \
            patch.object(current, 'current_board', return_value=('猫粮', ['畅销榜', '好评榜', '降价榜'], True)), \
            patch.object(current.ios, '_switch_rank_tab'), \
            patch.object(current, 'read_tab', return_value=(['排行', '标题'], [{'排行': 1, '标题': '商品'}])) as read, \
            patch('automation.pdd.spider.full_flow.IosFullFlow'):
        assert current.collect_sources(store, runtime) == 1  # Empty D is a reported failed link.
        assert [call.args[2] for call in read.call_args_list] == ['畅销榜', '好评榜', '降价榜']
        assert navigate.call_count == 3
        assert store.db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 4
        assert store.db.execute('SELECT count(*) FROM ranking_products').fetchone()[0] == 3
        read.reset_mock()
        navigate.reset_mock()
        assert current.collect_sources(store, runtime) == 1
        read.assert_not_called()
        navigate.assert_not_called()
    store.close()


def test_wrong_main_is_not_saved_under_configured_product(tmp_path):
    store = Store(tmp_path / 'test.db')
    entry = store.seed_rankings(current.today(), sources(tmp_path)[:1], tmp_path / 'out')[0]
    with patch.object(current, 'current_board', return_value=('其他主榜', ['畅销榜'], False)), \
            patch.object(current, 'read_tab') as read, pytest.raises(RuntimeError, match='不对应'):
        current.collect(store, Mock(), entry=entry)
    read.assert_not_called()
    assert not store.items(entry['id'])
    store.close()


def test_changed_b_name_does_not_duplicate_existing_main_tab(tmp_path):
    store = Store(tmp_path / 'test.db')
    rows = sources(tmp_path)[:1]
    entry = store.seed_rankings('2026-11-05', rows, tmp_path / 'out')[0]
    rows[0]['name'] = '另一个来源名称'
    with pytest.raises(ValueError, match='请先合并'):
        store.seed_rankings('2026-11-05', rows, tmp_path / 'out')
    assert store.db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 1
    assert store.list(entry['id'])['source_name'] == '猫粮【三个榜】'
    store.close()


@pytest.mark.parametrize('device', ['sogou', 'ios'])
def test_browser_processes_only_configured_rows_with_repeated_source_names(tmp_path, device):
    store = Store(tmp_path / 'test.db')
    runtime = Mock()
    runner = Ranking(store, runtime, SimpleNamespace(name=device))
    def complete(day, board, entry, device):
        assert board['main_name'] == '猫粮'
        assert board['url'].endswith('activeTab=' + str(entry['tab_index']))
        store.list_state(entry['id'], 'completed')
    with patch.object(current.config, 'OUTPUT_ROOT', tmp_path / 'out'), patch.object(runner, 'process', side_effect=complete) as process:
        assert runner.run(sources(tmp_path), day='2026-11-05') == 1
    assert process.call_count == 3
    if device == 'ios':
        runtime.ensure_driver.assert_called_once()
        runtime.start_ranking_browser.assert_not_called()
    runtime.close.assert_called_once()
    store.close()


def test_duplicate_main_tab_with_different_source_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='重复主榜/子榜'):
        read_leaderboards(workbook(tmp_path, [
            ['猫', '来源甲', '猫粮', '畅销榜', 'https://example.com/?activeTab=0'],
            ['猫', '来源乙', '猫粮', '畅销榜', 'https://example.com/?activeTab=0'],
        ]))


def test_product_table_order_follows_main_and_tab_in_excel(tmp_path):
    from automation.pdd.spider.schedule import ordered_product_tables
    path = workbook(tmp_path, [
        ['猫', '来源甲', '猫粮', '好评榜', 'https://example.com/?activeTab=1'],
        ['猫', '来源乙', '猫粮', '畅销榜', 'https://example.com/?activeTab=0'],
    ])
    first = tmp_path / 'out/猫粮/好评榜/2026-11-05/榜单数据.xlsx'
    second = tmp_path / 'out/猫粮/畅销榜/2026-11-05/榜单数据.xlsx'
    for file in (first, second):
        write_xlsx(file, ['排行'], [[1]])
    assert ordered_product_tables(tmp_path / 'out', path) == [first, second]


def test_browser_checks_main_and_builds_c_d_directory(tmp_path):
    from automation.pdd.spider.devices import Sogou
    runtime = Mock(ranking_tab=1)
    snapshot = {'heading': '猫粮好评榜', 'cards': [{'title': '商品', 'text': '好评率99%', 'goods_id': '1'}]}
    runtime.ranking_request.side_effect = lambda action, params: snapshot if action == 'ranking_read' else {}
    board = {'name': '表格来源', 'main_name': '猫粮', 'url': 'https://example.com/?activeTab=1'}
    entry = {'tab': '好评榜', 'tab_index': 1}
    with patch.object(current.config, 'OUTPUT_ROOT', tmp_path):
        _, rows, directory = Sogou(runtime).read(board, entry, '2026-11-05')
        assert directory == tmp_path / '猫粮/好评榜/2026-11-05'
        assert rows[0]['主榜单名称'] == '猫粮'
        snapshot['heading'] = '其他主榜好评榜'
        with pytest.raises(RuntimeError, match='不对应'):
            Sogou(runtime).read(board, entry, '2026-11-05')
