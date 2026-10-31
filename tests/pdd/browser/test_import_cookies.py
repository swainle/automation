"""只导入拼多多 Cookie，不打开页面。"""

import argparse
from pathlib import Path
import unittest

from automation.com.browser.connection import connect_context, import_cookies
from automation.pdd.storage.cookies import load_cookies

ROOT = Path(__file__).resolve().parents[3]
OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动操作调试浏览器 Cookie")
class ImportCookiesTests(unittest.TestCase):
    def test_import_cookies(self):
        from playwright.sync_api import sync_playwright
        cookies = load_cookies(OPTIONS.cookies)
        with sync_playwright() as p:
            context = connect_context(p, OPTIONS.port)
            import_cookies(context, cookies)
        print(f"已导入 {len(cookies)} 条拼多多 Cookie，未导航页面；如需生效请手动刷新。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--cookies", type=Path, default=ROOT / "一往无前宠物用品店cookie.json")
    OPTIONS = parser.parse_args()
    unittest.main(argv=[__file__], verbosity=2)
