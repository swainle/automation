"""cookies_server 存储层纯逻辑的本地测试。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from automation.com.cookies_server.storage import (
    StorageError,
    list_files,
    resolve_under,
    safe_filename,
    validate_cookies_data,
)


class ResolveUnderTests(unittest.TestCase):
    def test_rejects_absolute_posix_path(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(StorageError):
                resolve_under(Path(folder), "/etc/passwd")

    def test_rejects_backslash_and_drive(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(StorageError):
                resolve_under(root, r"C:\windows")
            with self.assertRaises(StorageError):
                resolve_under(root, "C:/windows")

    def test_rejects_parent_traversal(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(StorageError):
                resolve_under(Path(folder), "../secret.json")

    def test_rejects_empty(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(StorageError):
                resolve_under(Path(folder), "")

    def test_resolves_relative_subdir(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            target = resolve_under(root, "黄金万两/xxx.json")
            self.assertEqual(target, (root / "黄金万两" / "xxx.json").resolve())

    def test_keeps_chinese_filename(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            target = resolve_under(root, "一往无前宠物用品店cookie.json")
            self.assertEqual(target.name, "一往无前宠物用品店cookie.json")


class SafeFilenameTests(unittest.TestCase):
    def test_rejects_separators(self):
        for name in ("a/b.json", "a\\b.json"):
            with self.subTest(name=name):
                with self.assertRaises(StorageError):
                    safe_filename(name)

    def test_requires_json_suffix(self):
        with self.assertRaises(StorageError):
            safe_filename("cookies.txt")

    def test_accepts_chinese_name(self):
        self.assertEqual(safe_filename("一往无前宠物用品店cookie.json"), "一往无前宠物用品店cookie.json")


class ValidateTests(unittest.TestCase):
    def test_accepts_dict_with_cookies(self):
        validate_cookies_data({"url": "https://mms.pinduoduo.com/home", "cookies": [{"name": "a", "value": "b"}]})

    def test_accepts_array(self):
        validate_cookies_data([{"name": "a", "value": "b"}])

    def test_rejects_empty_cookies(self):
        with self.assertRaises(StorageError):
            validate_cookies_data({"cookies": []})

    def test_rejects_non_object_elements(self):
        with self.assertRaises(StorageError):
            validate_cookies_data(["x"])

    def test_rejects_scalar(self):
        with self.assertRaises(StorageError):
            validate_cookies_data("x")


class ListFilesTests(unittest.TestCase):
    def test_lists_only_top_level_json(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.json").write_text("{}", encoding="utf-8")
            (root / "b.txt").write_text("x", encoding="utf-8")
            sub = root / "sub"
            sub.mkdir()
            (sub / "c.json").write_text("{}", encoding="utf-8")
            items = list_files(root, root)
            self.assertEqual([item["name"] for item in items], ["a.json"])
            self.assertIsInstance(items[0]["size"], int)
            self.assertIsInstance(items[0]["modified"], str)

    def test_lists_subdir_with_relative_name(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            sub = root / "黄金万两"
            sub.mkdir()
            (sub / "x.json").write_text("{}", encoding="utf-8")
            items = list_files(sub, root)
            self.assertEqual([item["name"] for item in items], ["黄金万两/x.json"])

    def test_missing_directory_raises(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(StorageError):
                list_files(Path(folder) / "nope", Path(folder))


if __name__ == "__main__":
    unittest.main()
