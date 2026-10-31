"""只读取飞书并匹配 Cookie 文件名，不操作浏览器。"""

import argparse
from pathlib import Path
import unittest

from automation.pdd.credentials import match_shop_credentials

ROOT = Path(__file__).resolve().parents[3]
OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动读取真实飞书")
class MatchShopTests(unittest.TestCase):
    def test_match_shop(self):
        paths = [OPTIONS.cookies]
        if OPTIONS.cookies_dir:
            paths = sorted(p for p in OPTIONS.cookies_dir.iterdir() if p.is_file() and p.suffix.lower() == ".json" and p.stem.lower().endswith(("cookie", "cookies")))
        self.assertTrue(paths and all(p.is_file() for p in paths), "未找到 Cookie 文件")
        results = match_shop_credentials(ROOT, paths, required=True)
        self.assertTrue(all(item.credential is not None for item in results), "存在未匹配、重复或缺少账号密码的店铺")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, default=ROOT / "一往无前宠物用品店cookie.json")
    parser.add_argument("--cookies-dir", type=Path)
    OPTIONS = parser.parse_args()
    unittest.main(argv=[__file__], verbosity=2)
