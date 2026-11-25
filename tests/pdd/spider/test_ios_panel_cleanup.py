from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

from automation.pdd.spider.ios_product import IosProduct
from automation.pdd.spider.weekly_mobile import WeeklyProduct


PARAMETERS = '''<XCUIElementTypeApplication width="375" height="812">
    <XCUIElementTypeStaticText name="商品参数" visible="true" x="152" y="259" width="72" height="25"/>
    <XCUIElementTypeButton name="关闭" visible="true" x="341" y="251" width="32" height="40"/>
    <XCUIElementTypeButton name="分享" visible="true" x="330" y="50" width="32" height="40"/>
    </XCUIElementTypeApplication>'''
PRODUCT = '<XCUIElementTypeApplication width="375" height="812"><XCUIElementTypeStaticText name="商品" visible="true"/></XCUIElementTypeApplication>'


@pytest.mark.parametrize('kind', [IosProduct, WeeklyProduct])
@pytest.mark.parametrize('still_open', [False, True])
def test_parameters_popup_is_closed_and_verified(monkeypatch, kind, still_open):
    collector = kind(Mock(), Mock())
    collector.tap = Mock()
    read = Mock(side_effect=[PARAMETERS, PARAMETERS if still_open else PRODUCT])
    monkeypatch.setattr('automation.pdd.spider.ios._current_page_source', read)
    if still_open:
        with pytest.raises(RuntimeError, match='关闭后仍未消失'):
            collector.snapshot()
    else:
        assert collector.snapshot().find('XCUIElementTypeStaticText').get('name') == '商品'
    assert collector.source_reads == 2
    collector.tap.assert_called_once()
    assert collector.tap.call_args.args[0].get('name') == '关闭'


def test_inline_parameters_entry_is_not_a_popup(monkeypatch):
    collector = IosProduct(Mock(), Mock())
    xml = PARAMETERS.replace('x="152"', 'x="12"')
    monkeypatch.setattr('automation.pdd.spider.ios._current_page_source', Mock(return_value=xml))
    collector.tap = Mock()
    collector.snapshot()
    collector.tap.assert_not_called()


def test_parameters_without_unique_close_button_stops(monkeypatch):
    collector = IosProduct(Mock(), Mock())
    xml = PARAMETERS.replace('</XCUIElementTypeApplication>', '<XCUIElementTypeButton visible="true" x="330" y="251" width="32" height="40"/></XCUIElementTypeApplication>')
    monkeypatch.setattr('automation.pdd.spider.ios._current_page_source', Mock(return_value=xml))
    collector.tap = Mock()
    with pytest.raises(RuntimeError, match='唯一关闭按钮'):
        collector.snapshot()
    collector.tap.assert_not_called()


def test_parameters_close_icon_is_exposed_as_static_text(monkeypatch):
    collector = WeeklyProduct(Mock(), Mock())
    xml = PARAMETERS.replace('XCUIElementTypeButton name="关闭"', 'XCUIElementTypeStaticText value="\ue8f2"')
    monkeypatch.setattr('automation.pdd.spider.ios._current_page_source', Mock(side_effect=[xml, PRODUCT]))
    monkeypatch.setattr('automation.pdd.spider.ios_product.time.sleep', Mock())
    collector.snapshot()
    collector.client.click.assert_called_once_with(357, 271)
    assert collector.source_reads == 2


def test_weekly_initial_snapshot_closes_parameters(monkeypatch):
    collector = WeeklyProduct(Mock(), Mock())
    collector.tap = Mock()
    read = Mock(return_value=PRODUCT)
    monkeypatch.setattr('automation.pdd.spider.ios._current_page_source', read)
    collector.snapshot(initial_xml=PARAMETERS)
    assert collector.source_reads == 1
    collector.tap.assert_called_once()
    assert collector.root.find('XCUIElementTypeStaticText').get('name') == '商品'


VIDEO = '''<App><XCUIElementTypeButton name="pdd gallery bar back remove bg"
    visible="true" x="8" y="54" width="32" height="32"/>
    <XCUIElementTypeStaticText name="关注" visible="true"/></App>'''
DETAIL = '<App><XCUIElementTypeOther name="商品详情" visible="true"/></App>'


@pytest.mark.parametrize('returned', [True, False])
def test_detail_video_exit_requires_verified_product_details(returned):
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring(VIDEO)
    collector.tap = Mock()
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', ET.fromstring(DETAIL if returned else VIDEO)))
    if returned:
        assert collector._exit_detail_video() is True
    else:
        with pytest.raises(RuntimeError, match='未确认返回商品详情'):
            collector._exit_detail_video()
    assert collector.tap.call_args.args[0].get('name') == 'pdd gallery bar back remove bg'


def test_details_exits_video_then_collects_image(monkeypatch, tmp_path):
    monkeypatch.setattr('automation.pdd.spider.ios_product.time.sleep', Mock())
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height, collector.directory = 414, 896, tmp_path
    collector.root = ET.fromstring(DETAIL)
    collector._open_details = Mock()
    collector._detail_thumbnail = Mock(return_value=ET.fromstring('<Image x="0" y="200" width="414" height="300"/>'))
    pages = iter([VIDEO, DETAIL, '<App/>'])
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', ET.fromstring(next(pages))))
    collector.tap, collector.swipe = Mock(), Mock()
    collector.viewer_counter = Mock(return_value=(1, 1))
    collector.viewer_image, collector.image = Mock(), Mock()
    collector.record_image, collector.close_viewer = Mock(), Mock()
    collector.details()
    assert collector.client.click.call_count == 2
    collector.tap.assert_called_once()
    collector.swipe.assert_called_once_with(top=.3, bottom=.7)
    collector.record_image.assert_called_once_with('detail', tmp_path / '详情/0001.jpg', position=1)


def test_details_repeated_video_stops_without_marking_images_complete(monkeypatch):
    monkeypatch.setattr('automation.pdd.spider.ios_product.time.sleep', Mock())
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector._open_details = Mock()
    collector._detail_thumbnail = Mock(return_value=ET.fromstring('<Image x="0" y="200" width="414" height="300"/>'))
    collector.snapshot = Mock(side_effect=lambda: setattr(collector, 'root', ET.fromstring(VIDEO)))
    collector.tap = Mock(side_effect=lambda node: None)
    def snapshot():
        collector.root = ET.fromstring(DETAIL if collector.tap.call_count > snapshot.exits else VIDEO)
        snapshot.exits = collector.tap.call_count
    snapshot.exits = 0
    collector.snapshot.side_effect = snapshot
    collector.swipe, collector.viewer_counter = Mock(), Mock()
    with pytest.raises(RuntimeError, match='未找到可点击的详情图片'):
        collector.details()
    assert collector.tap.call_count == 12
    assert collector.swipe.call_count == 11
    collector.viewer_counter.assert_not_called()


@pytest.mark.parametrize('failed', [False, True])
def test_sku_panel_uses_verified_close(monkeypatch, failed):
    collector = IosProduct(Mock(), Mock())
    collector.find = Mock(return_value=object())
    collector.snapshot = Mock()
    close = Mock(side_effect=RuntimeError('SKU 面板仍未消失') if failed else None)
    monkeypatch.setattr('automation.pdd.spider.full_flow.dismiss_sku_panel', close)
    if failed:
        with pytest.raises(RuntimeError, match='仍未消失'):
            collector.close_panel()
        collector.snapshot.assert_not_called()
    else:
        collector.close_panel()
        collector.snapshot.assert_called_once()
    close.assert_called_once_with(collector.client)


def test_details_closes_sku_panel_when_no_image_viewer(monkeypatch):
    monkeypatch.setattr('automation.pdd.spider.ios_product.time.sleep', Mock())
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring('<App/>')
    collector._open_details = Mock()
    collector._detail_thumbnail = Mock(return_value=ET.fromstring(
        '<Image x="0" y="200" width="414" height="414"/>'))
    collector.snapshot = Mock()
    collector.viewer_counter = Mock(side_effect=RuntimeError('没有页码'))
    collector.find = Mock(return_value=object())
    collector.close_panel = Mock()
    collector.details()
    collector.close_panel.assert_called_once()


@pytest.mark.parametrize('text', ['立即购买', '发起拼单', '立即参与万人团', '11.11秒杀价 ¥28.98 剩余28:27:03', '加倍补 券后¥21.38 剩余53:36:10'])
def test_sku_entry_opens_purchase_or_flash_sale_sheet(text):
    collector = IosProduct(Mock(), Mock())
    collector.root = ET.fromstring('<App/>')
    button = ET.SubElement(collector.root, 'XCUIElementTypeButton', label=text, visible='true')
    collector.tap = Mock()
    collector.snapshot = Mock(side_effect=RuntimeError('已点击规格入口'))
    with pytest.raises(RuntimeError, match='已点击规格入口'):
        collector.skus()
    collector.tap.assert_called_once_with(button)
