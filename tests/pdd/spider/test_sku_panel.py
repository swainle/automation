import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.spider import product_data as product


class SkuPanelTests(unittest.TestCase):
    def test_hidden_dom_requires_purchase_button_and_visible_options(self):
        page = Mock()
        panel = page.locator.return_value.first
        panel.count.return_value = 0
        button = Mock()
        button.is_visible.return_value = True
        page.get_by_role.return_value.filter.return_value.all.return_value = [button]
        self.assertIs(product._ensure_sku_panel(page), panel)
        page.locator.assert_called_with(product.SKU_PANEL + ':visible')
        button.click.assert_called_once()
        panel.wait_for.assert_called_once_with(state='visible', timeout=5000)
        panel.locator.return_value.first.wait_for.assert_called_once()

    def test_already_visible_panel_does_not_click_buy_again(self):
        page = Mock()
        page.locator.return_value.first.count.return_value = 1
        product._ensure_sku_panel(page)
        page.get_by_role.assert_not_called()

    def test_panel_closing_reopens_and_reselects(self):
        page = Mock(); hidden = Mock(); visible = Mock()
        first = hidden.locator.return_value.nth.return_value.locator.return_value.nth.return_value
        first.wait_for.side_effect = RuntimeError('hidden')
        element = visible.locator.return_value.nth.return_value.locator.return_value.nth.return_value
        element.get_attribute.side_effect = ['', product.SKU_SELECTED_CLASS]
        with patch.object(product, '_ensure_sku_panel', side_effect=[hidden, visible]) as ensure:
            self.assertIs(product._select_sku_combo(page, [{'index': 1}], [{'index': 2, 'text': '鸡肉味'}]), visible)
            self.assertEqual(ensure.call_count, 2)
            element.click.assert_called_once_with(timeout=3000)

    def test_unselected_option_is_not_exported_as_success(self):
        page = Mock(); panel = Mock()
        element = panel.locator.return_value.nth.return_value.locator.return_value.nth.return_value
        element.get_attribute.return_value = ''
        with patch.object(product, '_ensure_sku_panel', return_value=panel):
            with self.assertRaisesRegex(RuntimeError, '未成功选中'):
                product._select_sku_combo(page, [{'index': 0}], [{'index': 0, 'text': '口味'}])


if __name__ == '__main__':
    unittest.main()
