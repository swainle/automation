"""离线验证手动榜单采集，不连接手机。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.spider import ios


class NavigationTests(unittest.TestCase):
    def setUp(self):
        scanner = patch("automation.pdd.spider.status.scan_output", return_value=[])
        scanner.start()
        self.addCleanup(scanner.stop)

    def test_completed_subranks_are_preserved(self):
        import tempfile
        from datetime import date
        from automation.pdd.spider import config

        xml = '<root>' + ''.join(
            f'<XCUIElementTypeStaticText label="{name}" />'
            for name in ("畅销榜", "好评榜", "降价榜")) + '</root>'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, "OUTPUT_ROOT", Path(tmp)), \
                patch.object(ios, "_current_page_source", return_value=xml), \
                patch.object(ios, "_dump_diag"), \
                patch.object(ios, "_switch_rank_tab", return_value=[]) as switch, \
                patch.object(ios, "_parse_rows", return_value=[{"排行": 1, "标题": "商品", "产品链接": "https://example.com/goods"}]), \
                patch.object(ios, "_fill_product_links") as fill:
            base = Path(tmp) / "猫榜"
            completed = base / "畅销榜" / date.today().isoformat()
            completed.mkdir(parents=True)
            from automation.pdd.spider.utils import write_xlsx
            write_xlsx(completed / "榜单数据.xlsx", ["排行", "标题", "产品链接"], [[1, "商品", "https://example.com/goods"]])
            (completed / "榜01").mkdir()
            from automation.pdd.spider.status import update_stage
            update_stage(completed / "榜01", "rank", "completed")
            workbook = (completed / "榜单数据.xlsx").read_bytes()
            def save_rows(client, rows, base, header):
                write_xlsx(base / "榜单数据.xlsx", ["排行", "标题", "产品链接"], [[1, "商品", "https://example.com/goods"]])
                (base / "榜01").mkdir(exist_ok=True)
                update_stage(base / "榜01", "rank", "completed")
            fill.side_effect = save_rows
            ios._collect_current_rank_ios(Mock(), "猫榜")
            self.assertEqual([c.args[1] for c in switch.call_args_list], ["好评榜", "降价榜"])
            self.assertEqual(fill.call_count, 2)
            self.assertEqual((completed / "榜单数据.xlsx").read_bytes(), workbook)
            self.assertFalse((completed / ".rank_pending").exists())
            switch.reset_mock()
            fill.reset_mock()
            ios._collect_current_rank_ios(Mock(), "猫榜")
            switch.assert_not_called()
            fill.assert_not_called()

    def test_manual_page_is_used_without_opening_safari(self):
        import tempfile
        from automation.pdd.spider import config
        xml = ('<root><XCUIElementTypeNavigationBar label="猫零食好评榜" />'
               '<XCUIElementTypeStaticText label="好评率 96.74%" /></root>')
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, "OUTPUT_ROOT", Path(tmp)), \
                patch.object(ios, "ensure_wda", return_value=Mock()) as ensure, \
                patch.object(ios, "_current_page_source", return_value=xml), \
                patch.object(ios, "open_link") as navigate, \
                patch.object(ios, "_collect_current_rank_ios") as collect:
            ios.collect_leaderboards_ios_element(debug=True)
            ensure.assert_called_once_with(debug=True)
            navigate.assert_not_called()
            self.assertEqual(collect.call_args.args[1], "猫零食")
            self.assertEqual(collect.call_args.kwargs["initial_xml"], xml)
            self.assertFalse((Path(tmp) / ".rank_pending").exists())

    def test_debug_startup_uses_ready_wda_without_device_probe(self):
        with patch.object(ios, "wda_ready", return_value=True), \
                patch.object(ios, "device_connected") as probe:
            ios.ensure_wda(debug=True)
            probe.assert_not_called()

    def test_current_tab_reuses_initial_source(self):
        import tempfile
        from automation.pdd.spider import config
        xml = ('<root><XCUIElementTypeStaticText label="畅销榜" />'
               '<XCUIElementTypeStaticText label="热卖指数100" /></root>')
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, "OUTPUT_ROOT", Path(tmp)), \
                patch.object(ios, "_current_page_source") as source, \
                patch.object(ios, "_dump_diag"), \
                patch.object(ios, "_switch_rank_tab") as switch, \
                patch.object(ios, "_parse_rows", return_value=[]), \
                patch.object(ios, "_fill_product_links"):
            with self.assertRaises(RuntimeError):
                ios._collect_current_rank_ios(Mock(), "猫榜", initial_xml=xml)
            source.assert_not_called()
            switch.assert_not_called()

    def test_non_rank_page_stops(self):
        with patch.object(ios, "ensure_wda", return_value=Mock()), \
                patch.object(ios, "_current_page_source", return_value="<root/>"), \
                patch.object(ios, "_collect_current_rank_ios") as collect:
            with self.assertRaisesRegex(RuntimeError, "手动打开"):
                ios.collect_leaderboards_ios_element(debug=True)
            collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
