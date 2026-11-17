from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

from automation.pdd.spider.ios_product import IosProduct


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
