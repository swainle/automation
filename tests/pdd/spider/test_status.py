import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.spider.status import (load_status, product_done, reconcile_board, reconcile_product,
                           reconcile_subrank, update_stage, set_listing_date, stage_done)
from automation.pdd.spider.utils import write_csv, write_xlsx


class StatusTests(unittest.TestCase):
    def test_parent_done_does_not_hide_missing_goods_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "猫榜" / "畅销榜" / "2026-10-26"
            write_xlsx(base / "榜单数据.xlsx", ["排行", "标题", "产品链接"],
                       [[1, "一", "https://example.com/1"], [2, "二", "https://example.com/2"]])
            (base / ".rank_done").touch()
            (base / "榜01").mkdir()
            (base / "榜01" / ".rank_done").touch()
            update_stage(base / "榜01", "rank", "completed")
            result = reconcile_subrank(base)
            self.assertEqual(result["pending_rank"], ["榜02"])
            self.assertEqual(load_status(base)["rank_state"], "pending")
            self.assertTrue((base / "榜02" / "status.json").exists())

    def test_rank_marker_does_not_mean_product_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / ".rank_done").touch()
            update_stage(directory, "rank", "completed")
            state = reconcile_product(directory)
            self.assertEqual(state["rank_state"], "completed")
            self.assertEqual(state["product_state"], "pending")
            self.assertFalse(product_done(directory))

    def test_empty_negative_is_removed_with_reason_and_nonempty_gets_reason_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_csv(directory / "差评.csv", ["序号", "评论内容"], [])
            state = reconcile_product(directory)
            self.assertFalse((directory / "差评.csv").exists())
            self.assertEqual(state["stages"]["negative_comments"]["state"], "skipped")
            write_csv(directory / "差评.csv", ["序号", "评论内容"], [[1, "包装破了"]])
            reconcile_product(directory)
            with (directory / "差评.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0][-1], "原因")
            self.assertEqual(rows[1][-1], "历史差评，原始标签未记录")

    def test_missing_negative_confirmation_keeps_full_product_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "主图").mkdir()
            (directory / "主图" / "主图_01.jpg").write_bytes(b"image")
            (directory / "产品页截图.jpg").write_bytes(b"image")
            write_csv(directory / "评论.csv", ["评论"], [["好"]])
            update_stage(directory, "sku", "skipped", "无SKU")
            update_stage(directory, "details", "skipped", "无详情图")
            self.assertFalse(product_done(directory))
            update_stage(directory, "negative_comments", "skipped", "无灰色标签", count=0)
            self.assertTrue(product_done(directory))
            self.assertEqual(load_status(directory)["product_state"], "completed")
            self.assertFalse((directory / ".product_done").exists())

    def test_main_subrank_and_product_have_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "猫榜"
            base = board / "好评榜" / "2026-10-26"
            write_xlsx(base / "榜单数据.xlsx", ["排行", "标题", "产品链接"], [[1, "一", ""]])
            state = reconcile_board(board)
            self.assertEqual(state["pending_rank"], ["好评榜"])
            for directory in (board, base.parent, base, base / "榜01"):
                self.assertTrue((directory / "status.json").exists())

    def test_legacy_collected_does_not_skip_failed_product(self):
        from automation.pdd.spider import collection as main
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "猫榜" / "畅销榜" / "2026-10-26"
            base.mkdir(parents=True)
            (base / ".collected.txt").write_text("1", encoding="utf-8")
            row = {"排行": 1, "标题": "商品", "产品ID": "1", "产品链接": "https://example.com/goods"}
            with patch.object(main, "open_product_page", side_effect=RuntimeError("加载失败")) as open_page:
                self.assertFalse(main._collect_one(Mock(), base / "榜单数据.xlsx", row))
                open_page.assert_called_once()
            self.assertEqual(load_status(base / "榜01")["stages"]["product"]["state"], "failed")

    def test_status_only_completion_works_without_legacy_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "猫榜" / "畅销榜" / "2026-10-26"
            write_xlsx(base / "榜单数据.xlsx", ["排行", "标题", "产品链接"], [[1, "商品", "https://example.com/goods"]])
            directory = base / "榜01"
            update_stage(directory, "rank", "completed", data={"排行": 1, "标题": "商品", "产品链接": "https://example.com/goods"})
            state = reconcile_subrank(base)
            self.assertEqual(state["rank_state"], "completed")
            self.assertTrue(stage_done(directory, "rank"))
            self.assertEqual(load_status(directory)["rank_data"]["标题"], "商品")
            self.assertFalse(any(p.name in {".rank_done", ".rank_pending", ".collected.txt", ".product_done", "排行数据.json"}
                                 for p in base.rglob("*")))

    def test_listing_date_is_kept_in_status_and_merged_into_excel(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            set_listing_date(base / "榜01", "2024-04-27")
            write_xlsx(base / "榜单数据.xlsx", ["排行", "上架日期"], [[1, ""]])
            from automation.pdd.spider.utils import read_xlsx
            self.assertEqual(read_xlsx(base / "榜单数据.xlsx")[1], [[1, "2024-04-27"]])
            self.assertEqual(load_status(base / "榜01")["rank_data"]["上架日期"], "2024-04-27")
            self.assertFalse((base / "榜01" / "上架日期.txt").exists())


if __name__ == "__main__":
    unittest.main()
