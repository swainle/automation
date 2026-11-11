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
def test_missing_viewer_metadata_is_safe_only_for_a_sole_panel_verified_sku(tmp_path, sole_variant):
    collector = IosProduct(Mock(), Mock())
    collector.width, collector.height = 414, 896
    collector.root = ET.fromstring('<App/>')
    collector.sku_catalog = {'规格': ['48罐'] if sole_variant else ['24罐', '48罐']}
    collector.tap, collector.snapshot, collector.close_viewer = Mock(), Mock(), Mock()
    collector.viewer_counter = Mock(side_effect=RuntimeError('无页码'))
    collector.viewer_image, collector.image = Mock(return_value='verified-image'), Mock()
    path = tmp_path / 'SKU/0001.jpg'
    if sole_variant:
        collector.sku_image('thumbnail', path)
        collector.image.assert_called_once_with('verified-image', path)
        collector.close_viewer.assert_called_once_with(sku=True)
    else:
        with pytest.raises(RuntimeError, match='完整规格'):
            collector.sku_image('thumbnail', path)
        collector.image.assert_not_called()
        collector.close_viewer.assert_called_once_with(sku=True)
