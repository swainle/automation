import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.spider.product_data import _clean_sku_text, _sku_price, is_sold_out, SOLD_OUT_TEXT, _sold_out_url_info
from automation.pdd.spider.status import load_status, product_done, update_stage
from automation.pdd.spider.utils import write_xlsx


class ProductTextTests(unittest.TestCase):
    def test_stock_badge_removed_and_whitespace_collapsed(self):
        self.assertEqual(_clean_sku_text('成猫【混合装口味】\n快要抢光'), '成猫【混合装口味】')
        self.assertEqual(_clean_sku_text('  \u200b成猫【混合装口味】  \n 库存紧张\n'), '成猫【混合装口味】')
        self.assertEqual(_clean_sku_text('【囤货首选】15g*120支（拆散发货）'), '【囤货首选】15g*120支（拆散发货）')
        self.assertEqual(_clean_sku_text('快要抢光口味'), '快要抢光口味')

    def test_price_contains_only_amount(self):
        self.assertEqual(_sku_price('首件¥176.9'), '176.9')
        self.assertEqual(_sku_price('首件\n￥\n119.9'), '119.9')
        self.assertEqual(_sku_price('91.9'), '91.9')
        self.assertEqual(_sku_price('暂无价格'), '')

    def test_sold_out_must_be_visible(self):
        page = Mock()
        notice = page.locator.return_value.filter.return_value
        notice.count.return_value = 1
        notice.first.is_visible.return_value = False
        self.assertFalse(is_sold_out(page))
        notice.first.is_visible.return_value = True
        self.assertTrue(is_sold_out(page))
        page.locator.return_value.filter.assert_called_with(has_text=SOLD_OUT_TEXT)

    def test_share_metadata_decoded_and_original_link_wins(self):
        url = ('https://mobile.yangkeduo.com/goods.html?goods_id=657405057389'
               '&_x_query=%E7%8C%AB%E5%92%AA%E5%86%BB%E5%B9%B2%E9%9B%B6%E9%A3%9F%E6%A6%9C%E5%8D%95'
               '&refer_share_channel=copy_link&_oak_share_time=1792994836')
        info = _sold_out_url_info(url, 'https://example.com/goods?goods_id=999')
        self.assertEqual(info['goods_id'], '657405057389')
        self.assertEqual(info['search_keyword'], '猫咪冻干零食榜单')
        self.assertEqual(info['share_channel'], 'copy_link')
        self.assertEqual(info['share_timestamp'], '1792994836')
        self.assertTrue(info['share_time'].endswith('+08:00'))

    def test_missing_metadata_and_invalid_timestamp_are_safe(self):
        info = _sold_out_url_info('https://example.com/goods?ps=opaque',
                                 'https://example.com/goods?goods_id=123&share_time=invalid')
        self.assertEqual(info['goods_id'], '123')
        self.assertEqual(info['search_keyword'], '')
        self.assertEqual(info['share_time'], '')
        self.assertEqual(_sold_out_url_info('https://example.com/goods?ps=opaque')['goods_id'], '')

    def test_ps_shortlink_uses_expanded_browser_url(self):
        info = _sold_out_url_info('https://mobile.yangkeduo.com/goods1.html?ps=dhzqr8BCyy',
                                 'https://mobile.yangkeduo.com/goods1.html?ps=dhzqr8BCyy&goods_id=682028385656'
                                 '&_x_query=%E7%8C%AB%E5%92%AA%E5%86%BB%E5%B9%B2%E9%9B%B6%E9%A3%9F%E6%A6%9C%E5%8D%95'
                                 '&refer_share_channel=copy_link&_oak_share_time=1792990543')
        self.assertEqual(info['goods_id'], '682028385656')
        self.assertEqual(info['search_keyword'], '猫咪冻干零食榜单')
        self.assertEqual(info['share_channel'], 'copy_link')
        self.assertEqual(info['share_timestamp'], '1792990543')

    def test_sold_out_product_retries_until_eleventh_detection(self):
        from automation.pdd.spider import collection as main
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / '主榜' / '畅销榜' / '2026-10-26'
            row = {'排行': 1, '标题': '产品', '产品链接': 'https://example.com/goods', '产品ID': '1'}
            write_xlsx(base / '榜单数据.xlsx', list(row), [list(row.values())])
            update_stage(base / '榜01', 'rank', 'completed', data=row)
            page = Mock()
            page.url = 'https://example.com/sold-out'
            with patch.object(main, 'open_product_page', return_value=page) as open_page, \
                    patch.object(main, 'is_sold_out', return_value=True), \
                    patch.object(main, 'scrape_product') as scrape, patch.object(main.time, 'sleep'):
                for attempt in range(1, 11):
                    self.assertFalse(main._collect_one(Mock(), base / '榜单数据.xlsx', row))
                    self.assertEqual(load_status(base / '榜01')['stages']['product']['sold_out_attempts'], attempt)
                    self.assertFalse(product_done(base / '榜01'))
                self.assertTrue(main._collect_one(Mock(), base / '榜单数据.xlsx', row))
                self.assertFalse(main._collect_one(Mock(), base / '榜单数据.xlsx', row))
                self.assertEqual(open_page.call_count, 11)
                scrape.assert_not_called()
                self.assertEqual(page.close.call_count, 11)
            status = load_status(base / '榜01')
            self.assertEqual(status['product_state'], 'skipped')
            self.assertEqual(status['reason'], SOLD_OUT_TEXT)
            self.assertTrue(product_done(base / '榜01'))


if __name__ == '__main__':
    unittest.main()
