import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from automation.pdd.spider.collectors import create_collector
from automation.pdd.spider.runtime import Runtime
from automation.pdd.spider.utils import write_xlsx, read_xlsx


class SogouRankingTests(unittest.TestCase):
    def test_step_changes_are_scoped_to_element_ranking(self):
        collector = create_collector("sogou", "element", "ranking", Runtime())
        for name in ("list_boards", "list_products", "get_crop_image"):
            self.assertIn(name, collector.steps)
        for name in ("get_boards", "get_products", "crop", "save_image", "save_data"):
            self.assertNotIn(name, collector.steps)
        with self.assertRaises(ValueError):
            create_collector("sogou", "api", "ranking", Runtime())
        with self.assertRaises(ValueError):
            create_collector("ios", "api", "ranking", Runtime())
        product = create_collector("sogou", "element", "product", Runtime())
        self.assertIn("crop", product.steps)
        self.assertIn("save_image", product.steps)

    def test_launcher_saves_excel_raw_images_crops_and_links_without_save_steps(self):
        flow = "open_browser,list_boards,[open_ranking,list_tabs,[switch_tab,list_products,[get_share_link,screenshot,get_crop_image]]],close_browser"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.xlsx"
            write_xlsx(source, ['种类', '来源', '主榜', '子榜', '链接'],
                       [['宠物', '猫零食来源', '猫零食', '畅销榜', 'https://example.com/rank']])
            runtime = Runtime()
            runtime.page = Mock()
            collector = create_collector("sogou", "element", "ranking", runtime)
            board = Mock()
            board.header = ["排行", "标题", "产品ID", "产品链接"]
            board.crop = (390, 442)
            board.ref_selector = "#header"
            board.scrape.return_value = [
                {"排行": i, "标题": f"商品{i}", "产品ID": str(i),
                 "产品链接": f"https://example.com/goods?goods_id={i}"} for i in (1, 2)]

            def capture(page, path):
                # Excel must already exist before attempting any screenshots.
                self.assertEqual(len(list((root / "output").rglob("榜单数据.xlsx"))), 1)
                Image.new("RGB", (800, 900), "white").save(path)
                return True

            with patch("automation.pdd.spider.config.EXCEL_PATH", source), \
                    patch("automation.pdd.spider.config.OUTPUT_ROOT", root / "output"), \
                    patch.object(runtime, "open_browser"), \
                    patch.object(collector, "_leaderboard", return_value=board), \
                    patch("automation.pdd.spider.rank.get_tabs", return_value=["畅销榜"]), \
                    patch("automation.pdd.spider.rank.get_leaderboard_name", return_value="猫零食畅销榜"), \
                    patch("automation.pdd.spider.sogou.scroll_card_to_top", return_value=True), \
                    patch("automation.pdd.spider.sogou.grab_window", side_effect=capture), \
                    patch("automation.pdd.spider.sogou.find_sogou_hwnd", return_value=None):
                collector.run(flow)
            table = next((root / "output").rglob("榜单数据.xlsx"))
            self.assertEqual(len(read_xlsx(table)[1]), 2)
            for i in (1, 2):
                product = table.parent / f"榜{i:02d}"
                with Image.open(product / "排行截图.jpg") as raw:
                    self.assertEqual(raw.size, (800, 900))
                with Image.open(product / "产品截图.jpg") as crop:
                    self.assertEqual(crop.size, (390, 442))
                self.assertTrue((product / "分享链接.json").is_file())


if __name__ == "__main__":
    unittest.main()
