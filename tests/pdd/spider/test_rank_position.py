from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from automation.pdd.spider import unified, weekly_mobile as flow
from automation.pdd.spider.mobile_database import Store, PRODUCT_COLUMNS


@pytest.fixture(autouse=True)
def mock_analysis(monkeypatch):
    runner = Mock()
    runner.process.return_value = runner.is_complete.return_value = True
    runner.has_failed.return_value = False
    monkeypatch.setattr(unified, 'AnalysisRunner', Mock(return_value=runner))


def title(text, y, order):
    return dict(type='StaticText', text=text, x=100, y=y, w=220, h=30, order=order)


def snapshot(elements):
    import xml.etree.ElementTree as ET
    root = ET.Element('root')
    for e in elements:
        ET.SubElement(root, 'XCUIElementTypeStaticText', name=e['text'], label=e['text'],
                      x=str(e['x']), y=str(e['y']), width=str(e['w']), height=str(e['h']),
                      visible=str(e.get('visible', True)).lower())
    return ET.tostring(root, encoding='unicode')


def test_large_scroll_uses_visible_titles_then_small_correction(monkeypatch):
    rows = [{'排行': i, '标题': f'商品{i}'} for i in range(1, 7)]
    baseline = [title(row['标题'], 200 + i * 400, i) for i, row in enumerate(rows)]
    client = Mock()
    client._rank_top_elements = baseline
    offsets = iter([0, 520, 1040, 1560, 1700])
    monkeypatch.setattr(flow.ios, '_get_window_size', lambda _: (414, 800))
    monkeypatch.setattr(flow.ios, 'require_pdd_foreground', Mock())
    def read(*_):
        offset = next(offsets)
        return snapshot([dict(e, y=e['y'] - offset) for e in baseline])
    monkeypatch.setattr(flow, 'wait_for_elements', read)
    monkeypatch.setattr(flow.ios, '_rank_details', lambda *_: {'主榜单名称': '猫粮'})
    monkeypatch.setattr(flow.ios, '_guess_tab', lambda _: '畅销榜')
    drag = Mock()
    tap = Mock(return_value=True)
    monkeypatch.setattr(flow.longshot, 'drag_for_screenshot', drag)
    monkeypatch.setattr(flow.ios, '_tap_xy', tap)
    monkeypatch.setattr(flow.time, 'sleep', Mock())
    element = flow.locate_product(client, dict(main_name='猫粮', tab='畅销榜'), rows, 6)
    element.tap()
    travels = [call.args[2] - call.args[3] for call in drag.call_args_list]
    assert travels[0] == 520
    assert 0 < travels[-1] <= 144
    tap.assert_called_once_with(client, 210, 515, '排行 6：商品6')


def test_duplicate_title_uses_board_occurrence_and_does_not_return_to_top(monkeypatch):
    rows = [{'排行': i, '标题': '同名'} for i in (1, 2)]
    baseline = [title('同名', 200, 0), title('同名', 900, 1)]
    client = Mock()
    client._rank_top_elements = baseline
    monkeypatch.setattr(flow.ios, '_get_window_size', lambda _: (414, 800))
    monkeypatch.setattr(flow.ios, 'require_pdd_foreground', Mock())
    monkeypatch.setattr(flow, 'wait_for_elements', lambda *_: snapshot([dict(e, y=e['y'] - 500) for e in baseline]))
    monkeypatch.setattr(flow.ios, '_rank_details', lambda *_: {'主榜单名称': '猫粮'})
    monkeypatch.setattr(flow.ios, '_guess_tab', lambda _: '畅销榜')
    tap = Mock(return_value=True)
    monkeypatch.setattr(flow.ios, '_tap_xy', tap)
    flow.locate_product(client, dict(main_name='猫粮', tab='畅销榜'), rows, 2).tap()
    assert tap.call_args.args[2] == 415


def test_no_visible_matching_title_prevents_blind_scroll(monkeypatch):
    client = Mock()
    client._rank_top_elements = [title('目标', 200, 0)]
    monkeypatch.setattr(flow.ios, '_get_window_size', lambda _: (414, 800))
    monkeypatch.setattr(flow.ios, 'require_pdd_foreground', Mock())
    monkeypatch.setattr(flow, 'wait_for_elements', lambda *_: '<root/>')
    monkeypatch.setattr(flow.ios, '_xcui_texts', lambda _: [])
    monkeypatch.setattr(flow.ios, '_rank_details', lambda *_: {'主榜单名称': '猫粮'})
    monkeypatch.setattr(flow.ios, '_guess_tab', lambda _: '畅销榜')
    drag = Mock()
    monkeypatch.setattr(flow.longshot, 'drag_for_screenshot', drag)
    with pytest.raises(RuntimeError, match='可见范围'):
        flow.locate_product(client, dict(main_name='猫粮', tab='畅销榜'), [{'排行': 1, '标题': '目标'}], 1)
    drag.assert_not_called()


def test_hidden_title_inside_screen_is_not_clicked(monkeypatch):
    client = Mock()
    client._rank_top_elements = [title('目标', 200, 0)]
    page = snapshot([dict(title('目标', 200, 0), visible=False)])
    monkeypatch.setattr(flow.ios, '_get_window_size', lambda _: (414, 800))
    monkeypatch.setattr(flow.ios, 'require_pdd_foreground', Mock())
    monkeypatch.setattr(flow, 'wait_for_elements', lambda *_: page)
    monkeypatch.setattr(flow.ios, '_rank_details', lambda *_: {'主榜单名称': '猫粮'})
    monkeypatch.setattr(flow.ios, '_guess_tab', lambda _: '畅销榜')
    tap = Mock()
    monkeypatch.setattr(flow.ios, '_tap_xy', tap)
    with pytest.raises(RuntimeError, match='可见范围'):
        flow.locate_product(client, dict(main_name='猫粮', tab='畅销榜'), [{'排行': 1, '标题': '目标'}], 1)
    tap.assert_not_called()


def test_snapshot_only_saves_count_and_each_item_is_upserted_separately():
    store = Store.__new__(Store)
    store.db = Mock()
    store.db.transaction.side_effect = lambda: nullcontext()
    rows = [{'排行': 1, '标题': '一'}, {'排行': 2, '标题': '二'}]
    store.save_snapshot(1, rows)
    assert store.db.execute.call_count == 1
    assert 't_pdd_ranking_products' not in store.db.execute.call_args.args[0]
    store.list = Mock(return_value={'capture_date': '2026-11-19'})
    store.item = Mock(return_value={'id': 12})
    store.db.execute.return_value.fetchone.return_value = {'id': 12}
    assert store.save_item(1, Path('board'), rows[1]) == {'id': 12}
    query, values = store.db.execute.call_args.args
    assert 'ON CONFLICT(ranking_id,rank) DO UPDATE' in query
    assert values[:3] == [1, 2, '二']
    assert 'main_images_state' not in query


@pytest.mark.parametrize('wrong_page', [False, True])
def test_first_capture_inserts_only_after_entering_and_verifying_product(tmp_path, monkeypatch, wrong_page):
    rows = [{'排行': 1, '标题': '一'}, {'排行': 2, '标题': '二'}]
    store = Mock()
    store.items.return_value = []
    store.db.transaction.side_effect = lambda: nullcontext()
    events = []
    def locate(client, entry, rows, rank):
        return SimpleNamespace(displayed=True, tap=lambda: events.append(('entered', rank)))
    def save(rid, directory, row):
        events.append(('saved', row['排行']))
        return dict(id=row['排行'], rank=row['排行'], product_screenshot_state='completed')
    store.save_item.side_effect = save
    def checks(xml, title):
        events.append(('verified', title))
        return [], dict(title_candidates=['wrong' if wrong_page else title], observed_title=title)
    monkeypatch.setattr(flow, 'locate_product', locate)
    monkeypatch.setattr(flow, 'wait_for_elements', lambda *args, **kwargs: 'xml')
    monkeypatch.setattr(flow, 'product_page_checks', checks)
    monkeypatch.setattr(flow, 'return_to_board', Mock())
    monkeypatch.setattr(flow, 'WeeklyProduct', Mock())
    top = Mock(side_effect=AssertionError('不应回顶'))
    monkeypatch.setattr('automation.pdd.spider.full_flow.prepare_top', top)
    entry = dict(id=1, directory=str(tmp_path), main_name='猫粮', tab='畅销榜')
    if wrong_page:
        with pytest.raises(RuntimeError, match='商品身份不符'):
            flow.run_products(store, Mock(), entry, rows, single_round=True)
        store.save_item.assert_not_called()
    else:
        flow.run_products(store, Mock(), entry, rows, single_round=True)
        assert events == [('entered', 1), ('verified', '一'), ('saved', 1),
                          ('entered', 2), ('verified', '二'), ('saved', 2)]
    top.assert_not_called()


def test_partial_completed_rows_do_not_hide_uninserted_ranks(monkeypatch, tmp_path):
    entry = dict(id=1, product_count=3, directory=str(tmp_path))
    Image.new('RGB', (20, 20)).save(tmp_path / '榜单长图.jpg')
    store = Mock()
    store.db.execute.return_value.fetchone.return_value = {'id': 1}
    store.list.return_value = entry
    items = [dict(id=1, **dict.fromkeys(PRODUCT_COLUMNS, 'completed'))]
    store.items.side_effect = lambda _: list(items)
    def mobile(*args, **kwargs):
        items.extend([dict(id=i, **dict.fromkeys(PRODUCT_COLUMNS, 'completed')) for i in (2, 3)])
        return 3
    run = Mock(side_effect=mobile)
    monkeypatch.setattr(unified, 'mobile_round', run)
    assert unified.collect(store, Mock(), ranking_id=1) == 0
    run.assert_called_once()


def test_resume_same_product_preserves_completed_stages(tmp_path, monkeypatch):
    item = dict(id=1, rank=1, title='一 商品页标题', directory=str(tmp_path / '榜01'),
                product_screenshot_state='completed', main_images_state='completed')
    store = Mock()
    store.items.return_value = [item]
    store.save_item.return_value = item
    store.db.transaction.side_effect = lambda: nullcontext()
    reset = Mock(side_effect=AssertionError('同一商品不应清理已有阶段'))
    monkeypatch.setattr(unified, 'reset_mobile', reset)
    monkeypatch.setattr(flow, 'locate_product', Mock(return_value=SimpleNamespace(displayed=True, tap=Mock())))
    monkeypatch.setattr(flow, 'wait_for_elements', lambda *args, **kwargs: 'xml')
    monkeypatch.setattr(flow, 'product_page_checks', lambda *_: ([], dict(title_candidates=['一 商品页标题'], observed_title='一 商品页标题')))
    monkeypatch.setattr(flow, 'return_to_board', Mock())
    collector = Mock()
    monkeypatch.setattr(flow, 'WeeklyProduct', Mock(return_value=collector))
    client = Mock()
    flow.run_products(store, client, dict(id=1, directory=str(tmp_path), main_name='猫粮', tab='畅销榜'),
                      [{'排行': 1, '标题': '一'}], single_round=True)
    reset.assert_not_called()
    client.screenshot.assert_not_called()
    assert collector.collect.call_args.args[0]['main_images_state'] == 'completed'


def test_status_total_includes_products_not_inserted_yet(monkeypatch):
    from automation.pdd.spider import health
    db = Mock()
    db.execute.return_value.fetchall.return_value = [dict(id=1, main_name='猫粮', tab_name='畅销榜',
        state='running', total=20, done=1, identity_done=0, matched_done=0, point_done=0, skipped=0, failed=0)]
    monkeypatch.setattr(health, 'columns', lambda *_: {'product_count'})
    display = Mock()
    monkeypatch.setattr(health, 'table', display)
    monkeypatch.setattr(unified, 'check', Mock())
    health.collection_status(db)
    assert 'GREATEST(COUNT(p.id),r.product_count)' in db.execute.call_args.args[0]
    assert display.call_args.args[2][0][3] == '1/20'


def test_refresh_keeps_completed_crops_and_identical_long_image(tmp_path, monkeypatch):
    entry = dict(id=1, directory=str(tmp_path), main_name='猫粮', tab='畅销榜',
                 source_name='猫粮', source_url='https://example.com/rank')
    rows = [{'排行': 1, '标题': '一'}, {'排行': 2, '标题': '二'}]
    store = Mock()
    store.items.return_value = [dict(id=11, rank=1, **dict.fromkeys(PRODUCT_COLUMNS, 'completed'))]
    (tmp_path / '榜01').mkdir()
    done = tmp_path / '榜01/产品榜单截图.jpg'
    done.write_bytes(b'keep')
    output = tmp_path / '榜单长图.jpg'
    output.write_bytes(b'long')
    old_time = output.stat().st_mtime_ns
    monkeypatch.setattr('automation.pdd.spider.navigation.open_app_board', lambda *args, **kwargs: 'xml')
    monkeypatch.setattr(unified.ios, '_xcui_texts', lambda _: [])
    monkeypatch.setattr(unified.ios, '_guess_tab', lambda _: '畅销榜')
    monkeypatch.setattr(unified.ios, '_rank_details', lambda *_: {'主榜单名称': '猫粮'})
    monkeypatch.setattr('automation.pdd.spider.rank.current.read_tab', lambda *args, **kwargs: (['排行', '标题'], rows))
    monkeypatch.setattr('automation.pdd.spider.full_flow.prepare_top', lambda *args: ('xml', []))
    def capture(client, rows, xml, directory, **kwargs):
        (directory / '榜单长图.jpg').write_bytes(b'long')
        for rank in (1, 2):
            folder = directory / f'榜{rank:02d}'
            folder.mkdir()
            (folder / '产品榜单截图.jpg').write_bytes(b'new')
    capture_mock = Mock(side_effect=capture)
    monkeypatch.setattr(unified.longshot, 'capture', capture_mock)
    monkeypatch.setattr('automation.pdd.spider.full_flow.valid_image', lambda p: p.is_file())
    monkeypatch.setattr(flow, 'run_products', Mock())
    refreshed = set()
    for _ in range(2):
        assert unified.mobile_round(store, Mock(), entry, refreshed=refreshed) == 2
    assert capture_mock.call_count == 1
    assert output.stat().st_mtime_ns == old_time
    assert done.read_bytes() == b'keep'
    assert (tmp_path / '榜02/产品榜单截图.jpg').read_bytes() == b'new'
    store.save_rows.assert_not_called()
    store.save_item.assert_not_called()
