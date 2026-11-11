import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from automation.pdd.spider.artifacts import read_products, read_share_link, write_json, write_share_link
from automation.pdd.spider.collectors import BrowserProduct, ProductItem
from automation.pdd.spider.runtime import Runtime
from automation.pdd.spider.utils import write_xlsx


class ArtifactTests(unittest.TestCase):
    def test_products_take_link_and_id_from_json_not_excel(self):
        with TemporaryDirectory() as directory:
            table = Path(directory) / "榜单数据.xlsx"
            write_xlsx(table, ["排行", "标题", "产品链接", "产品ID"],
                       [[1, "商品", "https://old.example/goods", "old"]])
            self.assertEqual(read_products(table)[0]["产品链接"], "")
            write_share_link(table.parent / "榜01", {
                "标题": "商品", "产品链接": "https://new.example/goods", "产品ID": "new"})
            row = read_products(table)[0]
            self.assertEqual(row["产品链接"], "https://new.example/goods")
            self.assertEqual(row["产品ID"], "new")

    def test_malformed_or_mismatched_share_file_is_rejected(self):
        with TemporaryDirectory() as directory:
            rank = Path(directory)
            for invalid in ([], {"share_link": "not-a-url"},
                            {"share_link": "https://example.com/goods", "title": "其他商品"}):
                write_json(rank / "分享链接.json", invalid)
                with self.assertRaises(ValueError):
                    read_share_link(rank, "当前商品")

    def test_failed_atomic_write_preserves_existing_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "分享链接.json"
            write_json(path, {"old": True})
            with patch("automation.pdd.spider.artifacts.os.replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    write_json(path, {"new": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_browser_shelves_time_writes_only_date_json_without_image_download(self):
        with TemporaryDirectory() as directory:
            item = ProductItem(Path(directory) / "榜单数据.xlsx", {"排行": 1, "标题": "商品"})
            collector = BrowserProduct(Runtime())
            collector.product_page = Mock()
            collector.product_page.locator.return_value.count.return_value = 1
            collector.product_page.locator.return_value.first.get_attribute.return_value = (
                "https://images.example/2024-04-27/main.jpg")
            with patch("automation.pdd.spider.product_data.download") as download, \
                    patch("automation.pdd.spider.product_data._scrape_main_images") as images:
                collector.get_shelves_time(item)
            download.assert_not_called()
            images.assert_not_called()
            data = json.loads((item.directory / "上架时间.json").read_text(encoding="utf-8"))
            self.assertEqual(data, {"shelves_time": "2024-04-27", "precision": "date"})
            self.assertFalse((item.directory / "主图").exists())
            self.assertFalse(item.table.exists())

    def test_missing_image_date_does_not_fabricate_shelves_time(self):
        with TemporaryDirectory() as directory:
            item = ProductItem(Path(directory) / "榜单数据.xlsx", {"排行": 1})
            collector = BrowserProduct(Runtime())
            collector.product_page = Mock()
            collector.product_page.locator.return_value.count.return_value = 1
            collector.product_page.locator.return_value.first.get_attribute.return_value = (
                "https://images.example/2024-99-99/main.jpg")
            with self.assertRaisesRegex(RuntimeError, "有效日期"):
                collector.get_shelves_time(item)
            self.assertFalse((item.directory / "上架时间.json").exists())

    def test_browser_initializer_skips_products_without_share_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            table = root / "榜单数据.xlsx"
            write_xlsx(table, ["排行", "标题"], [[1, "商品一"], [2, "商品二"]])
            write_share_link(root / "榜02", {"标题": "商品二", "产品链接": "https://example.com/goods"})
            collector = BrowserProduct(Runtime())
            with patch("automation.pdd.spider.schedule.ordered_product_tables", return_value=[table]):
                collector.get_data()
            self.assertEqual(len(collector.workflow.items), 1)
            self.assertEqual(collector.workflow.items[0].row["排行"], 2)


if __name__ == "__main__":
    unittest.main()
