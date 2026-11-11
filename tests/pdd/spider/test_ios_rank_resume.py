import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.spider import ios
from automation.pdd.spider.utils import write_xlsx, read_xlsx
from automation.pdd.spider.status import update_stage, stage_done, load_status


class RankResumeTests(unittest.TestCase):
    def test_completed_goods_skipped_and_old_links_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = base / "榜01"
            first.mkdir()
            update_stage(first, "rank", "completed")
            header = ["排行", "标题", "产品链接", "产品ID", "上架日期"]
            write_xlsx(base / "榜单数据.xlsx", header,
                       [[1, "商品一", "https://example.com/old", "old", "2024-04-27"],
                        [2, "商品二", "", "", ""]])
            rows = [{"排行": i, "标题": title, "产品链接": "", "产品ID": "", "上架日期": ""}
                    for i, title in [(1, "商品一"), (2, "商品二")]]
            with patch.object(ios, "_scroll_to_top"), \
                    patch.object(ios, "_locate_card_centered", return_value=None) as locate:
                ios._fill_product_links(Mock(), rows, base, header)
                locate.assert_called_once_with(unittest.mock.ANY, "商品二")
            values = read_xlsx(base / "榜单数据.xlsx")[1]
            self.assertEqual(values[0][2:], ["https://example.com/old", "old", "2024-04-27"])
            self.assertFalse((base / "榜02" / ".rank_done").exists())

    def test_all_goods_marked_needs_no_device_actions_or_workbook_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "榜01").mkdir()
            update_stage(base / "榜01", "rank", "completed")
            with patch.object(ios, "_scroll_to_top") as scroll, \
                    patch.object(ios, "_locate_card_centered") as locate:
                ios._fill_product_links(Mock(), [{"排行": 1, "标题": "商品", "产品链接": ""}], base, [])
                scroll.assert_not_called()
                locate.assert_not_called()
            self.assertFalse((base / "榜单数据.xlsx").exists())

    def test_success_writes_goods_record_and_completion_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            row = {"排行": 1, "标题": "商品一", "产品链接": "", "产品ID": "", "上架日期": ""}
            header = list(row)
            with patch.object(ios, "_scroll_to_top"), \
                    patch.object(ios, "_locate_card_centered", return_value=(200, 400)), \
                    patch.object(ios, "_tap_xy", return_value=True), \
                    patch.object(ios, "_find_center", return_value=(100, 100)), \
                    patch.object(ios, "_read_clipboard", return_value="https://example.com/goods?goods_id=123"), \
                    patch.object(ios, "_tap_label", return_value=True), \
                    patch.object(ios.time, "sleep"):
                ios._fill_product_links(Mock(), [row], base, header)
            self.assertTrue(stage_done(base / "榜01", "rank"))
            self.assertEqual(load_status(base / "榜01")["rank_data"]["产品ID"], "123")
            self.assertFalse((base / "榜01" / ".rank_done").exists())
            self.assertFalse((base / "榜01" / "排行数据.json").exists())
            values = read_xlsx(base / "榜单数据.xlsx")[1]
            self.assertEqual(values[0][header.index("产品ID")], "123")


if __name__ == "__main__":
    unittest.main()
