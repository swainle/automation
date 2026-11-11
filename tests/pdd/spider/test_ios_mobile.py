from pathlib import Path
from types import SimpleNamespace
import sqlite3
import xml.etree.ElementTree as ET

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.fixed_rankings import migrate
from automation.pdd.spider.ios_product import sku_groups, sku_summary, review_rows


def test_fixed_rankings_preserves_latest_children_and_stable_ids(tmp_path):
    path = tmp_path / 'sqlite.db'
    store = Store(path)
    sources = [{'name': 'B', 'main_name': '主B', 'tab': '畅销榜', 'url': 'https://example.com/b', 'tab_index': 0},
               {'name': 'A', 'main_name': '主A', 'tab': '畅销榜', 'url': 'https://example.com/a', 'tab_index': 0}]
    for day in ('2026-11-04', '2026-11-05'):
        for entry in store.seed_rankings(day, sources, tmp_path):
            store.save_rows(entry['id'], Path(entry['directory']), ['排行', '标题'], [{'排行': 1, '标题': day + entry['source_name']}], 'ios')
    latest_item = store.items(4)[0]['id']
    store.save_product_image(latest_item, {'image_type': 'main', 'position': 1, 'local_path': '主图/1.jpg', 'state': 'completed'})
    store.close()
    backup = migrate(path, sources)
    assert backup.is_file()
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 4
    store = Store(path)
    assert [(r['id'], r['source_name'], r['capture_date']) for r in store.db.execute('SELECT * FROM rankings ORDER BY id')] == [(1, 'B', '2026-11-05'), (2, 'A', '2026-11-05')]
    assert store.item(latest_item)['list_id'] == 2
    assert store.db.execute('SELECT count(*) FROM product_images').fetchone()[0] == 1
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    entries = store.seed_rankings('2026-11-06', sources, tmp_path)
    assert [e['id'] for e in entries] == [1, 2]
    assert all(e['state'] == 'pending' for e in entries)
    assert store.db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 2
    board = store.seed('2026-11-07', [{'name': 'A', 'url': 'https://example.com/a'}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    assert store.lists(board['id'])[0]['id'] == 2
    assert store.db.execute('SELECT count(*) FROM rankings').fetchone()[0] == 2
    store.close()
    assert migrate(path, sources) is None


def test_sku_groups_excludes_payment_controls_and_keeps_offscreen_options():
    root = ET.fromstring('''<App><XCUIElementTypeCollectionView>
      <XCUIElementTypeOther traits="Header"><XCUIElementTypeStaticText name="口味"/></XCUIElementTypeOther>
      <XCUIElementTypeCell><XCUIElementTypeButton name="鸡肉" visible="false"/></XCUIElementTypeCell>
      <XCUIElementTypeOther traits="Header"><XCUIElementTypeStaticText name="型号"/></XCUIElementTypeOther>
      <XCUIElementTypeCell><XCUIElementTypeButton name="10支" visible="true"/></XCUIElementTypeCell>
      </XCUIElementTypeCollectionView><XCUIElementTypeButton name="立即支付"/></App>''')
    groups = sku_groups(root)
    assert {key: list(value) for key, value in groups.items()} == {'口味': ['鸡肉'], '型号': ['10支']}


def test_ios_review_cells_keep_content_specs_and_deduplicate_identity():
    root = ET.fromstring('''<App><XCUIElementTypeCollectionView>
      <XCUIElementTypeCell><XCUIElementTypeOther><XCUIElementTypeStaticText name="买家"/>
      <XCUIElementTypeOther name="口味：鸡肉"/><XCUIElementTypeButton name="菜单"/></XCUIElementTypeOther></XCUIElementTypeCell>
      <XCUIElementTypeCell><XCUIElementTypeOther name="包装破损，气味不好"/></XCUIElementTypeCell>
      <XCUIElementTypeCell><XCUIElementTypeButton name="分享"/></XCUIElementTypeCell>
      </XCUIElementTypeCollectionView></App>''')
    rows = review_rows(root)
    assert len(rows) == 1
    assert rows[0]['name'] == '买家'
    assert rows[0]['specs'] == '口味：鸡肉'
    assert rows[0]['comment'] == '包装破损，气味不好'
    assert review_rows(root)[0]['ui_fingerprint'] == rows[0]['ui_fingerprint']


def test_mobile_dispatches_ios_without_android(monkeypatch):
    from automation.pdd.spider import __main__, commands
    def execute(description, collect, *, args, role):
        assert args.ios and args.mobile and not getattr(args, 'android', False)
        return 17
    monkeypatch.setattr(commands, 'execute', execute)
    assert __main__.main([]) == 17


@pytest.mark.parametrize('misses', [0, 1, 2])
def test_ios_comment_time_reads_menu_date_and_returns_to_same_review(misses):
    from unittest.mock import Mock
    from automation.pdd.spider.ios_product import IosProduct
    collector = IosProduct(Mock(), Mock())
    root = ET.fromstring('''<App><XCUIElementTypeCollectionView>
      <XCUIElementTypeCell><XCUIElementTypeStaticText name="买家"/>
      <XCUIElementTypeButton name="菜单" visible="true"/></XCUIElementTypeCell>
      <XCUIElementTypeCell><XCUIElementTypeOther name="包装破损，气味不好"/></XCUIElementTypeCell>
      </XCUIElementTypeCollectionView></App>''')
    row = review_rows(root, controls=True)[0]
    row.pop('_menu')
    collector.tap = Mock()
    collector.root = root
    menu = Mock()
    collector.client.return_value.find_elements.return_value = [menu]
    collector.snapshot = Mock()
    matches = [{'type': 'XCUIElementTypeStaticText', 'label': '发表时间: 2026.11.04'},
               {'type': 'XCUIElementTypeButton', 'label': '取消', 'ELEMENT': 'cancel-id'}]
    def post(path, *args, **kwargs):
        return SimpleNamespace(value=matches if menu.tap.call_count == misses + 1 else [])
    collector.client._session_http.post.side_effect = post
    collector.comment_time(row)
    assert row['time'] == '2026-11-04'
    assert '_menu' not in row
    assert menu.tap.call_count == misses + 1
    collector.tap.assert_not_called()
    assert any(call.args[0] == '/element/cancel-id/click' for call in collector.client._session_http.post.call_args_list)
    assert row['ui_fingerprint'] == review_rows(root)[0]['ui_fingerprint']


def test_virtualized_sku_options_inherit_known_group():
    root = ET.fromstring('''<App><XCUIElementTypeCollectionView>
      <XCUIElementTypeCell><XCUIElementTypeButton name="50支"/></XCUIElementTypeCell>
      <XCUIElementTypeCell><XCUIElementTypeButton name="10支"/></XCUIElementTypeCell>
      </XCUIElementTypeCollectionView></App>''')
    assert {k: list(v) for k, v in sku_groups(root, {'型号': {'50支': None}}).items()} == {'型号': ['50支', '10支']}


def test_sku_summary_ignores_underlying_product_collection():
    root = ET.fromstring('''<App>
      <XCUIElementTypeCollectionView y="0" height="800"/>
      <XCUIElementTypeOther name="鸡肉 100支" y="350" height="40" visible="true"/>
      <XCUIElementTypeCollectionView y="440" height="334">
      <XCUIElementTypeOther traits="Header"><XCUIElementTypeStaticText name="口味"/></XCUIElementTypeOther>
      <XCUIElementTypeCell><XCUIElementTypeButton name="鸡肉"/></XCUIElementTypeCell>
      </XCUIElementTypeCollectionView></App>''')
    assert sku_summary(root, {'口味': ['鸡肉']}) == '鸡肉 100支'


def test_completed_ranking_still_runs_missing_ios_product_details():
    from automation.pdd.spider.rank.current import completed
    from unittest.mock import Mock
    store = Mock()
    entry = {'id': 1, 'state': 'completed'}
    store.items.return_value = [{'product_state': 'pending'}]
    assert completed(store, entry, None)
    assert not completed(store, entry, object())
    store.items.return_value = [{'product_state': 'completed', 'screenshot_state': 'completed', 'product_json': '{"info":{"image_method":"ios_enlarged_screenshot_crop"}}'}]
    assert completed(store, entry, object())


def test_ios_full_flow_collects_details_without_share_link(tmp_path):
    from unittest.mock import Mock, patch
    from PIL import Image
    from automation.pdd.spider import full_flow
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed('2026-11-05', [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    rows = [{'排行': 1, '标题': '商品标题'}]
    store.save_rows(1, tmp_path, ['排行', '标题'], rows, 'ios')
    folder = tmp_path / '榜01'
    folder.mkdir()
    Image.new('RGB', (100, 100)).save(folder / '产品截图.jpg')
    client = Mock()
    client.screenshot.return_value = Image.new('RGB', (100, 100))
    collector = Mock()
    def collect(item, *args, **kwargs):
        store.product_state(item['id'], 'completed')
    collector.collect.side_effect = collect
    with patch.object(full_flow.longshot, 'reusable', return_value=True), \
            patch.object(full_flow, 'product_element'), \
            patch.object(full_flow.ios, 'require_pdd_foreground'), \
            patch.object(full_flow.ios, '_current_page_source', return_value='<App/>'), \
            patch.object(full_flow, 'product_page_checks', return_value=([], {'title_matched': True})), \
            patch.object(full_flow, 'return_to_board', return_value=(None, [])), \
            patch.object(full_flow, 'copy_share') as share, \
            patch.object(full_flow.time, 'sleep'):
        full_flow.IosFullFlow(store, client, product_collector=collector).run_list(1, rows, [])
    collector.collect.assert_called_once()
    share.assert_not_called()
    assert store.items(1)[0]['product_state'] == 'completed'
    assert store.items(1)[0]['state'] == 'completed'
    assert store.items(1)[0]['screenshot_state'] == 'completed'
    assert not (folder / '分享链接.json').exists()
    store.close()


@pytest.mark.parametrize('image_tag', ['XCUIElementTypeImage', 'XCUIElementTypeOther'])
def test_main_image_paging_reuses_geometry_without_full_source_per_image(tmp_path, image_tag):
    from unittest.mock import Mock, patch
    from PIL import Image
    from automation.pdd.spider.ios_product import IosProduct
    product = ET.fromstring('''<XCUIElementTypeApplication width="100" height="300">
      <XCUIElementTypeImage x="0" y="0" width="100" height="160" visible="true"/>
      </XCUIElementTypeApplication>''')
    viewer = ET.fromstring('''<XCUIElementTypeApplication width="100" height="300">
      <XCUIElementTypeStaticText name="1/2" visible="true"/>
      <XCUIElementTypeImage x="0" y="100" width="100" height="100" visible="true"/>
      <XCUIElementTypeButton x="0" y="10" width="10" height="10" visible="true"/>
      </XCUIElementTypeApplication>''')
    client = Mock()
    client.screenshot.side_effect = lambda: Image.new('RGB', (100, 300))
    for root in (product, viewer):
        image = next(root.iter('XCUIElementTypeImage'))
        image.tag = image_tag
        if image_tag == 'XCUIElementTypeOther':
            image.set('name', '浏览动态图片')
    collector = IosProduct(Mock(), client)
    collector.root, collector.width, collector.height = product, 100, 300
    collector.directory, collector.images, collector.item = tmp_path, [], {'id': 1}
    pages = iter([viewer, product])
    def snapshot():
        collector.root = next(pages)
    collector.snapshot = Mock(side_effect=snapshot)
    with patch('automation.pdd.spider.ios_product.time.sleep'), patch('automation.pdd.spider.ios._swipe', return_value=True):
        collector.main_images()
    assert collector.snapshot.call_count == 2
    client.assert_called_once_with(name='2/2', className='StaticText')
    assert len(collector.images) == 2
    assert all((tmp_path / r['local_path']).is_file() for r in collector.images)


def test_detail_gallery_enlarges_and_swipes_right_through_all_pages(tmp_path):
    from unittest.mock import Mock, patch
    from PIL import Image
    from automation.pdd.spider.ios_product import IosProduct
    page = ET.fromstring('<XCUIElementTypeApplication width="414" height="896"><XCUIElementTypeStaticText name="详情" visible="true"/><XCUIElementTypeImage x="0" y="200" width="414" height="300" visible="true"/></XCUIElementTypeApplication>')
    def gallery(index):
        return ET.fromstring(f'<XCUIElementTypeApplication width="414" height="896"><XCUIElementTypeStaticText name="{index}/3" visible="true"/><XCUIElementTypeImage x="0" y="200" width="414" height="414" visible="true"/><XCUIElementTypeButton x="4" y="48" width="44" height="44" visible="true"/></XCUIElementTypeApplication>')
    collector = IosProduct(Mock(), Mock())
    collector.root, collector.width, collector.height = page, 414, 896
    collector.directory, collector.images, collector.item = tmp_path, [], {'id': 1}
    collector.client.screenshot.side_effect = lambda: Image.new('RGB', (828, 1792))
    snapshots = iter([page, gallery(1), gallery(3), gallery(2), page])
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', next(snapshots)))
    with patch('automation.pdd.spider.ios_product.time.sleep'), patch('automation.pdd.spider.ios._swipe', return_value=True) as swipe:
        collector.details()
    assert [r['position'] for r in collector.images] == [1, 3, 2]
    assert swipe.call_count == 2
    assert all(call.args[0] is collector.client and call.args[1] < call.args[3] for call in swipe.call_args_list)
    assert Image.open(tmp_path / '详情/0003.jpg').size == (828, 828)


def test_sku_image_captures_enlarged_view_and_returns_to_panel(tmp_path):
    from unittest.mock import Mock, patch
    from PIL import Image
    from automation.pdd.spider.ios_product import IosProduct
    panel = ET.fromstring('<XCUIElementTypeApplication width="414" height="896"><XCUIElementTypeButton name="增加数量" visible="true"/><XCUIElementTypeImage x="12" y="200" width="100" height="100" visible="true"/></XCUIElementTypeApplication>')
    viewer = ET.fromstring('<XCUIElementTypeApplication width="414" height="896"><XCUIElementTypeStaticText name="21/24" visible="true"/><XCUIElementTypeImage x="0" y="241" width="414" height="414" visible="true"/></XCUIElementTypeApplication>')
    collector = IosProduct(Mock(), Mock())
    collector.root, collector.width, collector.height = panel, 414, 896
    collector.client.screenshot.return_value = Image.new('RGB', (828, 1792))
    snapshots = iter([viewer, panel])
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', next(snapshots)))
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        collector.sku_image(next(panel.iter('XCUIElementTypeImage')), tmp_path / 'SKU/0001.jpg')
    assert collector.root is panel
    assert Image.open(tmp_path / 'SKU/0001.jpg').size == (828, 828)


def test_negative_tag_large_count_is_unknown_instead_of_zero():
    from PIL import Image
    from automation.pdd.spider.ios_product import negative_filters
    root = ET.fromstring('''<App><XCUIElementTypeStaticText name="质量差(1.2万+)"
      visible="true" x="0" y="0" width="120" height="30"/></App>''')
    tags = negative_filters(root, Image.new('RGB', (200, 60), (240, 240, 240)))
    assert len(tags) == 1
    assert tags[0]['label'] == '质量差(1.2万+)'
    assert tags[0]['count'] is None


@pytest.mark.parametrize('rectangle', [(130, 10, 150, 870), (0, 400, 414, 50)])
def test_viewer_image_accepts_portraits_and_banners(rectangle):
    from unittest.mock import Mock
    from automation.pdd.spider.ios_product import IosProduct
    x, y, width, height = rectangle
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring(f'<App><XCUIElementTypeImage visible="true" x="{x}" y="{y}" width="{width}" height="{height}"/><XCUIElementTypeImage visible="true" x="370" y="48" width="18" height="20"/></App>')
    assert collector.viewer_image() is list(collector.root)[0]


def test_sku_gallery_parses_centered_specs_and_price_without_neighbor_skus():
    from automation.pdd.spider.ios_product import gallery_sku
    root = ET.fromstring('''<App>
      <XCUIElementTypeOther visible="true" x="-100" width="100" name="鸡肉｜10支"/>
      <XCUIElementTypeOther visible="true" x="130" width="160" name="三文鱼｜100支 (超值装)"/>
      <XCUIElementTypeOther visible="true" x="355" width="160" name="鸡肉｜100支 (超值装)"/>
      <XCUIElementTypeStaticText visible="true" name="¥ 26.22"/>
      </App>''')
    catalog = {'口味': ['鸡肉', '三文鱼'], '型号': ['10支', '100支（超值装）']}
    assert gallery_sku(root, catalog, 414) == (('三文鱼', '100支（超值装）'), '26.22')


def test_sku_gallery_rejects_missing_or_range_price():
    from automation.pdd.spider.ios_product import gallery_sku
    root = ET.fromstring('''<App><XCUIElementTypeOther visible="true" x="130" width="160" name="鸡肉｜10支"/>
      <XCUIElementTypeStaticText visible="true" name="¥ 5.86-26.22"/></App>''')
    with pytest.raises(RuntimeError, match='确定价格'):
        gallery_sku(root, {'口味': ['鸡肉'], '型号': ['10支']}, 414)


def test_sku_gallery_accepts_explicit_coupon_price():
    from automation.pdd.spider.ios_product import gallery_sku
    root = ET.fromstring('<App><XCUIElementTypeOther visible="true" x="138" width="158" name="鸡肉｜60支"/><XCUIElementTypeStaticText visible="true" name="券后 ¥&#x200a;98.16"/></App>')
    assert gallery_sku(root, {'口味': ['鸡肉'], '套餐': ['60支']}, 414) == (('鸡肉', '60支'), '98.16')
    root[-1].set('name', '券后 ¥98.16-120')
    with pytest.raises(RuntimeError, match='确定价格'):
        gallery_sku(root, {'口味': ['鸡肉'], '套餐': ['60支']}, 414)


def test_sku_gallery_keeps_original_flow_if_not_all_combinations_are_exposed():
    from automation.pdd.spider.ios_product import IosProduct
    from unittest.mock import Mock
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring('<App><XCUIElementTypeImage visible="true" x="12" y="200" width="132" height="133"/></App>')
    collector.tap, collector.snapshot, collector.close_viewer = Mock(), Mock(), Mock()
    collector.viewer_counter = Mock(return_value=(1, 2))
    assert not collector.sku_gallery({'口味': ['鸡肉', '鱼肉'], '型号': ['10支', '20支']})
    collector.close_viewer.assert_called_once_with(sku=True)


@pytest.mark.parametrize('start', [1, 2])
def test_sku_gallery_collects_all_combinations_with_one_open(tmp_path, start):
    from automation.pdd.spider.ios_product import IosProduct
    from unittest.mock import Mock, patch
    from PIL import Image
    panel = ET.fromstring('<XCUIElementTypeApplication width="414" height="896"><XCUIElementTypeButton name="增加数量" visible="true"/><XCUIElementTypeImage visible="true" x="12" y="200" width="132" height="133"/></XCUIElementTypeApplication>')
    def gallery(index):
        name = '鸡肉' if index == 1 else '鱼肉'
        return ET.fromstring(f'<XCUIElementTypeApplication width="414" height="896"><XCUIElementTypeImage visible="true" x="0" y="241" width="414" height="414"/><XCUIElementTypeOther visible="true" x="130" width="160" name="{name}"/><XCUIElementTypeStaticText visible="true" name="¥ {index}.00"/><XCUIElementTypeStaticText visible="true" name="{index}/2"/></XCUIElementTypeApplication>')
    collector = IosProduct(Mock(), Mock())
    collector.root, collector.width, collector.height = panel, 414, 896
    collector.directory, collector.images, collector.sku_rows, collector.item = tmp_path, [], [], {'id': 1}
    collector.client.screenshot.side_effect = lambda: Image.new('RGB', (828, 1792))
    snapshots = iter([gallery(start), gallery(3 - start), panel])
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', next(snapshots)))
    with patch('automation.pdd.spider.ios_product.time.sleep'), patch('automation.pdd.spider.ios._swipe', return_value=True):
        assert collector.sku_gallery({'口味': ['鸡肉', '鱼肉']})
    assert [(r['SPU1'], r['price']) for r in collector.sku_rows] == [('鸡肉', '1.00'), ('鱼肉', '2.00')]
    assert {r['position'] for r in collector.images} == {1, 2}
    assert collector.snapshot.call_count == 3
    assert collector.root is panel


def test_sku_gallery_falls_back_after_partial_progress(tmp_path):
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    p = IosProduct(Mock(), Mock())
    p.root = ET.fromstring('<App><XCUIElementTypeImage visible="true" x="10" y="200" width="100" height="100"/></App>')
    p.width, p.height, p.directory = 414, 896, tmp_path
    p.sku_rows, p.images = [], []
    p.tap, p.snapshot, p.image, p.record_image, p.close_viewer = [Mock() for _ in range(5)]
    p.viewer_counter = Mock(return_value=(1, 2))
    p.viewer_image = Mock()
    p.wait_viewer_page = Mock(return_value=2)
    with patch('automation.pdd.spider.ios_product.gallery_sku', side_effect=[(('鸡肉',), '10'), RuntimeError('规格被截断')]), patch('automation.pdd.spider.ios._swipe', return_value=True), patch('automation.pdd.spider.ios_product.time.sleep'):
        assert p.sku_gallery({'口味': ['鸡肉', '鱼肉']}) is False
    assert [(r['SPU1'], r['price']) for r in p.sku_rows] == [('鸡肉', '10')]
    p.image.assert_called_once()
    p.close_viewer.assert_called_once_with(sku=True)


def test_sku_entry_accepts_actual_ten_thousand_group_purchase_button():
    from automation.pdd.spider.ios_product import IosProduct
    from unittest.mock import Mock
    collector = IosProduct(Mock(), Mock())
    collector.root = ET.fromstring('<App><XCUIElementTypeButton name="返回" visible="true"/><XCUIElementTypeButton name="立即参与万人团  ¥ 27.61" visible="true"/></App>')
    collector.tap = Mock(side_effect=RuntimeError('已点击规格入口'))
    with pytest.raises(RuntimeError, match='已点击规格入口'):
        collector.skus()
    assert collector.tap.call_args.args[0].get('name') == '立即参与万人团  ¥ 27.61'


def test_sku_panel_waits_when_first_snapshot_precedes_controls(tmp_path):
    from automation.pdd.spider.ios_product import IosProduct
    from unittest.mock import Mock, patch
    collector = IosProduct(Mock(), Mock())
    collector.root = ET.fromstring('<App><XCUIElementTypeButton name="立即参与万人团  ¥ 27.61" visible="true"/></App>')
    empty = ET.fromstring('<App/>')
    loaded = ET.fromstring('<App><XCUIElementTypeButton name="增加数量" visible="true"/></App>')
    snapshots = iter([empty, loaded])
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', next(snapshots)))
    collector.tap, collector.close_panel = Mock(), Mock()
    collector.directory, collector.xml = tmp_path, '<App/>'
    with patch('automation.pdd.spider.ios_product.sku_groups', side_effect=RuntimeError('已进入规格解析')):
        with pytest.raises(RuntimeError, match='已进入规格解析'):
            collector.skus()
    collector.client.assert_called_once_with(name='增加数量', className='Button', visible=True)
    collector.client.return_value.get.assert_called_once_with(timeout=15)
    assert collector.snapshot.call_count == 2


def test_single_sku_gallery_without_page_counter_is_verified_by_specs_and_price(tmp_path):
    from automation.pdd.spider.ios_product import IosProduct
    from unittest.mock import Mock, patch
    from PIL import Image
    panel = ET.fromstring('<App><XCUIElementTypeButton name="增加数量" visible="true"/><XCUIElementTypeImage visible="true" x="12" y="200" width="132" height="133"/></App>')
    viewer = ET.fromstring('<XCUIElementTypeApplication width="414" height="896"><XCUIElementTypeImage visible="true" x="0" y="241" width="414" height="414"/><XCUIElementTypeOther visible="true" x="138" width="158" name="整支鸡胸肉500g"/><XCUIElementTypeStaticText visible="true" name="¥ 27.61"/></XCUIElementTypeApplication>')
    collector = IosProduct(Mock(), Mock())
    collector.root, collector.width, collector.height = panel, 414, 896
    collector.directory, collector.images, collector.sku_rows, collector.item = tmp_path, [], [], {'id': 1}
    collector.client.screenshot.return_value = Image.new('RGB', (828, 1792))
    snapshots = iter([viewer, panel])
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', next(snapshots)))
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        assert collector.sku_gallery({'容量': ['整支鸡胸肉500g']})
    assert collector.sku_rows[0]['price'] == '27.61'
    assert Image.open(tmp_path / 'SKU/0001.jpg').size == (828, 828)


@pytest.mark.parametrize('misses', [0, 1, 2, 3])
def test_sku_selection_waits_for_summary_and_uses_target_collection(misses):
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    def panel(selected):
        return ET.fromstring(f'''<App><XCUIElementTypeOther name="确认款式 ¥8.53 {selected}" visible="true" y="100" height="30"/>
          <XCUIElementTypeCollectionView y="200" height="200">
           <XCUIElementTypeOther traits="Header"><XCUIElementTypeStaticText name="款式"/></XCUIElementTypeOther>
           <XCUIElementTypeCell><XCUIElementTypeButton name="5罐" visible="true" x="10" y="220" width="100" height="30"/></XCUIElementTypeCell>
          </XCUIElementTypeCollectionView>
          <XCUIElementTypeCollectionView y="500" height="200">
           <XCUIElementTypeOther traits="Header"><XCUIElementTypeStaticText name="口味"/></XCUIElementTypeOther>
           <XCUIElementTypeCell><XCUIElementTypeButton name="鸡肉" visible="true" y="520" height="30"/></XCUIElementTypeCell>
          </XCUIElementTypeCollectionView></App>''')
    collector = IosProduct(Mock(), Mock())
    collector.root, collector.height = panel('2罐'), 896
    collector.sku_catalog = {'款式': {'5罐': None}, '口味': {'鸡肉': None}}
    collector.scroll_skus = Mock(side_effect=AssertionError('不应滚动其他规格分组'))
    taps = collector.client.return_value.get.return_value.tap
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', panel('5罐' if taps.call_count > misses else '2罐')))
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        if misses == 3:
            assert collector.select_sku('5罐') is False
        else:
            assert collector.select_sku('5罐')
    assert taps.call_count == min(misses + 1, 3)
    collector.client.assert_called_with(name='5罐', className='Button', visible=True)
    collector.scroll_skus.assert_not_called()


@pytest.mark.parametrize('valid_option,existing,cleanup_failure', [(True, False, False), (True, True, False), (False, False, False), (False, False, True)])
def test_skus_excludes_auto_replaced_combinations_from_csv(tmp_path, monkeypatch, valid_option, existing, cleanup_failure):
    import csv
    from unittest.mock import Mock, patch
    from automation.pdd.spider import ios_product as module
    collector = module.IosProduct(Mock(), Mock())
    collector.save_diagnostics = False
    collector.root = ET.fromstring('<App><XCUIElementTypeButton name="立即购买" visible="true"/><XCUIElementTypeButton name="增加数量" visible="true"/><XCUIElementTypeImage visible="true" x="12" y="200" width="132" height="133"/><XCUIElementTypeOther name="¥9.90" visible="true"/></App>')
    collector.width, collector.height = 414, 896
    collector.directory, collector.images, collector.sku_rows, collector.item = tmp_path, [], [], {'id': 1}
    if existing:
        collector.sku_rows.append({'SPU1': '混合', 'SPU2': '40罐', 'price': '39.7',
                                   '图片路径': 'SKU/0006.jpg', 'capture_method': 'ios_enlarged_screenshot_crop'})
    collector.tap, collector.snapshot, collector.scroll_skus, collector.close_panel = Mock(), Mock(), Mock(), Mock()
    if cleanup_failure:
        collector.close_panel.side_effect = RuntimeError('面板关闭错误')
    collector.sku_gallery = Mock(return_value=False)
    collector.sku_image = Mock()
    flavors = ['混合', '鸡肉'] if valid_option else ['混合']
    groups = {group: {name: ET.Element('XCUIElementTypeButton', name=name, y='200') for name in names}
              for group, names in [('口味', flavors), ('款式', ['5罐'])]}
    monkeypatch.setattr(module, 'sku_groups', lambda *_: groups)
    selection, flavor = ['混合 2罐'], ['混合']
    def select(name):
        if name in flavors:
            flavor[0] = name
            selection[0] = name + ' 2罐'
        elif flavor[0] == '混合':
            selection[0] = '请选择:口味'
        else:
            selection[0] = flavor[0] + ' 5罐'
        return True
    collector.select_sku = Mock(side_effect=select)
    monkeypatch.setattr(module, 'sku_summary', lambda *_: selection[0])
    with patch.object(module.time, 'sleep'):
        if valid_option:
            collector.skus()
        else:
            with pytest.raises(RuntimeError, match='没有成功读取任何可选 SKU'):
                collector.skus()
    collector.close_panel.assert_called_once()
    if existing:
        collector.sku_gallery.assert_not_called()
    if valid_option:
        with (tmp_path / 'SKU.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == (2 if existing else 1)
        captured = rows[-1]
        assert captured['SPU1'] == '鸡肉' and captured['SPU2'] == '5罐'
        assert captured['price'] == '9.90'
        assert Path(captured['图片路径']).name == ('0007.jpg' if existing else '0001.jpg')
        assert sum(r.get('available') is False for r in collector.sku_rows) == 1
        collector.sku_image.assert_called_once()
    else:
        assert not (tmp_path / 'SKU.csv').exists()
        collector.sku_image.assert_not_called()


@pytest.mark.parametrize('promotion', [False, True])
def test_details_scrolls_past_video_to_real_image(tmp_path, promotion):
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    def page(image_y, image_visible):
        root = ET.fromstring(f'''<App><XCUIElementTypeCell>
          <XCUIElementTypeImage visible="true" x="0" y="400" width="414" height="230"/>
          <XCUIElementTypeButton name="player play" visible="true"/>
         </XCUIElementTypeCell><XCUIElementTypeCell>
          <XCUIElementTypeImage visible="{image_visible}" x="0" y="{image_y}" width="414" height="414"/>
         </XCUIElementTypeCell></App>''')
        if promotion:
            root.insert(0, ET.fromstring('<XCUIElementTypeCell><XCUIElementTypeImage visible="true" x="12" y="200" width="390" height="214"/><XCUIElementTypeStaticText name="免费领券" visible="true"/></XCUIElementTypeCell>'))
        return root
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height, collector.directory = 414, 896, tmp_path
    collector.seek, collector.tap = Mock(), Mock()
    collector.root = page(790, 'false')
    collector.snapshot = Mock()
    collector.swipe = Mock(side_effect=lambda **kwargs: setattr(collector, 'root', page(300, 'true')))
    collector.viewer_counter = Mock(return_value=(1, 1))
    collector.viewer_image, collector.image, collector.record_image, collector.close_viewer = Mock(), Mock(), Mock(), Mock()
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        collector.details()
    collector.swipe.assert_called_once_with(top=.3, bottom=.7)
    collector.client.click.assert_called_once_with(207, 360)
    collector.record_image.assert_called_once_with('detail', tmp_path / '详情/0001.jpg', position=1)
    collector.close_viewer.assert_called_once()


@pytest.mark.parametrize('positions,expected_error', [([3, 4], False), ([3, 3, 4], False), ([3, 3, 3], True), ([2], True)])
def test_viewer_page_retries_only_unadvanced_page(positions, expected_error):
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    collector = IosProduct(Mock(), Mock())
    collector.snapshot = Mock()
    collector.viewer_counter = Mock(side_effect=[(position, 4) for position in positions])
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        if expected_error:
            with pytest.raises(RuntimeError, match='大图页码未确认'):
                collector.wait_viewer_page(3, 4, 4)
        else:
            assert collector.wait_viewer_page(3, 4, 4) == 4
    assert collector.snapshot.call_count == len(positions)


@pytest.mark.parametrize('image_top', [241, 50])
@pytest.mark.parametrize('counter', [True, False])
def test_sku_viewer_uses_safe_exit_if_picture_tap_does_not_close(image_top, counter):
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    viewer = ET.fromstring(f'<App><XCUIElementTypeImage visible="true" x="0" y="{image_top}" width="414" height="414"/><XCUIElementTypeStaticText name="1/1" visible="true"/></App>')
    if not counter:
        viewer.remove(viewer[-1])
    panel = ET.fromstring('<App><XCUIElementTypeButton name="增加数量" visible="true"/></App>')
    collector = IosProduct(Mock(), Mock())
    collector.root, collector.width, collector.height = viewer, 414, 896
    collector.tap, collector.swipe = Mock(), Mock()
    def snapshot():
        collector.root = panel if collector.snapshot.call_count > 1 else viewer
    collector.snapshot = Mock(side_effect=snapshot)
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        collector.close_viewer(sku=True)
    if image_top == 241:
        collector.client.click.assert_called_once_with(207, 170)
        collector.swipe.assert_not_called()
    else:
        collector.client.click.assert_not_called()
        collector.swipe.assert_called_once_with(up=False, top=.35, bottom=.75)
    assert collector.root is panel


def test_viewer_cleanup_keeps_original_error():
    from unittest.mock import Mock
    from automation.pdd.spider.ios_product import IosProduct
    collector = IosProduct(Mock(), Mock())
    collector._close_viewer = Mock(side_effect=RuntimeError('退出失败'))
    with pytest.raises(ValueError, match='原始采集错误'):
        try:
            raise ValueError('原始采集错误')
        finally:
            collector.close_viewer(sku=True)
    with pytest.raises(RuntimeError, match='退出失败'):
        collector.close_viewer(sku=True)


@pytest.mark.parametrize('loads', [True, False])
def test_viewer_image_waits_for_real_visible_image(loads):
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    empty = ET.fromstring('<App><XCUIElementTypeImage visible="false" x="0" y="241" width="414" height="414"/></App>')
    ready = ET.fromstring('<App><XCUIElementTypeImage visible="true" x="0" y="241" width="414" height="414"/></App>')
    collector.root = empty
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', ready if loads else empty))
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        if loads:
            assert collector.viewer_image() is ready[0]
            assert collector.snapshot.call_count == 1
        else:
            with pytest.raises(RuntimeError, match='没有可见的图片区域'):
                collector.viewer_image()
            assert collector.snapshot.call_count == 11


def test_viewer_image_does_not_capture_if_page_changes_while_loading():
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring('<App/>')
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', ET.fromstring('<App><XCUIElementTypeImage visible="true" x="0" y="241" width="414" height="414"/></App>')))
    collector.viewer_counter = Mock(side_effect=[(1, 10), (2, 10)])
    with patch('automation.pdd.spider.ios_product.time.sleep'):
        with pytest.raises(RuntimeError, match='页码发生变化'):
            collector.viewer_image()
    assert collector.snapshot.call_count == 1


def test_viewer_image_stops_at_loading_deadline():
    from unittest.mock import Mock, patch
    from automation.pdd.spider.ios_product import IosProduct
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height, collector.root = 414, 896, ET.fromstring('<App/>')
    collector.snapshot = Mock()
    with patch('automation.pdd.spider.ios_product.time.monotonic', side_effect=[0, 61]):
        with pytest.raises(RuntimeError, match='没有可见的图片区域'):
            collector.viewer_image()
    collector.snapshot.assert_not_called()
