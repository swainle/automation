import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from automation.pdd.screenshot.config import Config
from automation.pdd.screenshot.runner import ShopRunner, load_shops, shop_folder_name
from automation.pdd.credentials import ShopCredential
from automation.com.feishu.sheets import SheetRecord
from automation.pdd.screenshot.export import export_receivables, parse_amount
from automation.pdd.screenshot.state import StateStore

DAY = "2026-10-29"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class ReportTests(unittest.TestCase):
    def test_old_database_migrates_and_completed_shops_refresh_source_row(self):
        import sqlite3
        from contextlib import closing
        with TemporaryDirectory() as folder:
            path = Path(folder) / "old.sqlite"
            store = StateStore(path)
            store.seed(DAY, ["成功店", "登录异常店", "历史店"])
            store.finish(DAY, "成功店", "success")
            store.finish(DAY, "登录异常店", "login_error", "verification_required")
            store.close()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("ALTER TABLE daily_shops DROP COLUMN row_number")
                connection.commit()
            store = StateStore(path)
            try:
                self.assertIsNone(store.get(DAY, "历史店")["row_number"])
                for source_row in (5, 12):
                    store.save_shop_details(DAY, [ShopCredential(name, "", "", source_row)
                                                  for name in ("成功店", "登录异常店")])
                    for name, status in (("成功店", "success"), ("登录异常店", "login_error")):
                        record = store.get(DAY, name)
                        self.assertEqual(record["row_number"], source_row)
                        self.assertEqual(record["status"], status)
                        self.assertTrue(store.completed(DAY, name))
                self.assertEqual(store.db.execute("SELECT count(*) FROM daily_shops").fetchone()[0], 3)
            finally:
                store.close()

    def test_embeds_image_and_persists_manager_without_credentials(self):
        import base64
        with TemporaryDirectory() as folder:
            image = Path(folder) / "shot.png"
            image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII="))
            shop = ShopCredential("【拼多多】测试店", "secret-account", "secret-password", 2, "陈贤")
            store = StateStore(Path(folder) / "db.sqlite")
            store.seed(DAY, [shop.shop_name])
            store.save_shop_details(DAY, [shop])
            self.assertEqual(store.get(DAY, shop.shop_name)["manager"], "陈贤")
            store.close()
            path = export_receivables(folder, DAY, [{"shop": shop.shop_name, "amount": "0.00",
                "image_path": str(image), "manager": shop.manager, "exception": ""}])
            with ZipFile(path) as archive:
                self.assertIn("xl/media/image1.png", archive.namelist())
                self.assertIn("陈贤", archive.read("xl/sharedStrings.xml").decode())
                self.assertIn("【拼多多】测试店", archive.read("xl/sharedStrings.xml").decode())

    def test_load_shops_keeps_source_row_and_original_identity(self):
        fields = {"店铺名": "123测试店", "账号": "account", "密码": "password", "店铺管理人员": "管理员"}
        with patch("automation.pdd.screenshot.runner.read_source_shops", return_value={
            '123测试店': {'manager': '管理员', 'row_number': 5}}), patch("automation.pdd.screenshot.runner.FeishuCredentials") as service:
            service.return_value.shop_field = "店铺名"
            service.return_value.account_field = "账号"
            service.return_value.password_field = "密码"
            service.return_value.fetch_records.return_value = [SheetRecord(fields, row_number=5)]
            shops = load_shops(Path.cwd())
        self.assertEqual(shops[0].shop_name, "123测试店")
        self.assertEqual(shops[0].row_number, 5)
        self.assertEqual(shops[0].manager, "管理员")
        self.assertEqual(shop_folder_name(shops[0]), "005123测试店")
        self.assertEqual(shop_folder_name(ShopCredential("店铺", "", "", 1000)), "1000店铺")

    def test_amount_validation(self):
        self.assertEqual(parse_amount("0"), "0.00")
        self.assertEqual(parse_amount("569.05"), "569.05")
        for value in (None, "", "查看金额", "-1", "1.234", "NaN", "1e10", "1000000000000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_amount(value)

    def test_numbered_exports_preserve_old_workbook_and_numeric_amount(self):
        with TemporaryDirectory() as folder:
            records = [{"shop": "=测试店", "amount": "569.05", "image_path": "D:/data/数据截图.jpg"},
                       {"shop": "金额尚未获取", "amount": None, "image_path": "D:/data/部分截图.jpg"}]
            first = export_receivables(folder, DAY, records)
            original = first.read_bytes()
            second = export_receivables(folder, DAY, records)
            third = export_receivables(folder, DAY, records)
            self.assertEqual(second.name, "店铺应收截图.xlsx")
            self.assertEqual(third.name, "店铺应收截图.xlsx")
            self.assertEqual((first.parent / "店铺应收截图.1.xlsx").read_bytes(), original)
            self.assertTrue((first.parent / "店铺应收截图.2.xlsx").is_file())
            with ZipFile(first) as archive:
                sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                self.assertEqual(sheet.find('.//s:c[@r="C3"]/s:v', NS).text, "569.05")
                self.assertEqual(sheet.find('.//s:c[@r="B3"]', NS).get("t"), "s")
                self.assertIsNone(sheet.find('.//s:c[@r="B3"]/s:f', NS))
                self.assertIsNone(sheet.find('.//s:c[@r="C4"]/s:v', NS))
                self.assertIn("附注图片/退店备注", archive.read("xl/sharedStrings.xml").decode())

    def test_records_survive_restart_and_refresh_does_not_keep_stale_amount(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "sqlite.db"
            store = StateStore(path)
            store.save_receivable(DAY, "店铺", "image.jpg", "569.05")
            store.close()
            store = StateStore(path)
            self.assertEqual(store.receivables(DAY)[0]["amount"], "569.05")
            self.assertEqual(store.receivables("2026-10-30"), [])
            store.save_receivable(DAY, "店铺", "image.jpg")
            self.assertIsNone(store.receivables(DAY)[0]["amount"])
            self.assertEqual(len(store.receivables(DAY)), 1)
            store.close()


class ExitExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_uses_sheet_order_even_when_records_were_saved_in_retry_order(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store = StateStore(root / "sqlite.db")
            try:
                store.save_receivable(DAY, "第二家", "second.jpg", "2.00")
                store.save_receivable(DAY, "第一家", "first.jpg", "1.00")
                runner = ShopRunner(Config(root=root), store, None)
                runner._run_day = AsyncMock()
                shops = [ShopCredential("第一家", "", "", 2), ShopCredential("第二家", "", "", 5)]
                with patch("automation.pdd.screenshot.runner.export_receivables", wraps=export_receivables) as export:
                    await runner.run_day(DAY, shops)
                rows = export.call_args.args[2]
                self.assertEqual([row["shop"] for row in rows], ["【拼多多】第一家", "【拼多多】第二家"])
                self.assertEqual([row["amount"] for row in rows], ["1.00", "2.00"])
            finally:
                store.close()

    async def test_daily_completion_cancellation_and_exception_all_export(self):
        for error in (None, asyncio.CancelledError(), RuntimeError("fixture failure")):
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as folder:
                root = Path(folder)
                store = StateStore(root / "sqlite.db")
                try:
                    store.save_receivable(DAY, "测试店", str(root / "数据截图.jpg"), "569.05")
                    runner = ShopRunner(Config(root=root), store, None)
                    runner._run_day = AsyncMock(side_effect=error)
                    if error is None:
                        await runner.run_day(DAY, [])
                    else:
                        with self.assertRaises(type(error)):
                            await runner.run_day(DAY, [])
                    self.assertTrue((root / "data" / DAY / "店铺应收截图.xlsx").is_file())
                finally:
                    store.close()
