import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from automation.pdd.credentials import CredentialError
from automation.com.feishu.sheets import column_name, read_sheet_records


class SheetRecordTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"FEISHU_HEADER_ROW": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service = SimpleNamespace(shop_field="店铺名", account_field="账号", password_field="密码")

    def test_reads_all_batches_after_blank_rows_and_maps_offset_columns(self):
        calls = []

        def get(uri):
            calls.append(uri)
            if uri.endswith("A1:E1"):
                rows = [["备注", "账号", "其他", "店铺名", "密码"]]
            elif uri.endswith("B2:E501"):
                rows = [["account1", "unused", "店铺一", " secret "], [], ["", "", "", ""]]
            elif uri.endswith("B502:E503"):
                rows = [["account2", "unused", "店铺二", "password2"]]
            else:
                raise AssertionError("unexpected range")
            return {"valueRange": {"values": rows}}

        records = read_sheet_records(get, "/sheets", {"sheetId": "abc", "rowCount": 503, "columnCount": 5}, self.service)
        self.assertEqual(len(calls), 3)
        self.assertEqual([r["店铺名"] for r in records], ["店铺一", "店铺二"])
        self.assertEqual(records[0]["密码"], " secret ")
        self.assertEqual(records[1]["账号"], "account2")
        self.assertEqual([record.row_number for record in records], [2, 502])

    def test_duplicate_or_missing_header_is_rejected(self):
        for header in (["店铺名", "账号"], ["店铺名", "账号", "密码", "账号"]):
            with self.assertRaises(CredentialError):
                read_sheet_records(lambda _: {"valueRange": {"values": [header]}}, "/sheets",
                                   {"sheetId": "abc", "rowCount": 2, "columnCount": 4}, self.service)

    def test_configurable_header_and_empty_trailing_cell(self):
        with patch.dict(os.environ, {"FEISHU_HEADER_ROW": "3"}):
            def get(uri):
                rows = [["店铺名", "账号", "密码"]] if uri.endswith("A3:C3") else [["店铺", "account"]]
                return {"valueRange": {"values": rows}}
            records = read_sheet_records(get, "/sheets", {"sheetId": "abc", "rowCount": 4, "columnCount": 3}, self.service)
            self.assertEqual(records[0]["密码"], "")
            self.assertEqual(records[0].row_number, 4)

    def test_blank_rows_preserve_original_row_numbers(self):
        def get(uri):
            rows = [["店铺名", "账号", "密码"]] if uri.endswith("A1:C1") else [
                ["店铺一", "account1", "password1"], [], [" ", "", ""],
                ["店铺二", "account2", "password2"]]
            return {"valueRange": {"values": rows}}
        records = read_sheet_records(get, "/sheets", {"sheetId": "abc", "rowCount": 5, "columnCount": 3}, self.service)
        self.assertEqual([record.row_number for record in records], [2, 5])
        self.assertEqual([list(record) for record in records], [["店铺名", "账号", "密码"]] * 2)

    def test_column_labels_beyond_z(self):
        self.assertEqual([column_name(i) for i in (1, 26, 27, 52, 703)], ["A", "Z", "AA", "AZ", "AAA"])

    def test_manager_column_outside_credentials_is_read(self):
        def get(uri):
            rows = [["店铺名", "账号", "密码", "备注", "店铺管理人员"]] if uri.endswith("A1:E1") else [
                ["【拼多多】测试店", "account", "password", "", "陈贤"]]
            return {"valueRange": {"values": rows}}
        records = read_sheet_records(get, "/sheets", {"sheetId": "abc", "rowCount": 2, "columnCount": 5}, self.service)
        self.assertEqual(records[0]["店铺管理人员"], "陈贤")


if __name__ == "__main__":
    unittest.main()
