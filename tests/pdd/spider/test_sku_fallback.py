from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

from automation.pdd.spider.ios_product import IosProduct


@pytest.mark.parametrize('counter', [True, False])
def test_gallery_without_specs_or_price_falls_back_to_verified_panel(counter):
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring('<App><XCUIElementTypeImage visible="true" x="12" y="200" width="132" height="133"/></App>')
    collector.images, collector.sku_rows = [], []
    collector.tap, collector.snapshot, collector.close_viewer = Mock(), Mock(), Mock()
    collector.viewer_counter = Mock(return_value=(1, 1)) if counter else Mock(side_effect=RuntimeError('无页码'))
    assert collector.sku_gallery({'规格': ['48罐']}) is False
    collector.close_viewer.assert_called_once_with(sku=True)


@pytest.mark.parametrize('sole_variant', [True, False])
def test_panel_verified_sku_image_does_not_require_viewer_metadata(tmp_path, sole_variant):
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring('<App/>')
    collector.sku_catalog = {'规格': ['48罐'] if sole_variant else ['24罐', '48罐']}
    collector.tap, collector.snapshot, collector.close_viewer = Mock(), Mock(), Mock()
    collector.viewer_counter = Mock(side_effect=RuntimeError('无页码'))
    collector.viewer_image, collector.image = Mock(return_value='verified-image'), Mock()
    path = tmp_path / 'SKU/0001.jpg'
    collector.sku_image('thumbnail', path)
    collector.image.assert_called_once_with('verified-image', path)
    collector.close_viewer.assert_called_once_with(sku=True)


def test_failed_thumbnail_tap_does_not_capture_the_sku_panel(tmp_path):
    collector = IosProduct(Mock(), Mock())
    collector.root = ET.fromstring('<App><XCUIElementTypeButton name="增加数量" visible="true"/></App>')
    collector.tap, collector.snapshot, collector.close_viewer, collector.image = Mock(), Mock(), Mock(), Mock()
    with pytest.raises(RuntimeError, match='未进入 SKU 大图'):
        collector.sku_image('thumbnail', tmp_path / 'SKU/0001.jpg')
    collector.image.assert_not_called()


def test_disabled_sku_is_skipped_without_tapping():
    collector = IosProduct(Mock(), Mock())
    collector.root = ET.fromstring('''<App>
      <XCUIElementTypeOther name="已选48罐" visible="true" y="100" height="30"/>
      <XCUIElementTypeCollectionView y="200" height="400">
       <XCUIElementTypeOther traits="Header"><XCUIElementTypeStaticText name="规格"/></XCUIElementTypeOther>
       <XCUIElementTypeCell><XCUIElementTypeButton name="24罐" enabled="false" visible="true" y="250" height="30"/></XCUIElementTypeCell>
      </XCUIElementTypeCollectionView></App>''')
    collector.sku_catalog, collector.height = {'规格': {'24罐': None, '48罐': None}}, 896
    assert collector.select_sku('24罐') is False
    collector.client.assert_not_called()
