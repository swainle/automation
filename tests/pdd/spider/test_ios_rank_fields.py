"""Regression coverage for iOS card fields and Excel schema migration."""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from automation.pdd.spider import ios
from automation.pdd.spider.collectors import IosElementRanking
from automation.pdd.spider.rank import BestSelling
from automation.pdd.spider.runtime import Runtime
from automation.pdd.spider.utils import read_xlsx, write_xlsx


def text(value, x=192, y=300, w=60, h=24, kind="StaticText"):
    return {"type": kind, "text": value, "x": x, "y": y, "w": w, "h": h}


def board():
    return [text("其他猫零食畅销榜", y=0, kind="NavigationBar"),
            text("根据近7日销量、成交总额等数据每日更新", y=189),
            text("热卖指数6683", x=52, y=246),
            text("猫零食测试商品的完整标题内容", y=298),
            text("24小时内500+人拼单", y=395),
            text("￥", y=425, w=16), text("5.58", x=207, y=418, w=48, h=33),
            text("疯抢200万+件", x=259, y=425),
            text("热卖指数6680", x=52, y=468),
            text("另一个猫零食测试商品的完整标题", y=520),
            text("券后", y=647, w=31), text("￥", x=223, y=647, w=16),
            text("5.82", x=238, y=640, w=49, h=33),
            text("已抢43.1万件", x=290, y=647)]


class RankFieldTests(unittest.TestCase):
    def test_prices_orders_sales_and_board_description_are_separate(self):
        rows = ios._parse_rows(board(), BestSelling)
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["展示价"], "￥5.58")
        self.assertEqual(first["券后价"], "")
        self.assertEqual(first["近期拼单"], "24小时内500+人拼单")
        self.assertEqual(first["销售数量"], "疯抢200万+件")
        self.assertEqual(first["销量"], 2000000)
        self.assertEqual(second["券后价"], "券后 ￥5.82")
        self.assertEqual(second["展示价"], "")
        self.assertEqual(second["价格"], "5.82")
        self.assertEqual(second["销量"], 431000)
        for row in rows:
            self.assertEqual(row["主榜单名称"], "其他猫零食")
            self.assertEqual(row["子榜单名称"], "畅销榜")
            self.assertEqual(row["榜单统计说明"], "根据近7日销量、成交总额等数据每日更新")

    def test_sales_prefixes_units_and_plus_are_preserved(self):
        for value in ("疯抢200万+件", "已抢43.1万件", "总销4万+件", "已抢3643件", "总售21.3万+件"):
            with self.subTest(value=value):
                self.assertEqual(ios._card_details([text(value)])["销售数量"], value)

    def test_combined_prices_and_integer_price(self):
        for value, field in (("卷后 ￥5.82", "券后价"), ("券后 ￥5", "券后价"), ("￥5.58", "展示价")):
            with self.subTest(value=value):
                self.assertEqual(ios._card_details([text(value)])[field], value)

    def test_coupon_marker_cannot_label_a_price_on_another_line(self):
        details = ios._card_details([text("券后", y=200), text("￥5.58", y=300)])
        self.assertEqual(details["展示价"], "￥5.58")
        self.assertEqual(details["券后价"], "")

    def test_missing_fields_stay_empty_and_shipping_is_not_orders(self):
        details = ios._card_details([text("24小时内发货"), text("超长商品标题含100件的套装")])
        self.assertEqual(details, {key: "" for key in ios._IOS_RANK_FIELDS[:4]})
        self.assertEqual(ios._rank_details([], BestSelling)["榜单统计说明"], "")

    def test_old_excel_removes_link_columns_but_keeps_links_in_memory(self):
        with TemporaryDirectory() as tmp:
            runtime = Runtime()
            runtime.client = Mock()
            collector = IosElementRanking(runtime)
            rows = ios._parse_rows(board(), BestSelling)
            from datetime import date
            target = Path(tmp) / "其他猫零食" / "畅销榜" / date.today().isoformat() / "榜单数据.xlsx"
            old_header = BestSelling("").header
            rows[0]["产品链接"], rows[0]["产品ID"] = "https://example.com/goods?goods_id=1", "1"
            write_xlsx(target, old_header, [[row.get(key, "") for key in old_header] for row in rows])
            with patch("automation.pdd.spider.config.OUTPUT_ROOT", Path(tmp)), \
                    patch.object(ios, "_current_page_source", return_value="xml"), \
                    patch.object(ios, "_xcui_texts", return_value=board()):
                items = collector.list_rank_data()
            header, values = read_xlsx(target)
            first = dict(zip(header, values[0]))
            self.assertNotIn("产品链接", header)
            self.assertNotIn("产品ID", header)
            self.assertEqual(items[0].row["产品链接"], "https://example.com/goods?goods_id=1")
            self.assertEqual(first["展示价"], "￥5.58")
            self.assertEqual(first["销售数量"], "疯抢200万+件")
            self.assertEqual(first["主榜单名称"], "其他猫零食")
            self.assertEqual(items[0].header, header)
            self.assertNotIn("券后价", BestSelling("").header)


if __name__ == "__main__":
    unittest.main()
