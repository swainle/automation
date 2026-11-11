import sys
import tempfile
import unittest
from pathlib import Path

from automation.pdd.spider.schedule import ordered_product_tables
from automation.pdd.spider.status import load_status, reconcile_board
from automation.pdd.spider.utils import write_xlsx


class ScheduleTests(unittest.TestCase):
    def test_excel_order_and_name_survive_status_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "拼多多榜单.xlsx"
            write_xlsx(source, ["种类", "名称"], [["猫", "B榜【畅销榜、好评榜】"], ["猫", "A榜"]])
            files = {}
            for name in ("A榜", "B榜", "未列出榜"):
                files[name] = root / name / "畅销榜" / "2026-10-26" / "榜单数据.xlsx"
                write_xlsx(files[name], ["排行", "标题", "产品链接"], [[1, "商品", ""]])
            self.assertEqual(ordered_product_tables(root, source), [files["B榜"], files["A榜"], files["未列出榜"]])
            state = reconcile_board(root / "B榜")
            self.assertEqual(state["source"]["name"], "B榜【畅销榜、好评榜】")
            self.assertEqual(state["source"]["row"], 2)
            self.assertEqual(load_status(root / "未列出榜")["source"]["match_state"], "unmatched")
            write_xlsx(source, ["种类", "名称"], [["猫", "A榜"], ["猫", "B榜【畅销榜、好评榜】"]])
            self.assertEqual(ordered_product_tables(root, source)[:2], [files["A榜"], files["B榜"]])
            self.assertEqual(load_status(root / "B榜")["source"]["row"], 3)

    def test_manually_maintained_source_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "拼多多榜单.xlsx"
            write_xlsx(source, ["种类", "名称"], [["猫", "表格正式名称"]])
            file = root / "目录简称" / "畅销榜" / "2026-10-26" / "榜单数据.xlsx"
            write_xlsx(file, ["排行"], [[1]])
            (root / "目录简称" / "status.json").write_text('{"source":{"name":"表格正式名称"}}', encoding="utf-8")
            self.assertEqual(ordered_product_tables(root, source), [file])
            self.assertEqual(load_status(root / "目录简称")["source"]["match_state"], "matched")


if __name__ == "__main__":
    unittest.main()
