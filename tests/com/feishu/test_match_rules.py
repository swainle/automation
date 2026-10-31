import unittest
from pathlib import Path
from types import SimpleNamespace

from automation.pdd.credentials import (
    CredentialError, FeishuCredentials, cookie_shop_name, match_records,
)


class CredentialTests(unittest.TestCase):
    def match(self, records):
        return match_records([Path("一往无前宠物用品店cookie.json")], records,
                             "店铺名称", "账号", "店铺登录密码")[0]

    def test_exact_matching_trims_shop_and_preserves_password(self):
        result = self.match([
            {"店铺名称": "一往无前宠物用品店分店", "账号": "wrong", "店铺登录密码": "wrong"},
            {"店铺名称": [{"text": " 一往无前宠物用品店 "}], "账号": [{"text": "account"}], "店铺登录密码": " secret "},
        ])
        self.assertEqual(result.credential.account, "account")
        self.assertEqual(result.credential.password, " secret ")
        self.assertNotIn("secret", repr(result))
        self.assertNotIn("account", repr(result.credential))

    def test_similar_name_does_not_match(self):
        self.assertIsNone(self.match([{"店铺名称": "一往无前宠物用品店分店"}]).credential)

    def test_duplicates_are_skipped_even_if_one_has_missing_credentials(self):
        result = self.match([{"店铺名称": "一往无前宠物用品店"}] * 2)
        self.assertIn("重复", result.status)
        self.assertIsNone(result.credential)

    def test_missing_or_nontext_password_is_skipped(self):
        for password in (None, "", " ", True, {"value": "secret"}):
            result = self.match([{"店铺名称": "一往无前宠物用品店", "账号": "account", "店铺登录密码": password}])
            self.assertIsNone(result.credential)

    def test_numeric_credentials_are_preserved_without_decimal_suffix(self):
        result = self.match([{"店铺名称": "一往无前宠物用品店", "账号": 13800138000,
                              "店铺登录密码": 123456.0}])
        self.assertEqual(result.credential.account, "13800138000")
        self.assertEqual(result.credential.password, "123456")

    def test_archived_cookie_path(self):
        path = Path("data/一往无前宠物用品店/2026-10-29/cookies.json")
        self.assertEqual(cookie_shop_name(path), "一往无前宠物用品店")

    def test_api_error_does_not_expose_response(self):
        response = SimpleNamespace(success=lambda: False, code=123, msg="sensitive password")
        with self.assertRaises(CredentialError) as caught:
            FeishuCredentials.checked(response, "读取")
        self.assertNotIn("sensitive", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
