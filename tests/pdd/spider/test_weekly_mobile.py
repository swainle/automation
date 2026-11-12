import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from automation.pdd.spider import weekly_mobile as flow
from automation.pdd.spider import mobile_page
from automation.pdd.spider.mobile_database import Store, WEBVIEW_SKIP, week_start, columns as pg_columns


pytestmark = pytest.mark.usefixtures('pg_database')

SOURCES = [{'name': '猫粮', 'main_name': '猫粮', 'tab': '畅销榜',
            'tab_index': 0, 'url': 'https://example.com/ranking'}]
PRODUCT_XML = '''<XCUIElementTypeApplication width="414" height="896">
 <XCUIElementTypeStaticText name="原标题1 商品当前标题"/>
 <XCUIElementTypeButton name="分享" visible="true"/>
 <XCUIElementTypeButton name="返回" visible="true"/>
 <XCUIElementTypeButton name="店铺" visible="true"/>
 </XCUIElementTypeApplication>'''
WEBVIEW_XML = '<XCUIElementTypeApplication><XCUIElementTypeWebView/></XCUIElementTypeApplication>'


@pytest.fixture(autouse=True)
def output_root(tmp_path, monkeypatch, pg_database):
    monkeypatch.setattr(flow.config, 'OUTPUT_ROOT', tmp_path)
    now = [0.0]
    monkeypatch.setattr(mobile_page, 'monotonic', lambda: now[0])
    monkeypatch.setattr(mobile_page, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))


def prepared(tmp_path, count=2):
    store = Store()
    entry = store.seed_rankings('2026-11-06', SOURCES, tmp_path / "data")[0]
    rows = [{'排行': n, '标题': f'原标题{n}', 'metrics': {'display_price': '¥19.9起'}} for n in range(1, count + 1)]
    store.save_rows(entry['id'], entry['directory'], [], rows)
    return store, store.list(entry['id']), rows


def test_weekly_identity_retains_history_and_month_week(tmp_path):
    store, entry, _ = prepared(tmp_path)
    same = store.seed_rankings('2026-11-08', SOURCES, tmp_path / "data")[0]
    assert same['id'] == entry['id']
    assert same['directory'] == entry['directory']
    assert same['capture_date'] == '2026-11-06'
    new = store.seed_rankings('2026-11-09', SOURCES, tmp_path / "data")[0]
    assert new['id'] != entry['id']
    store.save_rows(new['id'], new['directory'], [], [{'排行': 1, '标题': '下一周'}])
    item = store.items(new['id'])[0]
    assert (item['collection_month'], item['collection_month_week'], item['collection_year_week']) == ('2026-11', 2, 46)
    assert len(store.items(entry['id'])) == 2
    assert week_start('2027-01-01') == '2026-12-28'
    year_entry = store.seed_rankings('2027-01-01', SOURCES, tmp_path / "data")[0]
    store.save_rows(year_entry['id'], year_entry['directory'], [], [{'排行': 1, '标题': '跨年'}])
    item = store.items(year_entry['id'])[0]
    assert (item['collection_week_year'], item['collection_year_week']) == (2026, 53)
    store.close()


def test_schema_and_normalized_details_roundtrip(tmp_path):
    store, entry, _ = prepared(tmp_path)
    item = store.items(entry['id'])[0]
    columns = lambda table: pg_columns(store.db, table)
    assert not {'header_json', 'stages_json', 'last_error'} & columns('t_pdd_rankings')
    assert not {'goods_url', 'goods_id', 'shop_id', 'shop_name', 'sale_state', 'info_json', 'metrics_json',
                'raw_json', 'link_state', 'info_state', 'negative_comments_state', 'stages_json'} & columns('t_pdd_ranking_products')
    assert not {'sku_id', 'stock', 'available', 'image', 'raw_json'} & columns('t_pdd_product_skus')
    assert not {'comment_id', 'rating', 'is_negative', 'tags_json', 'media_json', 'raw_json'} & columns('t_pdd_product_comments')
    assert not store.db.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='ods' AND table_name='product_images'").fetchone()
    for kind in ('main', 'detail'):
        store.save_product_image(item['id'], {'image_type': kind, 'position': 1, 'local_path': f'{kind}/1.jpg'})
    details = {'sku': [{'SPU1': '鸡肉', 'price': '19.9', '图片路径': 'SKU/1.jpg'}],
               'negative_comments': [{'comment': '包装破损', 'specs': '鸡肉', 'negative_tags': ['包装差']} ]}
    store.save_progress(item['id'], details)
    store.save_progress(item['id'], details)
    store.close()
    store = Store()
    saved = json.loads(store.item(item['id'])['product_json'])
    assert saved['sku'][0]['图片路径'] == 'SKU/1.jpg'
    assert {i['image_type'] for i in saved['images']} == {'main', 'detail', 'sku'}
    assert saved['negative_comments'][0]['negative_tags'] == ['包装差']
    assert store.db.execute('SELECT tag FROM t_pdd_product_comments').fetchone()['tag'] == '包装差'
    assert store.db.execute('SELECT count(*) FROM t_pdd_product_comments').fetchone()['count'] == 1
    with pytest.raises(ValueError):
        store.save_product_image(item['id'], {'image_type': 'main', 'position': 2, 'local_path': '../escape.jpg'})
    store.close()


def test_snapshot_is_saved_once(tmp_path):
    store, entry, _ = prepared(tmp_path)
    store.save_rows(entry['id'], entry['directory'], [], [{'排行': 1, '标题': '变化', 'metrics': {'display_price': '¥9'}}])
    assert len(store.items(entry['id'])) == 2
    assert store.items(entry['id'])[0]['title'] == '原标题1'
    assert store.items(entry['id'])[0]['display_price'] == '¥19.9起'
    store.close()


def test_old_comments_requeue_only_comment_stage_and_dated_comments_stay_done(tmp_path):
    store, entry, _ = prepared(tmp_path)
    items = store.items(entry['id'])
    for item in items:
        for stage in ('main_images', 'sku', 'detail_images', 'negative_comments', 'product'):
            store.stage_state(entry['id'], stage, 'completed', item_id=item['id'])
    store.save_progress(items[0]['id'], {'negative_comments': [{'comment': '旧差评'}]})
    store.save_progress(items[1]['id'], {'negative_comments': [{'comment': '有日期', 'time': '2026-11-04'}]})
    store.list_state(entry['id'], 'completed')
    assert store.requeue_undated_comments(entry['id']) == 1
    first, second = store.items(entry['id'])
    assert first['comments_state'] == first['product_state'] == 'pending'
    assert all(first[key] == 'completed' for key in ('main_images_state', 'sku_state', 'detail_images_state'))
    assert second['product_state'] == 'completed'
    assert store.list(entry['id'])['collect_state'] == 'pending'
    assert store.requeue_undated_comments(entry['id']) == 0
    assert json.loads(second['product_json'])['negative_comments'][0]['time'] == '2026-11-04'
    store.close()


def test_product_step_resume_survives_reopen(tmp_path):
    store, entry, _ = prepared(tmp_path, 1)
    item = store.items(entry['id'])[0]
    for stage in ('product_screenshot', 'main_images'):
        store.stage_state(entry['id'], stage, 'completed', item_id=item['id'])
    collector = flow.WeeklyProduct(store, Mock())
    collector.main_images = Mock()
    collector.skus = Mock(side_effect=RuntimeError('SKU 中断'))
    collector.details, collector.comments = Mock(), Mock()
    with pytest.raises(RuntimeError, match='SKU 中断'):
        collector.collect(item, Path(item['directory']), {'标题': '当前标题'}, initial_xml=PRODUCT_XML)
    store.product_state(item['id'], 'failed')
    assert store.list(entry['id'])['last'] == 1
    assert store.list(entry['id'])['last_stage'] == 'sku'
    collector.main_images.assert_not_called()
    collector.details.assert_not_called()
    store.close()
    store = Store()
    collector = flow.WeeklyProduct(store, Mock())
    collector.main_images, collector.skus, collector.details, collector.comments = Mock(), Mock(), Mock(), Mock()
    collector.collect(store.item(item['id']), Path(item['directory']), {'标题': '更新标题'}, initial_xml=PRODUCT_XML)
    collector.main_images.assert_not_called()
    collector.skus.assert_called_once()
    collector.details.assert_called_once()
    collector.comments.assert_called_once()
    assert store.item(item['id'])['product_state'] == 'completed'
    assert store.item(item['id'])['title'] == '更新标题'
    store.close()


def patch_product_navigation(monkeypatch, xml=PRODUCT_XML):
    from automation.pdd.spider import full_flow
    monkeypatch.setattr(full_flow, 'prepare_top', Mock())
    monkeypatch.setattr(flow, 'product_element', Mock(return_value=SimpleNamespace(displayed=True, tap=Mock())))
    monkeypatch.setattr(flow, 'return_to_board', Mock())
    monkeypatch.setattr(flow.ios, '_current_page_source', Mock(return_value=xml))
    monkeypatch.setattr(flow.time, 'sleep', Mock())


def test_comment_interrupt_preserves_dates_already_read(tmp_path):
    store, entry, _ = prepared(tmp_path, 1)
    item = store.items(entry['id'])[0]
    for stage in ('main_images', 'sku', 'detail_images'):
        store.stage_state(entry['id'], stage, 'completed', item_id=item['id'])
    collector = flow.WeeklyProduct(store, Mock())
    def interrupt():
        collector.negative = [{'comment': '包装破损', 'time': '2026-10-13'}]
        raise KeyboardInterrupt
    collector.comments = interrupt
    with pytest.raises(KeyboardInterrupt):
        collector.collect(item, Path(item['directory']), {'标题': '当前标题'}, initial_xml=PRODUCT_XML)
    saved = json.loads(store.item(item['id'])['product_json'])
    assert saved['negative_comments'][0]['time'] == '2026-10-13'
    assert store.item(item['id'])['comments_state'] == 'failed'
    store.close()


def test_user_interrupt_is_not_replaced_by_return_failure(tmp_path, monkeypatch):
    store, entry, rows = prepared(tmp_path, 1)
    patch_product_navigation(monkeypatch)
    monkeypatch.setattr(flow.WeeklyProduct, 'collect', Mock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(flow, 'return_to_board', Mock(side_effect=RuntimeError('返回失败')))
    client = Mock()
    client.screenshot.return_value = Image.new('RGB', (100, 100))
    with pytest.raises(KeyboardInterrupt):
        flow.run_products(store, client, entry, rows)
    store.close()


def test_board_product_taps_native_title_and_checks_occurrence(monkeypatch):
    xml = '''<XCUIElementTypeApplication width="414" height="896">
      <XCUIElementTypeStaticText name="同名商品" x="10" y="1200" width="300" height="30" visible="false"/>
      <XCUIElementTypeStaticText name="同名商品" x="10" y="200" width="300" height="30" visible="true"/>
    </XCUIElementTypeApplication>'''
    monkeypatch.setattr(flow.ios, '_current_page_source', Mock(return_value=xml))
    monkeypatch.setattr(flow.ios, '_get_window_size', Mock(return_value=(414, 896)))
    client = Mock()
    native = [Mock(), Mock()]
    client.return_value.find_elements.return_value = native
    assert not flow.product_element(client, '同名商品').displayed
    element = flow.product_element(client, '同名商品', occurrence=2)
    assert element.displayed
    element.tap()
    native[1].tap.assert_called_once()
    native[0].tap.assert_not_called()
    client.click.assert_not_called()
    client._session_http.post.assert_not_called()
    with pytest.raises(RuntimeError, match='未找到排行商品标题'):
        flow.product_element(client, '同名商品', occurrence=3)


def test_wrong_product_is_rejected_before_screenshot_or_collection(tmp_path, monkeypatch):
    store, entry, rows = prepared(tmp_path, 1)
    patch_product_navigation(monkeypatch, PRODUCT_XML.replace('原标题1', '另一商品'))
    collect = Mock()
    monkeypatch.setattr(flow.WeeklyProduct, 'collect', collect)
    client = Mock()
    with pytest.raises(RuntimeError, match='商品身份不符'):
        flow.run_products(store, client, entry, rows)
    client.screenshot.assert_not_called()
    collect.assert_not_called()
    assert store.items(entry['id'])[0]['product_state'] == 'failed'
    store.close()


def test_webview_skips_whole_product_and_only_retries_flags(tmp_path, monkeypatch):
    store, entry, rows = prepared(tmp_path)
    items = store.items(entry['id'])
    patch_product_navigation(monkeypatch, WEBVIEW_XML)
    client = Mock()
    flow.run_products(store, client, entry, rows)
    assert all(i['product_state'] == WEBVIEW_SKIP for i in store.items(entry['id']))
    flow.product_element.reset_mock()
    flow.run_products(store, client, entry, rows)
    flow.product_element.assert_not_called()
    store.product_state(items[1]['id'], 'pending')
    monkeypatch.setattr(flow.ios, '_current_page_source', Mock(return_value=PRODUCT_XML))
    def complete(self, item, *args, **kwargs):
        self.store.product_state(item['id'], 'completed')
    monkeypatch.setattr(flow.WeeklyProduct, 'collect', complete)
    client.screenshot.side_effect = lambda: Image.new('RGB', (100, 100))
    flow.run_products(store, client, entry, rows, only_webview=True)
    assert flow.product_element.call_count == 1
    assert store.item(items[0]['id'])['product_state'] == 'completed'
    assert store.item(items[1]['id'])['product_state'] == 'pending'
    store.close()


def test_product_waits_through_transient_webview_without_persisting_skip(tmp_path, monkeypatch):
    store, entry, rows = prepared(tmp_path, 1)
    patch_product_navigation(monkeypatch)
    read = Mock(side_effect=[WEBVIEW_XML, PRODUCT_XML])
    monkeypatch.setattr(flow.ios, '_current_page_source', read)
    def complete(self, item, *args, **kwargs):
        assert kwargs['initial_xml'] == PRODUCT_XML
        self.store.product_state(item['id'], 'completed')
    monkeypatch.setattr(flow.WeeklyProduct, 'collect', complete)
    client = Mock()
    client.screenshot.return_value = Image.new('RGB', (100, 100))
    flow.run_products(store, client, entry, rows)
    assert read.call_count == 2
    assert store.items(entry['id'])[0]['product_state'] == 'completed'
    store.close()


def test_completed_or_webview_board_does_not_connect(tmp_path, monkeypatch):
    store, entry, _ = prepared(tmp_path)
    monkeypatch.setattr(flow, 'today', lambda: '2026-11-07')
    monkeypatch.setattr(flow, 'read_leaderboards', lambda _: SOURCES)
    runtime = Mock()
    for state in ('completed', WEBVIEW_SKIP):
        store.list_state(entry['id'], state)
        assert flow.collect_sources(store, runtime) == 0
    runtime.ensure_wda.assert_not_called()
    store.list_state(entry['id'], 'pending')
    assert flow.collect_sources(store, runtime, only_webview=True) == 0
    runtime.ensure_wda.assert_not_called()
    store.close()


def test_unified_cli(monkeypatch):
    from automation.pdd.spider import __main__, commands
    execute = Mock(return_value=0)
    monkeypatch.setattr(commands, 'execute', execute)
    assert __main__.main(['--ranking-id', '1', '--ranking-product-id', '2']) == 0
    assert execute.call_args.kwargs['args'].mobile
    with pytest.raises(SystemExit):
        __main__.main(['--mobile'])


def test_board_webview_flag_survives_runs(tmp_path, monkeypatch):
    from automation.pdd.spider import navigation
    store = Store()
    monkeypatch.setattr(flow, 'today', lambda: '2026-11-06')
    monkeypatch.setattr(flow.config, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(flow, 'read_leaderboards', lambda _: SOURCES)
    monkeypatch.setattr(navigation, 'open_app_board', Mock(return_value=WEBVIEW_XML))
    monkeypatch.setattr(flow.ios, '_current_page_source', Mock(return_value=WEBVIEW_XML))
    runtime = Mock()
    assert flow.collect_sources(store, runtime) == 0
    assert store.list(1)['collect_state'] == WEBVIEW_SKIP
    assert navigation.open_app_board.call_count == 1
    assert flow.collect_sources(store, runtime) == 0
    assert navigation.open_app_board.call_count == 1
    assert flow.collect_sources(store, runtime, only_webview=True) == 0
    assert navigation.open_app_board.call_count == 2
    store.close()


def test_inaccessible_board_is_detected_during_navigation(monkeypatch):
    from automation.pdd.spider import navigation
    value = Mock()
    value._session_http.post.return_value.value = 1
    value.http.get.return_value.value = {'bundleId': flow.ios.PDD_BUNDLE_ID}
    monkeypatch.setattr(navigation, '_accept_app_prompt', lambda _: False)
    monkeypatch.setattr(flow.ios, '_current_page_source', lambda _: WEBVIEW_XML)
    monkeypatch.setattr(navigation.time, 'sleep', lambda _: None)
    with pytest.raises(flow.WebviewElementSkip):
        navigation.open_app_board(value, SOURCES[0], inspect_xml=flow.require_elements)


def test_switch_rejects_existing_week_before_moving_data(tmp_path):
    from automation.pdd.spider.switch_data import switch_data
    store, entry, _ = prepared(tmp_path, 1)
    store.seed_rankings('2026-11-10', SOURCES, tmp_path / "data")
    store.close()
    with pytest.raises(ValueError, match='目标周'):
        switch_data(flow.config.POSTGRES_URL, '2026-11-06', '2026-11-09')
    store = Store()
    assert store.list(entry['id'])['capture_date'] == '2026-11-06'
    store.close()


def test_resume_does_not_recollect_ranking_or_images(tmp_path, monkeypatch):
    from automation.pdd.spider import navigation
    store = Store()
    rows = [{'排行': 1, '标题': '快照标题'}]
    monkeypatch.setattr(flow, 'today', lambda: '2026-11-06')
    monkeypatch.setattr(flow.config, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(flow, 'read_leaderboards', lambda _: SOURCES)
    monkeypatch.setattr(navigation, 'open_app_board', Mock(return_value=PRODUCT_XML))
    monkeypatch.setattr(flow.ios, '_current_page_source', Mock(return_value=PRODUCT_XML))
    monkeypatch.setattr(flow.ios, '_rank_details', lambda *_: {'主榜单名称': '猫粮'})
    monkeypatch.setattr(flow.ios, '_guess_tab', lambda *_: '畅销榜')
    read = Mock(return_value=(['排行', '标题'], rows))
    monkeypatch.setattr(flow, 'read_tab', read)
    monkeypatch.setattr(flow.ios, '_parse_rows', lambda *_: [{'排行': 1, '标题': '当前标题'}])
    monkeypatch.setattr(flow, 'prepare_top', Mock(return_value=(PRODUCT_XML, [])))
    def capture(client, current_rows, xml, directory, **kwargs):
        assert kwargs == {'compact': True}
        path = directory / '榜01/产品榜单截图.jpg'
        path.parent.mkdir(parents=True)
        Image.new('RGB', (50, 50)).save(path)
    capture_mock = Mock(side_effect=capture)
    monkeypatch.setattr(flow.longshot, 'capture', capture_mock)
    process = Mock(side_effect=RuntimeError('商品中断'))
    monkeypatch.setattr(flow, 'run_products', process)
    assert flow.collect_sources(store, Mock()) == 1
    assert (Path(store.list(1)['directory']) / '榜单数据.xlsx').is_file()
    process.side_effect = None
    def complete(store, client, entry, live_rows, **kwargs):
        assert live_rows[0]['标题'] == '当前标题'
        item = store.items(entry['id'])[0]
        store.product_state(item['id'], 'completed')
        store.item_state(item['id'], 'completed')
    process.side_effect = complete
    assert flow.collect_sources(store, Mock()) == 0
    assert read.call_count == capture_mock.call_count == 1
    assert store.items(1)[0]['title'] == '快照标题'
    assert store.list(1)['collect_state'] == 'completed'
    store.close()


def test_longshot_invalidates_pre_scroll_coordinates(tmp_path, monkeypatch):
    store, entry, rows = prepared(tmp_path, 1)
    client = Mock()
    client._rank_last_elements = [{'text': '旧坐标', 'y': 100}]
    monkeypatch.setattr(flow, 'prepare_top', Mock(return_value=(PRODUCT_XML, [])))
    def capture(client, rows, xml, directory, **kwargs):
        path = directory / '榜01/产品榜单截图.jpg'
        path.parent.mkdir(parents=True)
        Image.new('RGB', (50, 50)).save(path)
    monkeypatch.setattr(flow.longshot, 'capture', capture)
    flow.capture_ranking(store, client, entry, rows, [])
    assert '_rank_last_elements' not in client.__dict__
    assert store.list(entry['id'])['screenshot_state'] == 'completed'
    store.close()


def test_switch_weekly_dates_updates_calendar_and_image_paths(tmp_path):
    from automation.pdd.spider.switch_data import switch_data
    store, entry, _ = prepared(tmp_path, 1)
    item = store.items(entry['id'])[0]
    Path(entry['directory']).mkdir(parents=True)
    store.save_product_image(item['id'], {'image_type': 'detail', 'position': 1, 'local_path': '详情/1.jpg'})
    store.save_progress(item['id'], {'sku': [{'SPU1': '鸡肉', 'price': '19', '图片路径': 'SKU/1.jpg'}]})
    store.close()
    switch_data(flow.config.POSTGRES_URL, '2026-11-06', '2026-11-09')
    store = Store()
    assert store.list(entry['id'])['week_start'] == '2026-11-09'
    item = store.item(item['id'])
    assert item['collection_month_week'] == 2
    assert item['collection_year_week'] == 46
    assert item['directory'].endswith('2026-11-09\\榜01')
    for table in ('t_pdd_product_skus', 't_pdd_products_detail_image'):
        path = store.db.execute(f'SELECT local_path FROM {table}').fetchone()['local_path']
        assert '/2026-11-09/' in path
        assert '2026-11-06' not in path
    store.close()
